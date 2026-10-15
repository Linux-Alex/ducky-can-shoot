"""Removable media discovery and mounting.

We deliberately shell out to lsblk and udisksctl instead of depending on
pyudev/dbus bindings:

* lsblk -J is available everywhere util-linux is, and reports exactly the
  fields the GUI wants (transport, removable flag, label, size, mountpoint).
* udisksctl mount works for an unprivileged desktop user through polkit,
  which is the only sane way to handle a stick that is not mounted yet.

Everything here is read-only with respect to the device itself: DCS never
writes to the scanned medium.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from dcs.core.log import get_logger
from dcs.core.util import human_bytes

log = get_logger("device")

LSBLK_COLUMNS = (
    "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,TRAN,RM,HOTPLUG,MODEL,VENDOR,RO,FSVER"
)

#: filesystems that are interesting to triage
SCANNABLE_TYPES = {
    "vfat",
    "exfat",
    "ntfs",
    "ntfs3",
    "fuseblk",
    "ext2",
    "ext3",
    "ext4",
    "btrfs",
    "xfs",
    "f2fs",
    "iso9660",
    "udf",
    "hfsplus",
    "apfs",
    "vfat32",
}


@dataclass(slots=True)
class BlockDevice:
    """One block device node as reported by lsblk."""

    name: str
    path: str
    type: str = ""
    size: int = 0
    fstype: str = ""
    label: str = ""
    uuid: str = ""
    mountpoints: list[str] = field(default_factory=list)
    transport: str = ""
    removable: bool = False
    hotplug: bool = False
    model: str = ""
    vendor: str = ""
    read_only: bool = False
    fsver: str = ""

    @property
    def mounted(self) -> bool:
        return bool(self.mountpoints)

    @property
    def mount_path(self) -> str:
        return self.mountpoints[0] if self.mountpoints else ""

    @property
    def display_name(self) -> str:
        return self.label or self.model or self.name

    @property
    def scannable(self) -> bool:
        """True when this is a filesystem we can actually walk."""
        if self.type not in ("part", "disk", "loop", "rom"):
            return False
        if not self.fstype:
            return False
        return self.fstype.lower() in SCANNABLE_TYPES or self.fstype.startswith("ext")

    @property
    def size_text(self) -> str:
        return human_bytes(self.size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "size": self.size,
            "sizeText": self.size_text,
            "fstype": self.fstype,
            "fsver": self.fsver,
            "label": self.label,
            "uuid": self.uuid,
            "mountpoints": list(self.mountpoints),
            "mountPath": self.mount_path,
            "mounted": self.mounted,
            "transport": self.transport,
            "removable": self.removable,
            "hotplug": self.hotplug,
            "model": self.model,
            "vendor": self.vendor,
            "readOnly": self.read_only,
            "displayName": self.display_name,
            "scannable": self.scannable,
        }


# ---------------------------------------------------------------------------
# enumeration
# ---------------------------------------------------------------------------


def _run_lsblk() -> dict[str, Any]:
    argv = ["lsblk", "--json", "--bytes", "--output", LSBLK_COLUMNS]
    env = dict(os.environ)
    env["LC_ALL"] = "C"  # keep sizes and labels free of locale formatting
    env["LANG"] = "C"
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=10, env=env, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("lsblk failed: %s", exc)
        return {}
    if proc.returncode != 0:
        log.error("lsblk exited with %s: %s", proc.returncode, proc.stderr.strip())
        return {}
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        log.error("cannot parse lsblk output: %s", exc)
        return {}


def _device_from_node(node: dict[str, Any]) -> BlockDevice:
    mountpoints = node.get("mountpoints") or []
    if isinstance(mountpoints, str):
        mountpoints = [mountpoints]
    return BlockDevice(
        name=str(node.get("name") or ""),
        path=str(node.get("path") or f"/dev/{node.get('name', '')}"),
        type=str(node.get("type") or ""),
        size=int(node.get("size") or 0),
        fstype=str(node.get("fstype") or ""),
        label=str(node.get("label") or ""),
        uuid=str(node.get("uuid") or ""),
        mountpoints=[str(m) for m in mountpoints if m],
        transport=str(node.get("tran") or ""),
        removable=bool(node.get("rm")),
        hotplug=bool(node.get("hotplug")),
        model=str(node.get("model") or "").strip(),
        vendor=str(node.get("vendor") or "").strip(),
        read_only=bool(node.get("ro")),
        fsver=str(node.get("fsver") or ""),
    )


def list_block_devices(*, include_loop: bool = False) -> list[BlockDevice]:
    """Every block device lsblk can see, flattened."""
    data = _run_lsblk()
    devices: list[BlockDevice] = []

    def visit(node: dict[str, Any]) -> None:
        device = _device_from_node(node)
        if include_loop or device.type != "loop":
            devices.append(device)
        for child in node.get("children") or []:
            visit(child)

    for node in data.get("blockdevices") or []:
        visit(node)
    return devices


def is_removable(device: BlockDevice) -> bool:
    """True when a human would call this device a USB stick.

    A device qualifies when it is scannable and either flagged removable, or
    attached over usb/mmc, or hotplug capable.  Internal NVMe/SATA partitions
    are therefore excluded while USB enclosures that do not set the removable
    bit (very common) are still listed.
    """
    if not device.scannable:
        return False
    bus = device.transport.lower()
    return bool(device.removable or device.hotplug or bus in ("usb", "mmc", "sdio", "ieee1394"))


def list_removable_devices(*, include_loop: bool = False) -> list[BlockDevice]:
    """Every device a human would call a USB stick, mounted ones first."""
    result = [d for d in list_block_devices(include_loop=include_loop) if is_removable(d)]
    result.sort(key=lambda d: (not d.mounted, d.name))
    return result


def find_device(identifier: str) -> BlockDevice | None:
    """Look a device up by node path, name, label or mount point.

    Unlike list_removable_devices() this searches *every* block device: the
    caller asked for a specific node, which may be a loop device, an LVM volume
    or anything else that does not carry the "removable" flag.
    """
    if not identifier:
        return None
    wanted = os.path.abspath(identifier) if os.path.isabs(identifier) else identifier
    devices = list_block_devices(include_loop=True)
    for device in devices:
        if wanted in (device.path, device.name, device.label, device.uuid):
            return device
    for device in devices:
        if device.mount_path and (device.mount_path == wanted or wanted.startswith(device.mount_path + "/")):
            return device
    return None


def device_for_path(path: str) -> BlockDevice | None:
    """The block device a file path lives on (longest mount point wins)."""
    target = os.path.abspath(path)
    best: BlockDevice | None = None
    best_length = -1
    for device in list_block_devices(include_loop=True):
        for mount in device.mountpoints:
            if (target == mount or target.startswith(mount.rstrip("/") + "/")) and len(mount) > best_length:
                best, best_length = device, len(mount)
    return best


def is_removable_mount(path: str) -> bool:
    device = device_for_path(path)
    if device is None:
        return False
    bus = device.transport.lower()
    return bool(device.removable or device.hotplug or bus in ("usb", "mmc"))


# ---------------------------------------------------------------------------
# mount / unmount through udisks2 (no sudo needed)
# ---------------------------------------------------------------------------

_MOUNT_RE = re.compile(r"at\s+(?P<path>/\S+?)\.?\s*$")


def mount_device(device: BlockDevice | str) -> tuple[bool, str]:
    """Mount through udisksctl; returns (ok, mount_path_or_error)."""
    node = device.path if isinstance(device, BlockDevice) else device
    if isinstance(device, BlockDevice) and device.mounted:
        return True, device.mount_path
    try:
        proc = subprocess.run(
            ["udisksctl", "mount", "-b", node],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except FileNotFoundError:
        return False, "udisksctl is not installed (package udisks2)"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, output.strip() or f"udisksctl exited with {proc.returncode}"
    match = _MOUNT_RE.search(output.strip())
    if match:
        return True, match.group("path").rstrip(".")
    # fall back to a fresh lsblk query
    fresh = find_device(node)
    if fresh and fresh.mounted:
        return True, fresh.mount_path
    return False, output.strip() or "mounted but the mount point is unknown"


def unmount_device(device: BlockDevice | str) -> tuple[bool, str]:
    node = device.path if isinstance(device, BlockDevice) else device
    try:
        proc = subprocess.run(
            ["udisksctl", "unmount", "-b", node],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except FileNotFoundError:
        return False, "udisksctl is not installed (package udisks2)"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output


def open_in_file_manager(path: str) -> bool:
    """Hand a path to the desktop's file manager (best effort)."""
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices

    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
