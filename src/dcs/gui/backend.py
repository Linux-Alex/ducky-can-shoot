"""The object QML talks to.

Everything the user interface can do goes through this facade: it owns the
configuration, the models, the worker thread and the small desktop
integrations (clipboard, file manager).  Keeping it in one place means the QML
files stay declarative and contain no logic worth testing.
"""

from __future__ import annotations

import contextlib
import copy
import os
import shlex
import subprocess
import threading
from dataclasses import fields, replace
from typing import Any

from PyQt6.QtCore import (
    QCoreApplication,
    QObject,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QClipboard, QDesktopServices, QGuiApplication

from dcs.core import autostart, service, updates
from dcs.core.config import (
    WATCH_MODES,
    AppConfig,
    EmailConfig,
    default_config_path,
    load_config,
    write_default_config,
)
from dcs.core.device import (
    find_device,
    list_removable_devices,
    mount_device,
    unmount_device,
)
from dcs.core.events import STAGE_ORDER
from dcs.core.installer import Installer, build_plan, manual_instructions
from dcs.core.installer import verify as verify_clamav
from dcs.core.log import get_logger, log_ring
from dcs.core.notify.desktop import ACTION_OPEN, build_notifier
from dcs.core.pipeline import JobRequest
from dcs.core.scanner import Scanner
from dcs.core.upload import provider_catalog
from dcs.core.util import human_bytes
from dcs.core.watch import MODE_DESCRIPTIONS, MODE_LABELS
from dcs.gui.models import (
    DevicesModel,
    FindingsModel,
    LogModel,
    StagesModel,
    TextLinesModel,
    ThroughputModel,
)
from dcs.gui.watcher import UsbWatcher, device_summary
from dcs.gui.worker import PipelineWorker
from dcs.version import APP_HOMEPAGE, APP_NAME, __version__

log = get_logger("gui")

IDLE = "idle"
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"


class AsyncRunner(QObject):
    """Runs one blocking callable on a plain thread and reports the result."""

    done = pyqtSignal(object)
    problem = pyqtSignal(str)

    def __init__(self, fn, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    @pyqtSlot()
    def start(self) -> None:
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def _run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:
            self.problem.emit(str(exc))


class Backend(QObject):
    """Facade between the QML views and dcs.core."""

    stateChanged = pyqtSignal()
    devicesChanged = pyqtSignal()
    selectionChanged = pyqtSignal()
    summaryChanged = pyqtSignal()
    settingsChanged = pyqtSignal()
    busyChanged = pyqtSignal()
    toast = pyqtSignal(str, str)  # level, message
    testResult = pyqtSignal(str, bool, str)  # what, ok, message
    #: a medium was plugged in while the interface was open
    deviceInserted = pyqtSignal("QVariantMap")
    pendingDeviceChanged = pyqtSignal()
    settingsSaved = pyqtSignal(bool, str)  # ok, message
    watchStateChanged = pyqtSignal()
    installStateChanged = pyqtSignal()
    installFinished = pyqtSignal(object)
    #: the update check or its installation changed state (see updateState)
    updateStateChanged = pyqtSignal()
    #: progress lines from the update thread; the message is updated on the GUI thread
    updateLineReady = pyqtSignal(str)
    #: emitted from the installer thread; the model lives in the GUI thread, so
    #: Qt delivers these as queued calls and the model is only touched there
    installLineReady = pyqtSignal(str)

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        poll_devices: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config or load_config()
        self.devices_model = DevicesModel(self)
        self.stages_model = StagesModel(self)
        self.findings_model = FindingsModel(self)
        self.log_model = LogModel(self)
        self.throughput_model = ThroughputModel(self)
        self.install_model = TextLinesModel(self)
        self.installLineReady.connect(self.install_model.append)

        self._state = IDLE
        self._busy = False
        self._progress: dict[str, Any] = {}
        #: last progress event per stage: the headline tiles must keep showing the
        #: scan numbers while a later stage (the upload, which has no file count)
        #: is the one currently reporting
        self._progress_by_stage: dict[str, dict[str, Any]] = {}
        self._summary: dict[str, Any] = {}
        self._selected_row = -1
        self._selected_folder = ""
        self._source_label = ""
        self._overall = 0.0
        self._engine_status: list[dict[str, Any]] = []
        self._providers: list[dict[str, Any]] = provider_catalog()
        self._thread: QThread | None = None
        self._worker: PipelineWorker | None = None
        self._runners: list[AsyncRunner] = []
        self._installer: Installer | None = None
        self._install_thread: threading.Thread | None = None
        self._install_state = "idle"
        self._install_outcome: dict[str, Any] = {}
        #: idle | checking | uptodate | available | installing | restarting | error
        self._update_state = "idle"
        self._update_check: updates.UpdateCheck | None = None
        self._update_message = ""
        self._restart_pending = False
        self.updateLineReady.connect(self._on_update_line)
        #: the medium a notification or an automatic run is waiting for
        self._pending_device: dict[str, Any] = {}
        #: last save outcome, shown next to the save button instead of only in
        #: the window wide toast (which is easy to miss on a long page)
        self._save_message = ""
        self._save_ok = True

        # Watches for media plugged in while the window is open.  The mode is
        # "off" by default; apply_config() only starts the timer when the user
        # asked for it (see the USB detection card in the settings page).
        self.watcher = UsbWatcher(self.config, self)
        self.watcher.inserted.connect(self._on_device_inserted)

        log_ring().add_listener(self._on_log_record)
        self.refreshDevices()
        self.refreshEngineStatus()
        self._watch_state = ""
        if poll_devices:
            self._poll = QTimer(self)
            self._poll.setInterval(3000)
            self._poll.timeout.connect(self._refresh_watch_state)
            self._poll.start()

    # ------------------------------------------------------------------
    # models exposed to QML
    # ------------------------------------------------------------------
    @pyqtProperty(QObject, constant=True)
    def devices(self) -> DevicesModel:
        return self.devices_model

    @pyqtProperty(QObject, constant=True)
    def stages(self) -> StagesModel:
        return self.stages_model

    @pyqtProperty(QObject, constant=True)
    def findings(self) -> FindingsModel:
        return self.findings_model

    @pyqtProperty(QObject, constant=True)
    def logs(self) -> LogModel:
        return self.log_model

    @pyqtProperty(QObject, constant=True)
    def throughput(self) -> ThroughputModel:
        return self.throughput_model

    # ------------------------------------------------------------------
    # simple state
    # ------------------------------------------------------------------
    @pyqtProperty(str, notify=stateChanged)
    def state(self) -> str:
        return self._state

    @pyqtProperty(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @pyqtProperty(float, notify=stateChanged)
    def overallPercent(self) -> float:
        return self._overall

    @pyqtProperty(str, notify=selectionChanged)
    def sourceLabel(self) -> str:
        return self._source_label

    @pyqtProperty(str, notify=selectionChanged)
    def sourcePath(self) -> str:
        return self._selected_folder

    @pyqtProperty(int, notify=selectionChanged)
    def selectedRow(self) -> int:
        return self._selected_row

    @pyqtProperty("QVariantMap", notify=summaryChanged)
    def summary(self) -> dict[str, Any]:
        return self._summary

    @pyqtProperty("QVariantList", notify=settingsChanged)
    def engineStatus(self) -> list[dict[str, Any]]:
        return self._engine_status

    @pyqtProperty("QVariantList", constant=True)
    def providers(self) -> list[dict[str, Any]]:
        return self._providers

    @pyqtProperty("QVariantMap", notify=settingsChanged)
    def settings(self) -> dict[str, Any]:
        return self.config.to_dict(mask_secrets=True)

    @pyqtProperty(str, constant=True)
    def configPath(self) -> str:
        return self.config.source_path or str(write_default_config())

    @pyqtProperty(str, constant=True)
    def appVersion(self) -> str:
        return __version__

    @pyqtProperty(str, constant=True)
    def appName(self) -> str:
        return APP_NAME

    @pyqtProperty(str, constant=True)
    def appHomepage(self) -> str:
        return APP_HOMEPAGE

    # ------------------------------------------------------------------
    # live values for the progress view
    #
    # The stage list is driven by StagesModel; these string properties are the
    # headline numbers, and they all notify through stateChanged, which the
    # backend emits on every progress event.
    # ------------------------------------------------------------------
    def _live(self, key: str, fallback: str = "") -> str:
        value = self._progress.get(key)
        return str(value) if value not in (None, "") else fallback

    def _scan_progress(self) -> dict[str, Any]:
        """The last progress event of the scan stage, which owns the counters."""
        return self._progress_by_stage.get("scan", {})

    def _finished_summary(self) -> dict[str, Any]:
        return self._summary if self._state in (FINISHED, FAILED) else {}

    @pyqtProperty(str, notify=stateChanged)
    def liveStageTitle(self) -> str:
        return self._live("title", "Waiting")

    @pyqtProperty(str, notify=stateChanged)
    def liveCurrent(self) -> str:
        return self._live("current")

    @pyqtProperty(str, notify=stateChanged)
    def liveRateText(self) -> str:
        from dcs.core.util import human_rate

        if self._state == RUNNING and (self._progress.get("rate") or 0) > 0:
            return self._live("rateText", "-")
        # once the job is over the instantaneous rate is gone, but the average
        # over the scan is still meaningful
        summary = self._finished_summary()
        if summary:
            stats = ((summary.get("scan") or {}).get("stats") or {})
            duration = float(stats.get("duration") or 0.0)
            scanned = float(summary.get("bytesScanned") or 0)
            if duration > 0 and scanned > 0:
                return human_rate(scanned / duration)
        scan = self._scan_progress()
        if (scan.get("rate") or 0) > 0:
            return str(scan.get("rateText") or "-")
        return "-"

    @pyqtProperty(str, notify=stateChanged)
    def liveEtaText(self) -> str:
        return self._live("etaText", "--:--")

    @pyqtProperty(str, notify=stateChanged)
    def liveElapsedText(self) -> str:
        summary = self._finished_summary()
        if summary:
            return str(summary.get("durationText") or "-")
        scan = self._scan_progress()
        return str(scan.get("elapsedText") or self._live("elapsedText", "00:00"))

    @pyqtProperty(str, notify=stateChanged)
    def liveFilesText(self) -> str:
        summary = self._finished_summary()
        if summary:
            return f"{int(summary.get('filesScanned') or 0):,}"
        scan = self._scan_progress()
        total = int(scan.get("total") or 0)
        processed = int(scan.get("processed") or 0)
        if total:
            return f"{processed:,} / {total:,}"
        if processed:
            return f"{processed:,}"
        # the upload reports bytes only, and before the first scan event there is
        # simply nothing to count yet
        return "0" if self._state == RUNNING else "-"

    @pyqtProperty(str, notify=stateChanged)
    def liveBytesText(self) -> str:
        summary = self._finished_summary()
        if summary:
            return str(summary.get("bytesScannedText") or "-")
        scan = self._scan_progress()
        done = str(scan.get("bytesProcessedText") or self._live("bytesProcessedText", "0 B"))
        total = str(scan.get("bytesTotalText") or self._live("bytesTotalText") or "")
        return f"{done} / {total}" if total and total != "0 B" else done

    @pyqtProperty(str, notify=stateChanged)
    def liveMessage(self) -> str:
        return self._live("message")

    @pyqtProperty(float, notify=stateChanged)
    def livePercent(self) -> float:
        return float(self._progress.get("percent") or 0.0)

    @pyqtProperty(float, notify=stateChanged)
    def liveRate(self) -> float:
        return float(self._progress.get("rate") or 0.0)

    @pyqtSlot(int, result="QVariantMap")
    def stageRow(self, row: int) -> dict[str, Any]:
        """Return one pipeline stage as a plain dict (used by the QML views)."""
        model = self.stages_model
        if row < 0 or row >= model.rowCount():
            return {}
        index = model.index(row, 0)
        keys = {
            "id": model.IdRole,
            "title": model.TitleRole,
            "description": model.DescriptionRole,
            "icon": model.IconRole,
            "state": model.StateRole,
            "percent": model.PercentRole,
            "message": model.MessageRole,
            "current": model.CurrentRole,
            "elapsed": model.ElapsedRole,
            "eta": model.EtaRole,
            "rate": model.RateRole,
            "processed": model.ProcessedRole,
        }
        return {name: model.data(index, role) for name, role in keys.items()}

    # ------------------------------------------------------------------
    # devices
    # ------------------------------------------------------------------
    @pyqtSlot()
    def refreshDevices(self) -> None:
        try:
            devices = list_removable_devices()
        except Exception as exc:
            log.warning("cannot list devices: %s", exc)
            devices = []
        previous = self._current_device_path()
        self.devices_model.replace(devices)
        self.devicesChanged.emit()
        if previous:
            for row, device in enumerate(devices):
                if device.path == previous:
                    self._selected_row = row
                    self._selected_folder = device.mount_path
                    self.selectionChanged.emit()
                    break
        elif devices and self._selected_row < 0 and self.config.gui.auto_select_new_device:
            self.selectDevice(0)

    def _current_device_path(self) -> str:
        device = self.devices_model.device_at(self._selected_row)
        return device.path if device else ""

    @pyqtSlot(int)
    def selectDevice(self, row: int) -> None:
        device = self.devices_model.device_at(row)
        if device is None:
            return
        self._selected_row = row
        self._selected_folder = device.mount_path
        self._source_label = device.display_name
        self.selectionChanged.emit()

    @pyqtSlot(str)
    def setFolder(self, url_or_path: str) -> None:
        path = url_or_path
        if path.startswith("file:"):
            path = QUrl(path).toLocalFile()
        self._selected_folder = path
        self._selected_row = -1
        self._source_label = os.path.basename(path.rstrip("/")) or path
        self.selectionChanged.emit()

    @pyqtSlot(result=bool)
    def mountSelected(self) -> bool:
        device = self.devices_model.device_at(self._selected_row)
        if device is None:
            self.toast.emit("warning", "No device is selected")
            return False
        if device.mounted:
            self._selected_folder = device.mount_path
            self.selectionChanged.emit()
            return True
        ok, message = mount_device(device)
        if ok:
            self._selected_folder = message
            self.toast.emit("info", f"Mounted {device.path} at {message}")
            self.refreshDevices()
            self.selectionChanged.emit()
        else:
            self.toast.emit("error", f"Could not mount {device.path}: {message}")
        return ok

    @pyqtSlot(result=bool)
    def unmountSelected(self) -> bool:
        device = self.devices_model.device_at(self._selected_row)
        if device is None:
            return False
        ok, message = unmount_device(device)
        self.toast.emit("info" if ok else "error", message or "done")
        self.refreshDevices()
        return ok

    # ------------------------------------------------------------------
    # scanning / settings helpers
    # ------------------------------------------------------------------
    @pyqtSlot()
    def refreshEngineStatus(self) -> None:
        try:
            self._engine_status = [status.to_dict() for status in Scanner(self.config.scan).statuses()]
        except Exception as exc:
            log.warning("cannot probe scan engines: %s", exc)
            self._engine_status = []
        self.settingsChanged.emit()

    @pyqtSlot("QVariantMap", result=str)
    def validateDraft(self, values: dict[str, Any]) -> str:
        """What would be wrong if this draft were saved, as one message.

        The settings page edits a draft that only reaches the config file on
        Save, so the page asks here instead of validating the stored config -
        otherwise the message would describe values the user is not looking at.
        """
        try:
            candidate = copy.deepcopy(self.config)
            candidate.merge(values or {})
            problems = candidate.validation_problems()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("cannot validate the settings draft: %s", exc)
            return ""
        return "\n".join(f"• {problem}" for problem in problems)

    @pyqtSlot("QVariantMap")
    def saveSettings(self, values: dict[str, Any]) -> None:
        """Apply the settings page to the config object and persist it."""
        self.config.merge(values)
        path = self.config.source_path or str(default_config_path())
        try:
            path = self.config.save()
        except OSError as exc:
            self._save_ok = False
            # HIG: an error says what happened *and* what to do about it
            self._save_message = f"Could not save: {exc}"
            self.toast.emit(
                "error",
                f"Could not save the settings: {exc}. Check that the folder holding "
                f"{path} can be written to.",
            )
        else:
            self._save_ok = True
            self._save_message = f"Saved to {path}"
            self.toast.emit("info", f"Settings saved to {path}")
        self._apply_watch_settings()
        self.refreshEngineStatus()
        self.settingsChanged.emit()
        self.settingsSaved.emit(self._save_ok, self._save_message)

    def _apply_watch_settings(self) -> None:
        """Follow the USB settings at once: timer, autostart entry, watcher process.

        Enabling a mode also starts the background watcher *now*: the autostart
        entry only takes effect at the next login, and until then nothing would
        be listening while the window is closed.
        """
        self.watcher.apply_config(self.config)
        wanted = bool(self.config.watch.autostart) and self.config.watch.mode != "off"
        if autostart.sync(wanted):
            log.info(
                "autostart entry %s",
                "installed" if autostart.is_enabled() else "removed",
            )
        if self.config.watch.mode == "off":
            log.debug("watch mode is off; the background watcher keeps running until stopped")
        else:
            started, message = service.start()
            log.info("background watcher: %s", message)
            if started:
                self.toast.emit("info", f"The background watcher is {message}")
        self.watchStateChanged.emit()

    @pyqtSlot()
    def reloadSettings(self) -> None:
        self.config = load_config(self.config.source_path or None)
        self.refreshEngineStatus()
        self.settingsChanged.emit()

    @pyqtSlot(result=str)
    def validationProblems(self) -> str:
        return "\n".join(self.config.validation_problems())

    # ------------------------------------------------------------------
    # settings feedback, and the USB watcher
    # ------------------------------------------------------------------
    @pyqtProperty(str, notify=settingsChanged)
    def saveMessage(self) -> str:
        """Where the last save landed, shown next to the save button."""
        return self._save_message

    @pyqtProperty(bool, notify=settingsChanged)
    def saveOk(self) -> bool:
        return self._save_ok

    @pyqtProperty("QVariantMap", notify=pendingDeviceChanged)
    def pendingDevice(self) -> dict[str, Any]:
        """The medium the interface is offering to triage (empty when none)."""
        return self._pending_device

    @pyqtProperty(str, notify=watchStateChanged)
    def watchMode(self) -> str:
        return self.config.watch.mode

    @pyqtProperty("QVariantList", constant=True)
    def watchModes(self) -> list[dict[str, Any]]:
        """The four reactions, for the combo box in the settings page."""
        return [
            {"value": mode, "label": MODE_LABELS[mode], "description": MODE_DESCRIPTIONS[mode]}
            for mode in WATCH_MODES
        ]

    @pyqtProperty(bool, notify=watchStateChanged)
    def watchActive(self) -> bool:
        """True while this window is listening for new media."""
        return self.watcher.active

    @pyqtProperty(bool, notify=watchStateChanged)
    def watchAutostart(self) -> bool:
        return autostart.is_enabled()

    @pyqtProperty(str, constant=True)
    def autostartPath(self) -> str:
        return str(autostart.autostart_path())

    @pyqtProperty(str, notify=watchStateChanged)
    def watchService(self) -> str:
        """What the background watcher is doing right now."""
        pid = service.watcher_pid()
        return f"running (pid {pid})" if pid else "not running"

    @pyqtProperty(str, constant=True)
    def watchLogPath(self) -> str:
        return str(service.log_path())

    @pyqtSlot(result=bool)
    def startWatchService(self) -> bool:
        """Start the background watcher now, without waiting for a login."""
        started, message = service.start()
        self.watchStateChanged.emit()
        self.toast.emit("info", ("Started: " if started else "Already running: ") + message)
        return started

    @pyqtSlot()
    def stopWatchService(self) -> None:
        stopped, message = service.stop()
        self.watchStateChanged.emit()
        self.toast.emit("info" if stopped else "warning", message)

    @pyqtSlot()
    def acceptPendingDevice(self) -> None:
        """Select the medium that was plugged in (mounting it if needed)."""
        payload = dict(self._pending_device)
        node = str(payload.get("node") or "")
        if not node:
            return
        if not self.openDevice(node):
            return
        self.toast.emit("info", f"{device_summary(payload)} is selected")

    @pyqtSlot()
    def runPendingDevice(self) -> None:
        """Start the whole job on the medium that was plugged in."""
        payload = dict(self._pending_device)
        node = str(payload.get("node") or "")
        if not node:
            return
        if self.openDevice(node, autorun=True):
            self._pending_device = {}
            self.pendingDeviceChanged.emit()

    @pyqtSlot()
    def dismissPendingDevice(self) -> None:
        self._pending_device = {}
        self.pendingDeviceChanged.emit()

    def _refresh_watch_state(self) -> None:
        """Poll devices, and tell the interface when the watcher comes or goes."""
        state = self.watchService
        if state != self._watch_state:
            self._watch_state = state
            self.watchStateChanged.emit()
        self.refreshDevices()

    @pyqtSlot(str, result=bool)
    def openDevice(self, node: str, autorun: bool = False) -> bool:
        """Select a medium by node, mount it when needed, optionally run the job."""
        device = find_device(node)
        if device is None:
            self.toast.emit("error", f"{node} is not a device DCS can see")
            return False
        if not device.mounted:
            ok, message = mount_device(device)
            if not ok:
                self.toast.emit("error", f"Could not mount {device.path}: {message}")
                return False
            device = find_device(node) or device
        self.refreshDevices()
        row = self._row_for_device(device.path, device.mount_path)
        if row >= 0:
            self.selectDevice(row)
        else:  # the medium is usable but lsblk did not list it as removable
            self.setFolder(device.mount_path or device.path)
        if autorun:
            if self._state == RUNNING:
                self.toast.emit("warning", "A job is already running")
                return True
            self.startJob(self.autoRunOptions())
        return True

    def _row_for_device(self, path: str, mount_path: str) -> int:
        for row in range(self.devices_model.rowCount()):
            device = self.devices_model.device_at(row)
            if device is None:
                continue
            if device.path == path or (mount_path and device.mount_path == mount_path):
                return row
        return -1

    def autoRunOptions(self) -> dict[str, Any]:
        """Job options for an automatic run (watch.mode = open_run).

        Everything the configuration enables is done without asking, because
        nobody is at the keyboard: the archive is built, the public link is
        created and, when a recipient is configured, the mail is sent.  Without
        a recipient the job stops at the report and the link is shown on the
        result page.
        """
        config = self.config
        recipients = ", ".join(config.email.to_addrs)
        return {
            "source": self._selected_folder,
            "label": self._source_label,
            "scan": True,
            "archive": config.archive.enabled,
            "upload": config.upload.enabled,
            "email": bool(config.email.enabled and config.email.to_addrs),
            "recipients": recipients,
            "dryRun": False,
            "assumeYes": True,
        }

    def _on_device_inserted(self, payload: dict[str, Any]) -> None:
        """React to a medium the watcher just noticed."""
        mode = self.config.watch.mode
        if mode == "off":
            return
        self._pending_device = dict(payload)
        self.pendingDeviceChanged.emit()
        self.deviceInserted.emit(self._pending_device)
        if mode == "notify":
            self.toast.emit("info", f"USB medium plugged in: {device_summary(payload)}")
            # HIG: no system notification while the window is in the foreground,
            # where it can appear away from where the user is looking; the banner
            # and the passive notification carry the message there instead.
            if not self._window_is_active():
                self._notify_desktop(payload)
            return
        if mode == "open":
            self.acceptPendingDevice()
            return
        if mode == "open_run":
            self.toast.emit("info", f"Starting on {device_summary(payload)}")
            self.runPendingDevice()

    @staticmethod
    def _window_is_active() -> bool:
        """True while this application is the active one on screen."""
        application = QGuiApplication.instance()
        if application is None:  # pragma: no cover - headless tests
            return False
        return application.applicationState() == Qt.ApplicationState.ApplicationActive

    def _notify_desktop(self, payload: dict[str, Any]) -> None:
        """Show a notification with an Open button; the answer opens the window.

        notify-send waits for the user, so it runs on a worker thread: the
        window must stay responsive while the notification is on screen.
        """
        notifier = build_notifier(timeout_ms=self.config.watch.notify_timeout_ms)
        title = f"USB medium plugged in: {payload.get('displayName') or payload.get('node')}"
        body = (
            f"{device_summary(payload)}. Open {APP_NAME} to scan it, archive it and mail "
            "the verdict; nothing has been read from the medium yet."
        )
        def work() -> str:
            return notifier.notify(
                title, body, actions=((ACTION_OPEN, f"Open {APP_NAME}"),)
            )

        def answered(choice: Any) -> None:
            if choice == ACTION_OPEN:
                self.acceptPendingDevice()
            elif not notifier.available():
                self.toast.emit("warning", "Install libnotify-bin to get a notification")

        self._run_async(work, answered, lambda error: log.warning("notification failed: %s", error))

    # ------------------------------------------------------------------
    # job control
    # ------------------------------------------------------------------
    @pyqtSlot("QVariantMap")
    def startJob(self, options: dict[str, Any]) -> None:
        if self._state == RUNNING:
            self.toast.emit("warning", "A job is already running")
            return
        source = str(options.get("source") or self._selected_folder or "")
        if not source:
            self.toast.emit("error", "Select a USB device or folder first")
            return
        if not os.path.isdir(source):
            self.toast.emit("error", f"{source} is not a directory")
            return

        device_path = ""
        device = self.devices_model.device_at(self._selected_row)
        if device is not None and device.mount_path == source:
            device_path = device.path

        recipients = options.get("recipients") or []
        if isinstance(recipients, str):
            recipients = [part.strip() for part in recipients.replace(";", ",").split(",") if part.strip()]

        request = JobRequest(
            source_path=source,
            label=str(options.get("label") or self._source_label or os.path.basename(source)),
            device_path=device_path,
            do_scan=bool(options.get("scan", True)),
            do_archive=bool(options.get("archive", True)),
            do_upload=bool(options.get("upload", True)),
            do_email=bool(options.get("email", True)),
            recipients=list(recipients),
            email_dry_run=bool(options.get("dryRun", False)),
            assume_yes=bool(options.get("assumeYes", False)),
        )

        self.stages_model.reset_stages()
        self.findings_model.clear()
        self.throughput_model.clear()
        self._progress = {}
        self._progress_by_stage = {}
        self._summary = {}
        self._overall = 0.0
        self._set_state(RUNNING)

        self._thread = QThread(self)
        self._worker = PipelineWorker(self.config, request)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.stageChanged.connect(self.stages_model.set_state)
        self._worker.progressChanged.connect(self._on_progress)
        self._worker.findingFound.connect(self.findings_model.append)
        self._worker.logMessage.connect(self._on_worker_log)
        self._worker.summaryReady.connect(self._on_summary)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    @pyqtSlot()
    def cancelJob(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()
            self.toast.emit("warning", "Cancelling the job...")

    def _set_state(self, state: str) -> None:
        self._state = state
        self._busy = state == RUNNING
        self.stateChanged.emit()
        self.busyChanged.emit()
        if state != RUNNING:
            # the task manager badge shows the progress of a running job only
            self._set_badge(0)

    @staticmethod
    def _set_badge(value: int) -> None:
        """Show (or clear) the completion percentage on the task manager icon.

        The HIG asks for this for a long job the user started: Plasma draws the
        number as a progress bar on the app's icon in the task manager, which is
        where a user looks when the window is behind something else.
        """
        application = QGuiApplication.instance()
        if application is None:  # pragma: no cover - headless tests
            return
        with contextlib.suppress(AttributeError, TypeError):  # pragma: no cover - old Qt
            application.setBadgeNumber(int(value))

    @pyqtSlot(object)
    def _on_progress(self, payload: Any) -> None:
        if isinstance(payload, dict):
            self._progress = payload
            stage = str(payload.get("stage", ""))
            if stage:
                self._progress_by_stage[stage] = payload
            self.stages_model.apply_progress(payload)
            self.throughput_model.append(payload)
            index = 0
            for position, stage in enumerate(STAGE_ORDER):
                if str(stage) == str(payload.get("stage", "")):
                    index = position
                    break
            self._overall = (index + float(payload.get("percent") or 0.0)) / len(STAGE_ORDER)
            self.stateChanged.emit()
            self._set_badge(round(self._overall * 100))

    @pyqtSlot(str, str)
    def _on_worker_log(self, level: str, message: str) -> None:
        log.log({"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 20), message)

    def _on_log_record(self, record: Any) -> None:
        # called from whichever thread logged; marshal through a queued signal
        QTimer.singleShot(0, lambda: self.log_model.append(record))

    @pyqtSlot(object)
    def _on_summary(self, summary: Any) -> None:
        if isinstance(summary, dict):
            self._summary = summary
            self.summaryChanged.emit()

    @pyqtSlot(object)
    def _on_finished(self, summary: Any) -> None:
        if isinstance(summary, dict):
            self._summary = summary
            self.summaryChanged.emit()
        self._overall = 1.0
        self._set_state(FINISHED)
        self.toast.emit("info", "Job finished")
        self.refreshDevices()

    @pyqtSlot(str)
    def _on_failed(self, message: str) -> None:
        self._set_state(FAILED)
        self.toast.emit("error", message)

    def _cleanup_thread(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None

    # ------------------------------------------------------------------
    # desktop integration
    # ------------------------------------------------------------------
    @pyqtSlot(str)
    def copyText(self, text: str) -> None:
        clipboard: QClipboard | None = QGuiApplication.clipboard()
        if clipboard is not None and text:
            clipboard.setText(text)
            self.toast.emit("info", "Copied to the clipboard")

    @pyqtSlot(str)
    def openPath(self, path: str) -> None:
        if not path:
            return
        target = path if os.path.isdir(path) else os.path.dirname(path)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(target)):
            self.toast.emit("error", f"Could not open {target}")

    @pyqtSlot(str)
    def openUrl(self, url: str) -> None:
        if url and not QDesktopServices.openUrl(QUrl(url)):
            self.toast.emit("error", f"Could not open {url}")

    # ------------------------------------------------------------------
    # updates
    # ------------------------------------------------------------------
    @pyqtProperty(str, notify=updateStateChanged)
    def updateState(self) -> str:
        """idle, checking, uptodate, available, installing, restarting or error."""
        return self._update_state

    @pyqtProperty("QVariantMap", notify=updateStateChanged)
    def updateInfo(self) -> dict[str, Any]:
        """Everything the settings card shows about the last check."""
        info = self._update_check.to_dict() if self._update_check else {
            "current": __version__,
            "latest": "",
            "newer": False,
            "canInstall": False,
            "asset": {},
            "install": {},
            "otherInstalls": [],
            "message": "",
            "releaseUrl": "",
        }
        info["state"] = self._update_state
        if self._update_message:
            info["message"] = self._update_message
        return info

    def _set_update_state(self, state: str, message: str = "") -> None:
        self._update_state = state
        if message:
            self._update_message = message
        self.updateStateChanged.emit()

    @pyqtSlot()
    def checkForUpdates(self) -> None:
        """Ask GitHub for the newest release (threaded; never blocks the window)."""
        if self._update_state in ("checking", "installing", "restarting"):
            return
        self._set_update_state("checking", "Checking GitHub for a newer release…")
        self._run_async(
            lambda: updates.check_for_update(),
            self._on_update_checked,
            self._on_update_failed,
        )

    def _on_update_checked(self, check: updates.UpdateCheck) -> None:
        self._update_check = check
        updates.write_state(check)
        if check.error:
            self._set_update_state("error", check.error)
            return
        if check.newer:
            self._set_update_state("available", check.message)
            # visible outside the settings page, which is where the update lives
            self.toast.emit("info", check.message)
            return
        self._set_update_state("uptodate", check.message)

    def _on_update_failed(self, message: str) -> None:
        self._set_update_state("error", message or "The update check failed.")

    @pyqtSlot(float, float)
    def _on_download_progress(self, received: float, total: float) -> None:
        if total:
            self._set_update_state(
                "installing",
                f"Downloading: {human_bytes(received)} of {human_bytes(total)}",
            )

    @pyqtSlot(str)
    def _on_update_line(self, line: str) -> None:
        if line.strip():
            self._update_message = line.strip()
            self.updateStateChanged.emit()

    @pyqtSlot()
    def installUpdate(self) -> None:
        """Download the release package, install it and restart the interface."""
        check = self._update_check
        if check is None or not check.can_install or check.asset is None:
            self.toast.emit("warning", "There is nothing to install right now.")
            return
        if self._update_state in ("installing", "restarting"):
            return
        self._set_update_state("installing", f"Downloading {check.asset.name}…")

        def work() -> updates.InstallResult:
            assert check.asset is not None
            package = updates.download(
                check.asset,
                updates.download_dir(),
                on_progress=lambda received, total: self._on_download_progress(
                    float(received), float(total)
                ),
            )
            self.updateLineReady.emit("Installing the package…")
            return updates.install_release(
                check, package, on_output=self.updateLineReady.emit
            )

        self._run_async(work, self._on_update_installed, self._on_update_failed)

    def _on_update_installed(self, result: updates.InstallResult) -> None:
        if not result.ok:
            self._set_update_state("error", result.message or "The installation failed.")
            self.toast.emit("error", f"Update failed: {result.message}")
            return
        if not result.needs_restart:
            self._set_update_state("uptodate", result.message)
            return
        self._set_update_state("restarting", "Installed. Restarting Ducky Can Shoot…")
        self.toast.emit("info", "Installed. Restarting Ducky Can Shoot…")
        self._restart_after_update()

    def _restart_after_update(self) -> None:
        """Stop the old watcher, start a new instance, and quit this one.

        The background watcher runs the code that was there when it started, so
        it is stopped here; app.aboutToQuit starts a fresh one from the new
        installation, which is what "restart with the watcher included" means.
        """
        if self._restart_pending:
            return
        self._restart_pending = True
        try:
            stopped, message = service.stop()
            log.info("watcher for the restart: %s", message if stopped else "not running")
        except Exception as exc:  # pragma: no cover - a watcher that will not stop
            log.warning("cannot stop the watcher before the restart: %s", exc)
        argv = updates.restart_argv()
        log.info("restarting Ducky Can Shoot: %s", " ".join(argv))
        # Two seconds of quiet: this process has to let go of the session marker
        # before the new one claims it, or the watcher would think no window is
        # open for a moment and could react to the same medium twice.
        command = "sleep 2; exec " + shlex.join(argv)
        try:
            subprocess.Popen(
                ["sh", "-c", command],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:  # pragma: no cover - nothing left to restart with
            self._set_update_state("error", f"Installed, but the restart failed: {exc}")
            self._restart_pending = False
            return
        QTimer.singleShot(300, self._quit_for_restart)

    @staticmethod
    def _quit_for_restart() -> None:
        application = QCoreApplication.instance()
        if application is not None:
            application.quit()

    @pyqtSlot()
    def openReleasePage(self) -> None:
        check = self._update_check
        url = check.release.url if check and check.release else ""
        if not url:
            url = f"{APP_HOMEPAGE}/releases"
        if not QDesktopServices.openUrl(QUrl(url)):
            self.toast.emit("error", f"Could not open {url}")

    @pyqtSlot()
    def resetUpdateState(self) -> None:
        """Back to idle, e.g. after the user dismissed the banner."""
        if self._update_state in ("installing", "restarting"):
            return
        self._update_message = ""
        self._update_state = "idle"
        self.updateStateChanged.emit()

    # ------------------------------------------------------------------
    # asynchronous tests (settings page)
    # ------------------------------------------------------------------
    def _run_async(self, fn, on_done, on_error) -> None:
        runner = AsyncRunner(fn, self)
        runner.done.connect(on_done)
        runner.problem.connect(on_error)
        runner.done.connect(lambda *_: self._drop_runner(runner))
        runner.problem.connect(lambda *_: self._drop_runner(runner))
        self._runners.append(runner)
        runner.start()

    def _drop_runner(self, runner: AsyncRunner) -> None:
        if runner in self._runners:
            self._runners.remove(runner)
        runner.deleteLater()

    @pyqtSlot("QVariantMap", result=bool)
    def testSmtp(self, draft: dict[str, Any] | None = None) -> bool:
        """Test the SMTP server, with the values currently in the form.

        The settings page edits a draft that only reaches the config file on
        Save.  Testing the *stored* settings while the user is looking at
        different values is how "Test SMTP" used to fail and then succeed after
        saving; the draft is what gets tested now, so the answer belongs to what
        is on screen.
        """
        from dcs.core.notify import Notifier

        email = self._email_from_draft(draft or {})
        password = str((draft or {}).get("password") or "") or self.config.smtp_password()
        self.toast.emit("info", "Testing the SMTP connection...")

        def work() -> tuple[bool, str]:
            notifier = Notifier(
                email,
                password=password,
                username=email.username or self.config.smtp_username(),
            )
            return notifier.test_connection()

        self._run_async(
            work,
            lambda result: self.testResult.emit("smtp", bool(result[0]), str(result[1])),
            lambda error: self.testResult.emit("smtp", False, error),
        )
        return True

    def _email_from_draft(self, draft: dict[str, Any]) -> EmailConfig:
        """An EmailConfig with the stored values overlaid by the form values."""
        known = {field_info.name for field_info in fields(EmailConfig)}
        values: dict[str, Any] = {
            key: value for key, value in draft.items() if key in known and key != "password"
        }
        recipients = values.get("to_addrs")
        if isinstance(recipients, str):
            values["to_addrs"] = [
                part.strip() for part in recipients.replace(";", ",").split(",") if part.strip()
            ]
        if not values:
            return self.config.email
        try:
            return replace(self.config.email, **values)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            log.warning("ignoring the SMTP values in the form: %s", exc)
            return self.config.email

    @pyqtSlot(str)
    def testProvider(self, provider_id: str) -> None:
        from dcs.core.upload import build_provider

        self.toast.emit("info", f"Checking {provider_id}...")

        def work() -> tuple[bool, str]:
            provider = build_provider(
                self.config.upload,
                provider_id,
                secrets={
                    "gofile_token": self.config.gofile_token(),
                    "pixeldrain_api_key": self.config.pixeldrain_api_key(),
                },
            )
            ok, detail = provider.availability()
            return ok, detail or f"{provider.label} is reachable"

        self._run_async(
            work,
            lambda result: self.testResult.emit(provider_id, bool(result[0]), str(result[1])),
            lambda error: self.testResult.emit(provider_id, False, error),
        )

    @pyqtSlot()
    def openConfig(self) -> None:
        write_default_config(self.config.source_path or None)
        self.openPath(self.config.source_path or str(write_default_config()))

    # ------------------------------------------------------------------
    # installing the optional ClamAV engine
    #
    # The GUI cannot ask for a password itself, so it hands the job to pkexec
    # (graphical polkit dialog) or to a terminal with sudo inside, and only
    # streams what those print.  Nothing in this path needs the password.
    # ------------------------------------------------------------------
    @pyqtProperty(str, notify=installStateChanged)
    def installState(self) -> str:
        return self._install_state

    @pyqtProperty(bool, notify=installStateChanged)
    def installRunning(self) -> bool:
        return self._install_state == "running"

    @pyqtProperty(QObject, constant=True)
    def installOutput(self) -> TextLinesModel:
        """The streamed installation output, one row per line."""
        return self.install_model

    @pyqtProperty(str, notify=installStateChanged)
    def installText(self) -> str:
        return self.install_model.text

    @pyqtProperty("QVariantMap", notify=installStateChanged)
    def installOutcome(self) -> dict[str, Any]:
        return self._install_outcome

    @pyqtSlot(result="QVariantMap")
    def clamavInstallPlan(self) -> dict[str, Any]:
        """What would be run to install ClamAV, for the confirmation dialog."""
        try:
            plan = build_plan(config=self.config.scan.clamav)
        except Exception as exc:
            log.warning("cannot build an installation plan: %s", exc)
            return {"possible": False, "blocker": str(exc), "steps": [], "commands": []}
        data = plan.to_dict()
        data["manual"] = manual_instructions(plan)
        data["status"] = verify_clamav(self.config)
        return data

    @pyqtSlot()
    def startClamavInstall(self) -> None:
        """Run the plan in the background, streaming the output to the dialog."""
        if self._install_state == "running":
            self.toast.emit("warning", "An installation is already running")
            return
        plan = build_plan(config=self.config.scan.clamav)
        if not plan.possible:
            self.toast.emit("error", plan.blocker or "nothing to install on this system")
            self.install_model.clear()
            self._append_install(
                manual_instructions(plan) if plan.steps else (plan.blocker or "no plan available")
            )
            return

        self.install_model.clear()
        self._install_outcome = {}
        self._append_install(f"# {plan.distro} - {plan.manager.label if plan.manager else ''}")
        self._append_install(f"# {plan.method_label}")
        if plan.script_path:
            self._append_install(f"# script: {plan.script_path}")
        self._append_install("")
        self._set_install_state("running")

        installer = Installer(plan)
        self._installer = installer

        def work() -> None:
            outcome = installer.run(on_output=self._append_install)
            outcome_data = outcome.to_dict()
            outcome_data["verification"] = verify_clamav(self.config)
            self._install_outcome = outcome_data
            self._installer = None
            self._set_install_state("done" if outcome.ok and not outcome.cancelled else "failed")
            if outcome.ok:
                self.toast.emit("info", "ClamAV is installed; re-checking the engine")
            elif outcome.cancelled:
                self.toast.emit("warning", "Installation cancelled")
            else:
                self.toast.emit("error", outcome.error or "the installation did not finish")
            QTimer.singleShot(0, self.refreshEngineStatus)
            self.installFinished.emit(outcome_data)

        self._install_thread = threading.Thread(target=work, daemon=True)
        self._install_thread.start()

    @pyqtSlot()
    def cancelClamavInstall(self) -> None:
        if self._installer is not None:
            self._installer.cancel()
            self._append_install("")
            self._append_install("# cancellation requested")
            self.toast.emit("warning", "Cancelling the installation...")

    @pyqtSlot()
    def verifyClamav(self) -> None:
        """Re-probe ClamAV and refresh the engine list in the interface."""
        self.refreshEngineStatus()
        status = verify_clamav(self.config)
        self._append_install("")
        self._append_install(status.get("line", ""))
        if status.get("warning"):
            self._append_install(f"warning: {status['warning']}")
        self._set_install_state("done" if status.get("available") else self._install_state)

    def _append_install(self, line: str) -> None:
        """Called from the installer thread; the signal queues it to the GUI."""
        self.installLineReady.emit(line)

    def _set_install_state(self, state: str) -> None:
        self._install_state = state
        self.installStateChanged.emit()
