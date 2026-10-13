"""Tests for removable media discovery: lsblk parsing and device lookups.

No test in this module ever runs the real lsblk: the canned payload below is
served by monkeypatching dcs.core.device._run_lsblk.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from dcs.core import device as device_module
from dcs.core.device import (
    LSBLK_COLUMNS,
    BlockDevice,
    device_for_path,
    find_device,
    is_removable_mount,
    list_block_devices,
    list_removable_devices,
)
from dcs.core.util import human_bytes

#: A machine with an internal NVMe disk, two USB sticks (one mounted, one not),
#: a raw unformatted USB disk, two loop devices and an optical drive.
_CANNED_LSBLK: dict[str, Any] = {
    "blockdevices": [
        {
            "name": "nvme0n1",
            "path": "/dev/nvme0n1",
            "type": "disk",
            "size": 1_000_204_886_016,
            "fstype": None,
            "label": None,
            "uuid": None,
            "mountpoints": None,
            "tran": "nvme",
            "rm": False,
            "hotplug": False,
            "model": "Samsung SSD 980 PRO 1TB",
            "vendor": None,
            "ro": False,
            "fsver": None,
            "children": [
                {
                    "name": "nvme0n1p1",
                    "path": "/dev/nvme0n1p1",
                    "type": "part",
                    "size": 536_870_912_000,
                    "fstype": "ext4",
                    "label": "root",
                    "uuid": "11111111-1111-1111-1111-111111111111",
                    "mountpoints": ["/"],
                    "tran": "nvme",
                    "rm": False,
                    "hotplug": False,
                    "model": None,
                    "vendor": None,
                    "ro": False,
                    "fsver": "1.0",
                },
                {
                    "name": "nvme0n1p2",
                    "path": "/dev/nvme0n1p2",
                    "type": "part",
                    "size": 429_496_729_600,
                    "fstype": "ext4",
                    "label": "home",
                    "uuid": "22222222-2222-2222-2222-222222222222",
                    "mountpoints": ["/home"],
                    "tran": "nvme",
                    "rm": False,
                    "hotplug": False,
                    "model": None,
                    "vendor": None,
                    "ro": False,
                    "fsver": "1.0",
                },
                {
                    "name": "nvme0n1p3",
                    "path": "/dev/nvme0n1p3",
                    "type": "part",
                    "size": 33_554_432_000,
                    "fstype": None,
                    "label": None,
                    "uuid": None,
                    "mountpoints": None,
                    "tran": "nvme",
                    "rm": False,
                    "hotplug": False,
                    "model": None,
                    "vendor": None,
                    "ro": False,
                    "fsver": None,
                    "children": [
                        {
                            "name": "vg0-root",
                            "path": "/dev/mapper/vg0-root",
                            "type": "lvm",
                            "size": 107_374_182_400,
                            "fstype": "ext4",
                            "label": None,
                            "uuid": "33333333-3333-3333-3333-333333333333",
                            "mountpoints": ["/var"],
                            "tran": "nvme",
                            "rm": False,
                            "hotplug": False,
                            "model": None,
                            "vendor": None,
                            "ro": False,
                            "fsver": "1.0",
                        }
                    ],
                },
            ],
        },
        {
            "name": "sdb",
            "path": "/dev/sdb",
            "type": "disk",
            "size": 16_000_000_000,
            "fstype": None,
            "label": None,
            "uuid": None,
            "mountpoints": None,
            "tran": "usb",
            "rm": False,
            "hotplug": False,
            "model": " Ultra Fit ",
            "vendor": "SanDisk ",
            "ro": False,
            "fsver": None,
            "children": [
                {
                    "name": "sdb1",
                    "path": "/dev/sdb1",
                    "type": "part",
                    "size": 15_999_000_000,
                    "fstype": "vfat",
                    "label": "USBSTICK",
                    "uuid": "ABCD-1234",
                    "mountpoints": ["/media/usb"],
                    "tran": "usb",
                    "rm": False,
                    "hotplug": False,
                    "model": None,
                    "vendor": None,
                    "ro": False,
                    "fsver": "FAT32",
                },
                {
                    "name": "sdb2",
                    "path": "/dev/sdb2",
                    "type": "part",
                    "size": 16_000_000,
                    "fstype": "ext4",
                    "label": "SUBDIR",
                    "uuid": "IJKL-9012",
                    "mountpoints": ["/media/usb/sub"],
                    "tran": "usb",
                    "rm": False,
                    "hotplug": False,
                    "model": None,
                    "vendor": None,
                    "ro": False,
                    "fsver": "1.0",
                },
            ],
        },
        {
            "name": "sdc",
            "path": "/dev/sdc",
            "type": "disk",
            "size": 32_000_000_000,
            "fstype": None,
            "label": None,
            "uuid": None,
            "mountpoints": None,
            "tran": "usb",
            "rm": False,
            "hotplug": True,
            "model": "Backup+",
            "vendor": "Kingston",
            "ro": False,
            "fsver": None,
            "children": [
                {
                    "name": "sdc1",
                    "path": "/dev/sdc1",
                    "type": "part",
                    "size": 31_999_000_000,
                    "fstype": "vfat",
                    "label": "BACKUP",
                    "uuid": "EFGH-5678",
                    "mountpoints": None,
                    "tran": "usb",
                    "rm": False,
                    "hotplug": True,
                    "model": None,
                    "vendor": None,
                    "ro": False,
                    "fsver": "FAT32",
                }
            ],
        },
        {
            # a raw stick without any filesystem: removable but not scannable
            "name": "sdd",
            "type": "disk",
            "size": 8_000_000_000,
            "fstype": None,
            "label": None,
            "uuid": None,
            "mountpoints": None,
            "tran": "usb",
            "rm": True,
            "hotplug": True,
            "model": "DataTraveler",
            "vendor": "Kingston",
            "ro": False,
            "fsver": None,
        },
        {
            # loop device backed by a squashfs snap: never a user medium
            "name": "loop0",
            "path": "/dev/loop0",
            "type": "loop",
            "size": 130_000_000,
            "fstype": "squashfs",
            "label": None,
            "uuid": None,
            "mountpoints": ["/snap/core/1"],
            "tran": None,
            "rm": False,
            "hotplug": False,
            "model": None,
            "vendor": None,
            "ro": True,
            "fsver": "4.0",
        },
        {
            # loop device backed by a removable image
            "name": "loop1",
            "path": "/dev/loop1",
            "type": "loop",
            "size": 4_000_000_000,
            "fstype": "vfat",
            "label": "IMAGE",
            "uuid": "MNOP-3456",
            "mountpoints": None,
            "tran": None,
            "rm": True,
            "hotplug": False,
            "model": None,
            "vendor": None,
            "ro": False,
            "fsver": "FAT32",
        },
        {
            "name": "sr0",
            "path": "/dev/sr0",
            "type": "rom",
            "size": 700_000_000,
            "fstype": "iso9660",
            "label": "DCS_TEST",
            "uuid": "2026-01-13-19-15-30-00",
            # lsblk emits a bare string here on some versions
            "mountpoints": "/media/cdrom",
            "tran": "sata",
            "rm": False,
            "hotplug": False,
            "model": "DVD-RW DA8AESH",
            "vendor": "PLDS",
            "ro": True,
            "fsver": "Joliet Extension",
        },
    ]
}


def canned_lsblk() -> dict[str, Any]:
    """A fresh deep copy of the canned lsblk payload for one test."""
    return copy.deepcopy(_CANNED_LSBLK)


def canned_node(name: str) -> dict[str, Any]:
    """A fresh deep copy of one canned node, looked up recursively by name."""
    stack = list(_CANNED_LSBLK["blockdevices"])
    while stack:
        node = stack.pop(0)
        if node["name"] == name:
            return copy.deepcopy(node)
        stack.extend(node.get("children") or [])
    raise AssertionError(f"no canned node named {name}")


@pytest.fixture
def lsblk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Serve the canned payload instead of running the real lsblk."""
    payload = canned_lsblk()
    monkeypatch.setattr(device_module, "_run_lsblk", lambda: payload)
    return payload


def device_named(name: str) -> BlockDevice:
    """Parse the canned payload and return one device by name."""
    by_name = {device.name: device for device in list_block_devices(include_loop=True)}
    return by_name[name]


# ---------------------------------------------------------------------------
# lsblk contract
# ---------------------------------------------------------------------------


def test_lsblk_columns_cover_every_parsed_field() -> None:
    columns = LSBLK_COLUMNS.split(",")
    assert len(columns) == len(set(columns))
    for field in (
        "PATH",
        "TYPE",
        "SIZE",
        "FSTYPE",
        "LABEL",
        "UUID",
        "MOUNTPOINTS",
        "TRAN",
        "RM",
        "HOTPLUG",
        "MODEL",
        "VENDOR",
        "RO",
        "FSVER",
    ):
        assert field in columns
    assert "NAME" in columns
    assert columns[0] == "NAME"


def test_parsed_device_fields_match_the_canned_payload(lsblk: dict[str, Any]) -> None:
    sdb1 = device_named("sdb1")
    assert sdb1.path == "/dev/sdb1"
    assert sdb1.type == "part"
    assert sdb1.size == 15_999_000_000
    assert sdb1.fstype == "vfat"
    assert sdb1.fsver == "FAT32"
    assert sdb1.label == "USBSTICK"
    assert sdb1.uuid == "ABCD-1234"
    assert sdb1.mountpoints == ["/media/usb"]
    assert sdb1.transport == "usb"
    assert sdb1.removable is False  # this enclosure does not set the rm bit
    assert sdb1.hotplug is False
    assert sdb1.mounted is True
    assert sdb1.scannable is True
    assert sdb1.display_name == "USBSTICK"

    sdb = device_named("sdb")
    assert sdb.model == "Ultra Fit"  # model and vendor are stripped
    assert sdb.vendor == "SanDisk"
    assert sdb.fstype == ""
    assert sdb.mountpoints == []
    assert sdb.mounted is False
    assert sdb.scannable is False

    raw = device_named("sdd")
    assert raw.path == "/dev/sdd"  # PATH is missing in the payload, so it is derived
    assert raw.removable is True
    assert raw.hotplug is True
    assert raw.fstype == ""
    assert raw.scannable is False

    sr0 = device_named("sr0")
    assert sr0.mountpoints == ["/media/cdrom"]
    assert sr0.mounted is True
    assert sr0.read_only is True
    assert sr0.scannable is True


def test_list_block_devices_flattens_children(lsblk: dict[str, Any]) -> None:
    assert [device.name for device in list_block_devices()] == [
        "nvme0n1",
        "nvme0n1p1",
        "nvme0n1p2",
        "nvme0n1p3",
        "vg0-root",
        "sdb",
        "sdb1",
        "sdb2",
        "sdc",
        "sdc1",
        "sdd",
        "sr0",
    ]
    assert [device.name for device in list_block_devices(include_loop=True)] == [
        "nvme0n1",
        "nvme0n1p1",
        "nvme0n1p2",
        "nvme0n1p3",
        "vg0-root",
        "sdb",
        "sdb1",
        "sdb2",
        "sdc",
        "sdc1",
        "sdd",
        "loop0",
        "loop1",
        "sr0",
    ]
    # vg0-root sits two levels below nvme0n1: recursion, not just one level
    assert device_named("vg0-root").mount_path == "/var"
    assert device_named("nvme0n1p1").mount_path == "/"


def test_list_removable_devices_selects_usb_media_and_sorts(lsblk: dict[str, Any]) -> None:
    devices = list_removable_devices()
    # sdb1 and sdb2 are mounted and come first, then the unmounted sdc1
    assert [device.name for device in devices] == ["sdb1", "sdb2", "sdc1"]
    assert [device.mounted for device in devices] == [True, True, False]
    assert [device.transport for device in devices] == ["usb", "usb", "usb"]
    names = {device.name for device in devices}
    assert "nvme0n1p1" not in names  # internal, not removable, transport nvme
    assert "nvme0n1p2" not in names  # internal, not removable, transport nvme
    assert "sdb" not in names  # a disk node without a filesystem is not scannable
    assert "sdd" not in names  # removable and hotplug, but no filesystem
    assert "sr0" not in names  # scannable iso9660, but sata and not removable
    assert "loop0" not in names  # loop devices are off unless asked for
    assert "loop1" not in names


def test_include_loop_only_adds_loop_devices(lsblk: dict[str, Any]) -> None:
    without_loops = list_removable_devices()
    with_loops = list_removable_devices(include_loop=True)
    assert [device.name for device in with_loops] == ["sdb1", "sdb2", "loop1", "sdc1"]
    added = [device for device in with_loops if device.name not in {d.name for d in without_loops}]
    assert [device.name for device in added] == ["loop1"]
    assert all(device.type == "loop" for device in added)
    assert [device.name for device in with_loops if device.type != "loop"] == [
        "sdb1",
        "sdb2",
        "sdc1",
    ]


def test_devices_with_empty_lsblk_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_module, "_run_lsblk", lambda: {})
    assert list_block_devices() == []
    assert list_block_devices(include_loop=True) == []
    assert list_removable_devices() == []
    assert list_removable_devices(include_loop=True) == []
    assert find_device("sdb1") is None
    assert find_device("/dev/sdb1") is None
    assert device_for_path("/media/usb") is None
    assert is_removable_mount("/media/usb") is False


# ---------------------------------------------------------------------------
# BlockDevice
# ---------------------------------------------------------------------------


def test_block_device_mount_helpers() -> None:
    mounted = BlockDevice(
        name="sdb1",
        path="/dev/sdb1",
        type="part",
        fstype="vfat",
        mountpoints=["/media/usb", "/media/usb-1"],
    )
    assert mounted.mounted is True
    assert mounted.mount_path == "/media/usb"

    unmounted = BlockDevice(name="sdc1", path="/dev/sdc1", type="part", fstype="vfat")
    assert unmounted.mountpoints == []
    assert unmounted.mounted is False
    assert unmounted.mount_path == ""


def test_block_device_display_name_prefers_label_then_model_then_name() -> None:
    assert (
        BlockDevice(name="sdb1", path="/dev/sdb1", label="USBSTICK", model="Ultra Fit").display_name
        == "USBSTICK"
    )
    assert BlockDevice(name="sdb", path="/dev/sdb", model="Ultra Fit").display_name == "Ultra Fit"
    assert BlockDevice(name="sdd", path="/dev/sdd").display_name == "sdd"


def test_block_device_size_text_uses_human_bytes() -> None:
    big = BlockDevice(name="sdb", path="/dev/sdb", size=16_000_000_000)
    assert big.size_text == human_bytes(16_000_000_000) == "14.9 GiB"
    assert BlockDevice(name="x", path="/dev/x", size=0).size_text == "0 B"
    assert BlockDevice(name="x", path="/dev/x", size=2048).size_text == "2.0 KiB"


@pytest.mark.parametrize(
    ("node_type", "fstype", "expected"),
    [
        ("part", "vfat", True),
        ("part", "VFAT", True),
        ("part", "ext4", True),
        ("part", "ext2", True),
        ("part", "ext4dev", True),
        ("part", "ntfs", True),
        ("disk", "ntfs", True),
        ("disk", "exfat", True),
        ("loop", "vfat", True),
        ("rom", "iso9660", True),
        ("part", "", False),
        ("disk", "", False),
        ("part", "swap", False),
        ("part", "squashfs", False),
        ("part", "crypto_LUKS", False),
        ("", "ext4", False),
        ("lvm", "ext4", False),
        ("crypt", "ext4", False),
    ],
)
def test_block_device_scannable(node_type: str, fstype: str, expected: bool) -> None:
    device = BlockDevice(name="node", path="/dev/node", type=node_type, fstype=fstype)
    assert device.scannable is expected


def test_block_device_to_dict_has_exact_keys_and_values() -> None:
    device = BlockDevice(
        name="sdb1",
        path="/dev/sdb1",
        type="part",
        size=16_000_000_000,
        fstype="vfat",
        label="USBSTICK",
        uuid="ABCD-1234",
        mountpoints=["/media/usb"],
        transport="usb",
        removable=False,
        hotplug=False,
        model="Ultra Fit",
        vendor="SanDisk",
        read_only=True,
        fsver="FAT32",
    )
    data = device.to_dict()
    assert set(data) == {
        "name",
        "path",
        "type",
        "size",
        "sizeText",
        "fstype",
        "fsver",
        "label",
        "uuid",
        "mountpoints",
        "mountPath",
        "mounted",
        "transport",
        "removable",
        "hotplug",
        "model",
        "vendor",
        "readOnly",
        "displayName",
        "scannable",
    }
    assert data["name"] == "sdb1"
    assert data["path"] == "/dev/sdb1"
    assert data["type"] == "part"
    assert data["size"] == 16_000_000_000
    assert data["sizeText"] == "14.9 GiB"
    assert data["fstype"] == "vfat"
    assert data["fsver"] == "FAT32"
    assert data["label"] == "USBSTICK"
    assert data["uuid"] == "ABCD-1234"
    assert data["mountpoints"] == ["/media/usb"]
    assert data["mountPath"] == "/media/usb"
    assert data["mounted"] is True
    assert data["transport"] == "usb"
    assert data["removable"] is False
    assert data["hotplug"] is False
    assert data["model"] == "Ultra Fit"
    assert data["vendor"] == "SanDisk"
    assert data["readOnly"] is True
    assert data["displayName"] == "USBSTICK"
    assert data["scannable"] is True

    data["mountpoints"].append("/somewhere/else")
    assert device.mountpoints == ["/media/usb"]  # the dict carries a copy


def test_block_device_to_dict_for_a_parsed_raw_disk(lsblk: dict[str, Any]) -> None:
    data = device_named("sdd").to_dict()
    assert data["mountpoints"] == []
    assert data["mountPath"] == ""
    assert data["mounted"] is False
    assert data["sizeText"] == "7.5 GiB"
    assert data["displayName"] == "DataTraveler"
    assert data["scannable"] is False


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("/dev/sdb1", "sdb1"),
        ("sdb1", "sdb1"),
        ("USBSTICK", "sdb1"),
        ("ABCD-1234", "sdb1"),
        ("/media/usb", "sdb1"),
        ("/media/usb/photos/holiday.jpg", "sdb1"),
        ("BACKUP", "sdc1"),
        ("EFGH-5678", "sdc1"),
        ("root", "nvme0n1p1"),
        ("/dev/sr0", "sr0"),
    ],
)
def test_find_device_by_path_name_label_uuid_and_mount_point(
    lsblk: dict[str, Any], identifier: str, expected: str
) -> None:
    found = find_device(identifier)
    assert found is not None
    assert found.name == expected


@pytest.mark.parametrize("identifier", ["", "/dev/nope", "nope", "/mnt/nowhere", "/dev/sdb9"])
def test_find_device_returns_none_for_unknown_identifiers(
    lsblk: dict[str, Any], identifier: str
) -> None:
    assert find_device(identifier) is None


def test_device_for_path_prefers_the_longest_mount_point(lsblk: dict[str, Any]) -> None:
    assert device_for_path("/media/usb").name == "sdb1"
    assert device_for_path("/media/usb/readme.txt").name == "sdb1"
    # both /media/usb and /media/usb/sub match, the longer one wins
    assert device_for_path("/media/usb/sub").name == "sdb2"
    assert device_for_path("/media/usb/sub/reports/q1.csv").name == "sdb2"


def test_device_for_path_covers_internal_loop_and_optical_paths(lsblk: dict[str, Any]) -> None:
    assert device_for_path("/etc/hosts").name == "nvme0n1p1"
    assert device_for_path("/home/aleks/notes.md").name == "nvme0n1p2"
    assert device_for_path("/var/log/dcs.log").name == "vg0-root"
    assert device_for_path("/media/cdrom/README.txt").name == "sr0"
    # loop devices are considered for path lookups even though they are not media
    assert device_for_path("/snap/core/1/data.bin").name == "loop0"


def test_device_for_path_returns_none_when_no_mount_point_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a machine whose root filesystem was not reported: only the stick is mounted
    monkeypatch.setattr(
        device_module,
        "_run_lsblk",
        lambda: {"blockdevices": [canned_node("sdb1")]},
    )
    assert device_for_path("/media/usb/photos/holiday.jpg").name == "sdb1"
    assert device_for_path("/mnt/nowhere/data.bin") is None
    assert is_removable_mount("/mnt/nowhere/data.bin") is False


def test_is_removable_mount(lsblk: dict[str, Any]) -> None:
    assert is_removable_mount("/media/usb") is True
    assert is_removable_mount("/media/usb/photos/holiday.jpg") is True
    assert is_removable_mount("/media/usb/sub/reports/q1.csv") is True
    assert is_removable_mount("/") is False
    assert is_removable_mount("/home/aleks/notes.md") is False
    assert is_removable_mount("/var/log/dcs.log") is False
    assert is_removable_mount("/snap/core/1/data.bin") is False
    # an unmounted-looking path still resolves to the root device here, which is not removable
    assert is_removable_mount("/mnt/nowhere/data.bin") is False
