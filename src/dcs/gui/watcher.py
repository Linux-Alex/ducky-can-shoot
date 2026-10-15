"""Poll for newly plugged media on the Qt event loop.

:class:`dcs.core.watch.DeviceWatcher` does the work without Qt; this class only
turns its events into a signal the interface can bind to, and follows the
configuration while the window is open (changing the mode in the settings page
starts or stops the timer without a restart).

The watcher polls instead of listening on udev on purpose: no extra dependency,
no root, no D-Bus, and one lsblk call every three seconds costs nothing.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

from dcs.core.config import AppConfig
from dcs.core.log import get_logger
from dcs.core.watch import DeviceEvent, DeviceWatcher

log = get_logger("gui.watcher")


class UsbWatcher(QObject):
    """Reports media that appear while the interface is open."""

    #: one device, as dcs.core.watch.DeviceEvent.to_dict()
    inserted = pyqtSignal("QVariantMap")

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._watcher = DeviceWatcher(config)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self.poll)
        self.apply_config(config)

    # -- state ------------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._watcher.mode

    @property
    def active(self) -> bool:
        """True while the timer is running (mode is not 'off')."""
        return self._timer.isActive()

    @property
    def interval_ms(self) -> int:
        return self._timer.interval()

    # -- control ----------------------------------------------------------
    def apply_config(self, config: AppConfig) -> None:
        """Follow the configuration: restart the watcher when it changed."""
        self._watcher = DeviceWatcher(config)
        self._timer.setInterval(int(max(0.5, config.watch.interval_s) * 1000))
        if not self._watcher.enabled:
            if self._timer.isActive():
                log.info("USB watching stopped (mode off)")
            self._timer.stop()
            return
        # whatever is attached right now is not news
        self._watcher.prime()
        self._timer.start()
        log.info(
            "watching for new media every %.1fs (mode %s)",
            self._timer.interval() / 1000.0,
            self._watcher.mode,
        )

    def poll(self) -> list[DeviceEvent]:
        """Check once and emit one signal per new medium."""
        events = self._watcher.poll()
        for event in events:
            log.info("new medium: %s (%s)", event.label, event.disk)
            self.inserted.emit(event.to_dict())
        return events

    def stop(self) -> None:
        self._timer.stop()


def device_summary(payload: dict[str, Any]) -> str:
    """One line describing a device payload (used by the toast and the email)."""
    name = str(payload.get("displayName") or payload.get("node") or "a USB medium")
    size = str(payload.get("sizeText") or "")
    where = str(payload.get("mountPath") or "")
    parts = [part for part in (size, where) if part]
    return f"{name}" + (f" ({', '.join(parts)})" if parts else "")


__all__ = ["UsbWatcher", "device_summary"]
