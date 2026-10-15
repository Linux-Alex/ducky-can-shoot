"""Who is running?  A one-line lock file shared by the interface and the watcher.

Both the interface and 'dcs watch' listen for new media, and each one reacts.
Without a marker they would both notify, both open a window and both start a
job.  The interface therefore claims a pid file when it starts and the watcher
stays quiet while that pid is alive: exactly one of them reacts, and the watcher
takes over again the moment the window is closed.

The file lives next to the session (XDG_RUNTIME_DIR), falls back to the
temporary directory, and a stale file (a pid that no longer exists) is simply
ignored, so a crash never leaves the watcher mute for ever.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from dcs.core.log import get_logger

log = get_logger("core.session")

#: subdirectory inside the runtime directory
RUNTIME_SUBDIR = "ducky-can-shoot"

#: name of the interface lock file
LOCK_NAME = "gui.pid"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / RUNTIME_SUBDIR


def lock_path() -> Path:
    return runtime_dir() / LOCK_NAME


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - another user's process
        return True
    except OSError:  # pragma: no cover - defensive
        return False
    return True


def gui_pid() -> int | None:
    """The pid of a running interface, or None."""
    path = lock_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text.split()[0])
    except (IndexError, ValueError):
        log.debug("ignoring the malformed lock file %s", path)
        return None
    return pid if _alive(pid) else None


def gui_running() -> bool:
    return gui_pid() is not None


def claim() -> Path:
    """Mark this process as the running interface."""
    path = lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - read only runtime dir
        log.debug("cannot write %s: %s", path, exc)
    return path


def release() -> None:
    """Drop the marker, but only when it is ours."""
    path = lock_path()
    try:
        if gui_pid() != os.getpid() and path.is_file():
            return  # somebody else's marker: not ours to remove
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - defensive
        log.debug("cannot remove %s: %s", path, exc)


__all__ = ["LOCK_NAME", "RUNTIME_SUBDIR", "claim", "gui_pid", "gui_running", "lock_path", "release", "runtime_dir"]
