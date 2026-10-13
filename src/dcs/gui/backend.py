"""The object QML talks to.

Everything the user interface can do goes through this facade: it owns the
configuration, the models, the worker thread and the small desktop
integrations (clipboard, file manager).  Keeping it in one place means the QML
files stay declarative and contain no logic worth testing.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from PyQt6.QtCore import (
    QObject,
    QThread,
    QTimer,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QClipboard, QDesktopServices, QGuiApplication

from dcs.core.config import AppConfig, load_config, write_default_config
from dcs.core.device import (
    list_removable_devices,
    mount_device,
    unmount_device,
)
from dcs.core.events import STAGE_ORDER
from dcs.core.installer import Installer, build_plan, manual_instructions
from dcs.core.installer import verify as verify_clamav
from dcs.core.log import get_logger, log_ring
from dcs.core.pipeline import JobRequest
from dcs.core.scanner import Scanner
from dcs.core.upload import provider_catalog
from dcs.gui.models import (
    DevicesModel,
    FindingsModel,
    LogModel,
    StagesModel,
    ThroughputModel,
)
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
    installTextChanged = pyqtSignal()
    installStateChanged = pyqtSignal()
    installFinished = pyqtSignal(object)

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

        self._state = IDLE
        self._busy = False
        self._progress: dict[str, Any] = {}
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
        self._install_lines: list[str] = []
        self._install_state = "idle"
        self._install_outcome: dict[str, Any] = {}
        self._install_dirty = False
        # one update per 100 ms keeps a fast moving apt log cheap for QML
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(100)
        self._install_timer.timeout.connect(self._flush_install_text)
        self._install_timer.start()

        log_ring().add_listener(self._on_log_record)
        self.refreshDevices()
        self.refreshEngineStatus()
        if poll_devices:
            self._poll = QTimer(self)
            self._poll.setInterval(3000)
            self._poll.timeout.connect(self.refreshDevices)
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

    @pyqtProperty(str, notify=stateChanged)
    def liveStageTitle(self) -> str:
        return self._live("title", "Waiting")

    @pyqtProperty(str, notify=stateChanged)
    def liveCurrent(self) -> str:
        return self._live("current")

    @pyqtProperty(str, notify=stateChanged)
    def liveRateText(self) -> str:
        return self._live("rateText", "-")

    @pyqtProperty(str, notify=stateChanged)
    def liveEtaText(self) -> str:
        return self._live("etaText", "--:--")

    @pyqtProperty(str, notify=stateChanged)
    def liveElapsedText(self) -> str:
        return self._live("elapsedText", "00:00")

    @pyqtProperty(str, notify=stateChanged)
    def liveFilesText(self) -> str:
        processed = self._progress.get("processed") or 0
        total = self._progress.get("total") or 0
        if not total:
            # stages such as the upload have no file count, only bytes
            return f"{processed:,}" if processed else "-"
        return f"{processed:,} / {total:,}"

    @pyqtProperty(str, notify=stateChanged)
    def liveBytesText(self) -> str:
        done = self._live("bytesProcessedText", "0 B")
        total = self._live("bytesTotalText")
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

    @pyqtSlot("QVariantMap")
    def saveSettings(self, values: dict[str, Any]) -> None:
        """Apply the settings page to the config object and persist it."""
        self.config.merge(values)
        try:
            path = self.config.save()
            self.toast.emit("info", f"Settings saved to {path}")
        except OSError as exc:
            self.toast.emit("error", f"Could not save the settings: {exc}")
        self.refreshEngineStatus()
        self.settingsChanged.emit()

    @pyqtSlot()
    def reloadSettings(self) -> None:
        self.config = load_config(self.config.source_path or None)
        self.refreshEngineStatus()
        self.settingsChanged.emit()

    @pyqtSlot(result=str)
    def validationProblems(self) -> str:
        return "\n".join(self.config.validation_problems())

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

    @pyqtSlot(object)
    def _on_progress(self, payload: Any) -> None:
        if isinstance(payload, dict):
            self._progress = payload
            self.stages_model.apply_progress(payload)
            self.throughput_model.append(payload)
            index = 0
            for position, stage in enumerate(STAGE_ORDER):
                if str(stage) == str(payload.get("stage", "")):
                    index = position
                    break
            self._overall = (index + float(payload.get("percent") or 0.0)) / len(STAGE_ORDER)
            self.stateChanged.emit()

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

    @pyqtSlot()
    def testSmtp(self) -> None:
        from dcs.core.notify import Notifier

        config = self.config.email
        self.toast.emit("info", "Testing the SMTP connection...")

        def work() -> tuple[bool, str]:
            notifier = Notifier(
                config,
                password=self.config.smtp_password(),
                username=self.config.smtp_username(),
            )
            return notifier.test_connection()

        self._run_async(
            work,
            lambda result: self.testResult.emit("smtp", bool(result[0]), str(result[1])),
            lambda error: self.testResult.emit("smtp", False, error),
        )

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

    @pyqtProperty(str, notify=installTextChanged)
    def installText(self) -> str:
        return "\n".join(self._install_lines)

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
            self._install_lines = []
            self._append_install(
                manual_instructions(plan) if plan.steps else (plan.blocker or "no plan available")
            )
            return

        self._install_lines = []
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
        self._install_lines.append(line)
        if len(self._install_lines) > 4000:
            del self._install_lines[:500]
        self._install_dirty = True

    def _flush_install_text(self) -> None:
        if self._install_dirty:
            self._install_dirty = False
            self.installTextChanged.emit()

    def _set_install_state(self, state: str) -> None:
        self._install_state = state
        self._install_dirty = True
        self.installStateChanged.emit()
        self.installTextChanged.emit()
