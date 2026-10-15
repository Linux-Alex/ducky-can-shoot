"""Watch for media that are plugged in while Ducky Can Shoot is running.

The watcher is deliberately small and Qt free:

* it polls `lsblk` (through :mod:`dcs.core.device`) at a configurable interval,
  so it needs no udev rules, no D-Bus connection and no root;
* it only reports media that *appear* while it runs, which is what a human means
  by "I just plugged a stick in";
* it never mounts, never scans and never touches a file: deciding what to do with
  the medium belongs to the caller (the interface, or `dcs watch`).

:class:`DeviceWatcher` is the whole mechanism; the four reactions live in
:data:`dcs.core.config.WATCH_MODES`:

===========  ==========================================================
`off`        do not watch at all
`notify`     show a desktop notification with an "Open" button
`open`       open the interface with the new medium selected
`open_run`   open the interface and start the job straight away
===========  ==========================================================

One event is reported per *physical disk*, not per partition: a stick with two
partitions is one thing a human plugged in, and it must not start two jobs.  The
event carries every partition it found and picks the one the interface lists
first as :attr:`DeviceEvent.device`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from dcs.core.config import WATCH_MODES, AppConfig, WatchConfig
from dcs.core.device import BlockDevice, is_removable, list_block_devices
from dcs.core.log import get_logger

log = get_logger("core.watch")

#: short labels for the four modes, in the order they are offered
MODE_LABELS: dict[str, str] = {
    "off": "Do nothing",
    "notify": "Notify me and offer to open Ducky Can Shoot",
    "open": "Open Ducky Can Shoot with the new medium selected",
    "open_run": "Open Ducky Can Shoot and start the job automatically",
}

#: one sentence per mode, used by the interface and by 'dcs watch --help'
MODE_DESCRIPTIONS: dict[str, str] = {
    "off": "Ducky Can Shoot never listens for new media.",
    "notify": "A desktop notification appears with a button that opens Ducky Can Shoot "
    "on the new medium. Nothing is scanned until you say so.",
    "open": "Ducky Can Shoot starts with the new medium already selected. The scan still "
    "waits for you to press Start.",
    "open_run": "Ducky Can Shoot starts and runs the whole job: scan, archive, upload and "
    "email. Without a configured recipient nothing is uploaded, and you get the "
    "report and the link on the result page.",
}

#: how a discovery function reports what it found
DiscoverFn = Callable[[], tuple[Sequence[BlockDevice], Sequence[str]]]


@dataclass(slots=True, frozen=True)
class DeviceEvent:
    """A medium that was plugged in while the watcher was running."""

    #: the physical disk, e.g. /dev/sda (what was plugged in)
    disk: str
    #: the partition DCS would look at first (the interface lists it first)
    device: BlockDevice
    #: every partition found on that disk, in interface order
    partitions: tuple[BlockDevice, ...] = ()
    #: when the watcher noticed it, seconds since the epoch
    inserted_at: float = 0.0

    @property
    def node(self) -> str:
        return self.device.path

    @property
    def mount_path(self) -> str:
        return self.device.mount_path

    @property
    def label(self) -> str:
        return self.device.display_name

    def summary(self) -> str:
        """One line a human can read, used by notifications and logs."""
        where = self.mount_path or "not mounted yet"
        return f"{self.label} ({self.device.size_text}, {where})"

    def to_dict(self) -> dict[str, Any]:
        data = self.device.to_dict()
        data.update(
            {
                "disk": self.disk,
                "node": self.node,
                "mountPath": self.mount_path,
                "displayName": self.label,
                "summary": self.summary(),
                "insertedAt": self.inserted_at,
                "partitions": [part.to_dict() for part in self.partitions],
            }
        )
        return data


def _partition_of(device_path: str, disks: Iterable[str]) -> str:
    """The physical disk a device node belongs to.

    `/dev/sdb1` belongs to `/dev/sdb`, `/dev/nvme0n1p1` to `/dev/nvme0n1` and a
    whole disk belongs to itself.  The split is made against the disk nodes lsblk
    reported, so a name that merely ends in digits cannot be cut in half.
    """
    for disk in disks:
        if device_path == disk:
            return disk
        if device_path.startswith(disk):
            rest = device_path[len(disk) :]
            if rest and (rest[0].isdigit() or (rest[0] == "p" and rest[1:].isdigit())):
                return disk
    return device_path


def default_discover() -> tuple[list[BlockDevice], list[str]]:
    """One lsblk call: the removable filesystems and every disk node."""
    devices = list_block_devices()
    removable = [device for device in devices if is_removable(device)]
    removable.sort(key=lambda device: (not device.mounted, device.name))
    disks = [device.path for device in devices if device.type == "disk"]
    return removable, disks


class DeviceWatcher:
    """Reports media that appear after it started.

    >>> watcher = DeviceWatcher(discover=lambda: ([], []))
    >>> watcher.enabled
    False
    """

    def __init__(
        self,
        config: AppConfig | WatchConfig | None = None,
        *,
        mode: str | None = None,
        interval_s: float | None = None,
        ignore_existing: bool | None = None,
        discover: DiscoverFn | None = None,
    ) -> None:
        if isinstance(config, AppConfig):
            settings: WatchConfig = config.watch
        else:
            settings = config or WatchConfig()
        self.mode = (mode or settings.mode or "off").strip().lower()
        if self.mode not in WATCH_MODES:
            log.warning("unknown watch mode %r, falling back to 'off'", self.mode)
            self.mode = "off"
        self.interval_s = float(settings.interval_s if interval_s is None else interval_s)
        self.ignore_existing = (
            settings.ignore_existing if ignore_existing is None else bool(ignore_existing)
        )
        self._discover: DiscoverFn = discover or default_discover
        self._known: set[str] = set()
        self._primed = False

    # -- state ------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """False when nothing has to run at all (mode 'off')."""
        return self.mode != "off"

    @property
    def known(self) -> frozenset[str]:
        """The disks the watcher has already seen."""
        return frozenset(self._known)

    def prime(self) -> None:
        """Remember what is attached right now, so it is not reported later."""
        try:
            devices, disks = self._discover()
        except Exception as exc:  # pragma: no cover - lsblk is best effort
            log.warning("cannot list the block devices: %s", exc)
            return
        self._known = {_partition_of(device.path, disks) for device in devices}
        self._primed = True
        if self._known:
            log.debug("watcher primed with %d attached medium(s)", len(self._known))

    def poll(self) -> list[DeviceEvent]:
        """List the media that appeared since the previous call."""
        try:
            devices, disks = self._discover()
        except Exception as exc:  # pragma: no cover - lsblk is best effort
            log.warning("cannot list the block devices: %s", exc)
            return []

        if not self._primed:
            # the first look without prime() reports what is already there
            self._primed = True

        by_disk: dict[str, list[BlockDevice]] = {}
        for device in devices:
            by_disk.setdefault(_partition_of(device.path, disks), []).append(device)

        events: list[DeviceEvent] = []
        for disk, group in sorted(by_disk.items()):
            known = disk in self._known if self.ignore_existing else False
            if known:
                continue
            partition = _first_usable(group)
            if partition is None:
                continue
            events.append(
                DeviceEvent(
                    disk=disk,
                    device=partition,
                    partitions=tuple(group),
                    inserted_at=time.time(),
                )
            )
        # a medium that disappeared is forgotten, so plugging it back in reports
        # it again; media that stay plugged in are reported exactly once
        self._known = set(by_disk)
        return events

    def wait(self, stop: threading.Event | None, timeout: float) -> bool:
        """Sleep for *timeout* seconds or until *stop* is set; True when stopped."""
        if stop is None:
            time.sleep(timeout)
            return False
        return stop.wait(timeout)

    def run(
        self,
        on_event: Callable[[DeviceEvent], Any],
        *,
        stop: threading.Event | None = None,
        on_error: Callable[[Exception], Any] | None = None,
        prime: bool | None = None,
    ) -> int:
        """Poll until *stop* is set and hand every new medium to *on_event*.

        Returns the number of events that were reported.  Exceptions raised by
        *on_event* are handed to *on_error* when one is given and abort the loop
        otherwise, so a broken reaction is never silently swallowed.
        """
        if prime is None:
            prime = self.ignore_existing
        if prime:
            self.prime()
        reported = 0
        while not (stop is not None and stop.is_set()):
            try:
                events = self.poll()
            except Exception as exc:  # pragma: no cover - poll() swallows its own
                if on_error is not None:
                    on_error(exc)
                    events = []
                else:
                    raise
            for event in events:
                log.info("new medium: %s (%s)", event.label, event.disk)
                try:
                    on_event(event)
                except Exception as exc:
                    if on_error is None:
                        raise
                    on_error(exc)
                reported += 1
            if self.wait(stop, max(0.1, self.interval_s)):
                break
        return reported


def _first_usable(partitions: Sequence[BlockDevice]) -> BlockDevice | None:
    """The partition the interface would list first for this disk."""
    for partition in partitions:
        if partition.scannable and partition.mounted:
            return partition
    for partition in partitions:
        if partition.scannable:
            return partition
    return None


def watch_interval(config: AppConfig | WatchConfig | None = None) -> float:
    settings = config.watch if isinstance(config, AppConfig) else (config or WatchConfig())
    try:
        value = float(settings.interval_s)
    except (TypeError, ValueError):  # pragma: no cover - config coerces already
        return 3.0
    return max(0.5, value)


__all__ = [
    "MODE_DESCRIPTIONS",
    "MODE_LABELS",
    "DeviceEvent",
    "DeviceWatcher",
    "default_discover",
    "watch_interval",
]
