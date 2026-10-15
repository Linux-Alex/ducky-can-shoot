"""Put the USB watcher into the desktop session (XDG autostart).

The watcher only helps when something is running while Ducky Can Shoot is
closed, so the autostart entry is what makes "open the interface as soon as I
plug a stick in" work.  It is one .desktop file in ~/.config/autostart, which
every desktop reads: no systemd unit, no daemon, no root.

The entry always runs `dcs watch`, whatever the mode is: with mode "off" the
command exits immediately, so turning the feature off never leaves a stray
process and toggling the mode does not have to touch the autostart file.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dcs.core.log import get_logger
from dcs.version import APP_ID, APP_NAME

log = get_logger("core.autostart")

#: file name inside the autostart directory
AUTOSTART_FILE = "org.kde.dcs.watch.desktop"


def autostart_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "autostart"


def autostart_path() -> Path:
    return autostart_dir() / AUTOSTART_FILE


def default_exec() -> str:
    """How the entry starts the watcher.

    The installed `dcs` launcher when there is one - which is the case for every
    installation scripts/install.sh makes, and also in ~/.local/bin even when
    that directory is not on the PATH of whatever wrote the entry.  Otherwise
    this very interpreter with `-m dcs`, plus the directory that holds the
    package when DCS is being run from a source checkout: without it the entry
    would start an interpreter that cannot import the module at all, and the
    watcher would die before it ever looked at a medium.
    """
    found = shutil.which("dcs") or shutil.which(
        "dcs", path=os.path.join(os.path.expanduser("~"), ".local", "bin")
    )
    if found:
        return found

    command = f"{sys.executable} -m dcs"
    source_root = _source_root()
    if source_root:
        return f"env PYTHONPATH={source_root} {command}"
    return command


def _source_root() -> str:
    """The directory to put on PYTHONPATH so 'python -m dcs' finds the package.

    Empty when the package lives in site-packages, where it needs no help.
    """
    from dcs.version import __file__ as version_file

    package_dir = Path(version_file).resolve().parent
    if "site-packages" in str(package_dir) or "dist-packages" in str(package_dir):
        return ""
    return str(package_dir.parent)


def desktop_entry(exec_line: str | None = None) -> str:
    """The contents of the autostart file."""
    command = (exec_line or default_exec()).strip()
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME} USB watcher
Comment=Watch for newly plugged USB media and notify or open {APP_NAME}
Exec={command} watch
Icon={APP_ID}
Terminal=false
NoDisplay=true
StartupNotify=false
X-GNOME-Autostart-enabled=true
X-KDE-autostart-after=panel
"""


def is_enabled() -> bool:
    return autostart_path().is_file()


def enable(exec_line: str | None = None) -> Path:
    """Write the autostart entry; returns the file that was written."""
    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desktop_entry(exec_line), encoding="utf-8")
    log.info("the USB watcher will start with the session (%s)", path)
    return path


def disable() -> bool:
    """Remove the autostart entry; True when a file was actually removed."""
    path = autostart_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover - read only home
        log.warning("cannot remove %s: %s", path, exc)
        return False
    log.info("the USB watcher will not start with the session any more")
    return True


def starts_the_watcher(text: str) -> bool:
    """True when an existing entry actually runs the watcher.

    Anything that ends in " watch" counts, so an entry written by
    scripts/install.sh (which knows where the launcher landed) survives a save
    from the settings page instead of being replaced by a guess.
    """
    for line in text.splitlines():
        if line.startswith("Exec=") and line.strip().endswith(" watch"):
            return True
    return False


def sync(enabled: bool, exec_line: str | None = None) -> bool:
    """Make the autostart state match *enabled*; True when something changed.

    With an explicit *exec_line* the entry is (re)written.  Without one, an
    entry that already starts the watcher is left exactly as it is, and a file
    that is missing or that cannot start the watcher is repaired.
    """
    if not enabled:
        return disable()
    if exec_line is not None:
        enable(exec_line)
        return True
    try:
        current = autostart_path().read_text(encoding="utf-8")
    except OSError:
        enable()
        return True
    if starts_the_watcher(current):
        return False
    enable()
    return True


__all__ = [
    "AUTOSTART_FILE",
    "autostart_dir",
    "autostart_path",
    "default_exec",
    "desktop_entry",
    "disable",
    "enable",
    "is_enabled",
    "sync",
]
