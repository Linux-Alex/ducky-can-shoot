"""The USB watcher: which media count as "just plugged in" and what is reported.

The watcher is the part of the feature that decides when Ducky Can Shoot reacts
at all, so these tests cover the rules rather than the plumbing:

* one event per physical disk, never one per partition (a two partition stick
  must not start two jobs);
* a medium that was already attached when the watcher started is not news;
* unplugging and plugging the same stick back in *is* news;
* the loop stops when it is told to, and a broken reaction is reported instead
  of silently swallowed.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from dcs.core.config import AppConfig, WatchConfig
from dcs.core.device import BlockDevice
from dcs.core.watch import (
    MODE_DESCRIPTIONS,
    MODE_LABELS,
    DeviceEvent,
    DeviceWatcher,
    default_discover,
    watch_interval,
)


def partition(path: str, **overrides: Any) -> BlockDevice:
    """One partition as lsblk reports it for a plugged in stick."""
    fields: dict[str, Any] = {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "type": "part",
        "size": 16_000_000_000,
        "fstype": "vfat",
        "label": "USB",
        "transport": "usb",
        "removable": True,
        "mountpoints": [],
    }
    fields.update(overrides)
    return BlockDevice(**fields)


def disk(path: str, **overrides: Any) -> BlockDevice:
    fields: dict[str, Any] = {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "type": "disk",
        "size": 16_000_000_000,
        "transport": "usb",
        "removable": True,
    }
    fields.update(overrides)
    return BlockDevice(**fields)


class FakeMedium:
    """A stand-in for lsblk: whatever is in *devices* is attached."""

    def __init__(self, disks: list[str] | None = None) -> None:
        self.devices: list[BlockDevice] = []
        self.disks = disks if disks is not None else ["/dev/sda", "/dev/sdb", "/dev/nvme0n1"]
        self.calls = 0

    def __call__(self) -> tuple[list[BlockDevice], list[str]]:
        self.calls += 1
        return list(self.devices), list(self.disks)


def watcher_for(medium: FakeMedium, **kwargs: Any) -> DeviceWatcher:
    return DeviceWatcher(discover=medium, **kwargs)


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


def test_only_the_off_mode_disables_the_watcher() -> None:
    assert DeviceWatcher(mode="off").enabled is False
    for mode in ("notify", "open", "open_run"):
        assert DeviceWatcher(mode=mode).enabled is True


def test_an_unknown_mode_falls_back_to_off() -> None:
    assert DeviceWatcher(mode="open-everything").mode == "off"


def test_every_mode_has_a_label_and_a_sentence() -> None:
    from dcs.core.config import WATCH_MODES

    for mode in WATCH_MODES:
        assert MODE_LABELS[mode]
        assert MODE_DESCRIPTIONS[mode]
    assert set(MODE_LABELS) == set(WATCH_MODES)


def test_the_mode_comes_from_the_configuration() -> None:
    config = AppConfig()
    config.watch.mode = "open_run"
    assert DeviceWatcher(config).mode == "open_run"
    assert DeviceWatcher(WatchConfig(mode="notify")).mode == "notify"


def test_the_interval_never_goes_below_half_a_second() -> None:
    assert watch_interval(WatchConfig(interval_s=0.01)) == 0.5
    assert watch_interval(WatchConfig(interval_s=12)) == 12.0


# ---------------------------------------------------------------------------
# which media are news
# ---------------------------------------------------------------------------


def test_a_medium_attached_before_the_first_poll_is_not_news() -> None:
    medium = FakeMedium()
    medium.devices = [partition("/dev/sda1", mountpoints=["/media/u/USB"])]
    watcher = watcher_for(medium, mode="notify")
    watcher.prime()

    assert watcher.poll() == []
    assert medium.calls == 2


def test_a_newly_plugged_medium_is_reported_once() -> None:
    medium = FakeMedium()
    watcher = watcher_for(medium, mode="notify")
    watcher.prime()

    medium.devices = [partition("/dev/sdb1", label="STICK", mountpoints=["/media/u/STICK"])]
    events = watcher.poll()
    assert len(events) == 1
    event = events[0]
    assert event.disk == "/dev/sdb"
    assert event.node == "/dev/sdb1"
    assert event.label == "STICK"
    assert event.mount_path == "/media/u/STICK"
    assert "STICK" in event.summary() and "not mounted" not in event.summary()

    assert watcher.poll() == [], "a medium that stays plugged in is only news once"


def test_plugging_the_same_medium_back_in_is_news_again() -> None:
    medium = FakeMedium()
    watcher = watcher_for(medium, mode="notify")
    watcher.prime()

    medium.devices = [partition("/dev/sdb1")]
    assert len(watcher.poll()) == 1
    medium.devices = []
    assert watcher.poll() == []
    medium.devices = [partition("/dev/sdb1")]
    assert len(watcher.poll()) == 1


def test_two_partitions_are_one_medium() -> None:
    medium = FakeMedium()
    watcher = watcher_for(medium, mode="open_run")
    watcher.prime()

    medium.devices = [
        partition("/dev/sda1", label="BOOT"),
        partition("/dev/sda2", label="DATA", mountpoints=["/media/u/DATA"]),
    ]
    events = watcher.poll()
    assert len(events) == 1, "one stick, one job"
    event = events[0]
    assert event.disk == "/dev/sda"
    assert [part.path for part in event.partitions] == ["/dev/sda1", "/dev/sda2"]
    # the mounted partition is the one a job should use
    assert event.node == "/dev/sda2"


def test_a_whole_disk_without_a_partition_table_is_its_own_medium() -> None:
    medium = FakeMedium()
    watcher = watcher_for(medium, mode="notify")
    watcher.prime()

    medium.devices = [disk("/dev/sdb", fstype="vfat", mountpoints=["/media/u/RAW"])]
    events = watcher.poll()
    assert [event.disk for event in events] == ["/dev/sdb"]
    assert events[0].node == "/dev/sdb"


def test_nvme_partitions_belong_to_their_disk() -> None:
    medium = FakeMedium()
    watcher = watcher_for(medium, mode="notify")
    watcher.prime()

    medium.devices = [partition("/dev/nvme0n1p1", transport="usb", fstype="ext4")]
    events = watcher.poll()
    assert [event.disk for event in events] == ["/dev/nvme0n1"]


def test_everything_attached_can_be_reported_on_demand() -> None:
    medium = FakeMedium()
    medium.devices = [partition("/dev/sda1"), partition("/dev/sdb1")]
    watcher = watcher_for(medium, mode="notify", ignore_existing=False)

    events = watcher.poll()
    assert sorted(event.disk for event in events) == ["/dev/sda", "/dev/sdb"]


def test_a_device_that_cannot_be_walked_is_ignored() -> None:
    medium = FakeMedium()
    watcher = watcher_for(medium, mode="notify")
    watcher.prime()
    medium.devices = [partition("/dev/sdb1", fstype="", type="disk")]
    assert watcher.poll() == []


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


class BoundedLoop(DeviceWatcher):
    """A watcher whose loop cannot spin for ever in a test.

    run() waits between two polls; counting the waits is what makes "the loop
    with no stop event" testable without a failing test hanging the suite.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.waits = 0

    def wait(self, stop: threading.Event | None, timeout: float) -> bool:
        self.waits += 1
        if self.waits > 20:  # pragma: no cover - only on a broken loop
            raise KeyboardInterrupt("the loop did not stop")
        return False


def test_run_reports_events_and_stops_when_asked() -> None:
    medium = FakeMedium()
    watcher = watcher_for(medium, mode="notify", interval_s=0.01)
    watcher.prime()  # nothing is attached yet, so the next medium is news
    stop = threading.Event()
    seen: list[DeviceEvent] = []

    def react(event: DeviceEvent) -> None:
        seen.append(event)
        stop.set()

    medium.devices = [partition("/dev/sdb1")]
    reported = watcher.run(react, stop=stop, prime=False)
    assert reported == 1
    assert seen[0].node == "/dev/sdb1"


def test_run_reports_a_broken_reaction() -> None:
    medium = FakeMedium()
    medium.devices = [partition("/dev/sdb1")]
    watcher = watcher_for(medium, mode="notify", ignore_existing=False, interval_s=0.01)
    stop = threading.Event()
    problems: list[Exception] = []

    def react(event: DeviceEvent) -> None:
        stop.set()
        raise RuntimeError("the notification server is gone")

    watcher.run(react, stop=stop, on_error=problems.append)
    assert problems and "notification server" in str(problems[0])


def test_run_without_a_stop_event_listens_until_the_reaction_stops_it() -> None:
    medium = FakeMedium()
    watcher = BoundedLoop(discover=medium, mode="notify", interval_s=0.01)
    seen: list[DeviceEvent] = []

    def react(event: DeviceEvent) -> None:
        seen.append(event)
        raise KeyboardInterrupt  # the reaction decides when to stop

    medium.devices = [partition("/dev/sdb1")]
    with pytest.raises(KeyboardInterrupt):
        watcher.run(react, prime=False)

    assert [event.node for event in seen] == ["/dev/sdb1"]
    assert watcher.waits == 0, "the reaction ended the loop before the first wait"


def test_run_primes_by_default_so_attached_media_are_ignored() -> None:
    medium = FakeMedium()
    medium.devices = [partition("/dev/sdb1")]
    watcher = BoundedLoop(discover=medium, mode="notify", interval_s=0.01)
    seen: list[DeviceEvent] = []

    def react(event: DeviceEvent) -> None:  # pragma: no cover - must not run
        seen.append(event)

    with pytest.raises(KeyboardInterrupt):
        watcher.run(react)
    assert seen == []


def test_a_discovery_failure_does_not_kill_the_loop() -> None:
    calls = {"n": 0}

    def broken() -> tuple[list[BlockDevice], list[str]]:
        calls["n"] += 1
        raise OSError("lsblk is not installed")

    watcher = DeviceWatcher(discover=broken, mode="notify", interval_s=0.01)
    assert watcher.poll() == []
    assert calls["n"] == 1
    watcher.prime()  # must not raise either


def test_the_event_carries_everything_the_interface_needs() -> None:
    medium = FakeMedium()
    watcher = watcher_for(medium, mode="notify", ignore_existing=False)
    medium.devices = [partition("/dev/sdb1", mountpoints=["/media/u/USB"])]
    payload = watcher.poll()[0].to_dict()

    assert payload["node"] == "/dev/sdb1"
    assert payload["disk"] == "/dev/sdb"
    assert payload["displayName"] == "USB"
    assert payload["mountPath"] == "/media/u/USB"
    assert payload["sizeText"]
    assert payload["summary"].startswith("USB")
    assert isinstance(payload["partitions"], list)
    assert payload["insertedAt"] > 0


def test_default_discover_works_on_this_machine() -> None:
    devices, disks = default_discover()
    for device in devices:
        assert device.path
    for disk_node in disks:
        assert disk_node.startswith("/dev/")
