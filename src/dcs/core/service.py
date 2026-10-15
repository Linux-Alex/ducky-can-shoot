"""The watcher as a background service: start it, stop it, find out about it.

`dcs watch` on its own is a foreground command.  This module is what makes the
same thing survive a logout:

* a pid file next to the session marker, so "is a watcher running?" has an
  answer and starting one twice is harmless;
* a log file under the state directory, because a process nobody looks at must
  leave a trace of why it died;
* the command line that starts a detached copy of itself - the installed
  launcher when there is one, this interpreter otherwise.

Why not a systemd user unit?  The watcher is a desktop thing: it needs the
session's notification service and the user's XDG directories, and it must not
run on a machine where nobody is logged in.  The XDG autostart entry is the
standard place for that; this module is what starts it *now* instead of at the
next login.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dcs.core.log import get_logger

log = get_logger("core.service")

#: pid of the running watcher, inside the runtime directory
PID_NAME = "watch.pid"

#: where a detached watcher writes what it does
LOG_NAME = "watch.log"

#: how long to wait for a freshly started watcher to appear
START_TIMEOUT_S = 3.0


def _runtime_dir() -> Path:
    from dcs.core.session import runtime_dir

    return runtime_dir()


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(base) / "dcs"


def pid_path() -> Path:
    return _runtime_dir() / PID_NAME


def log_path() -> Path:
    return _state_dir() / LOG_NAME


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


def watcher_pid() -> int | None:
    """The pid of a running watcher, or None (a stale file is ignored)."""
    try:
        text = pid_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text.split()[0])
    except (IndexError, ValueError):
        return None
    return pid if _alive(pid) else None


def watcher_running() -> bool:
    return watcher_pid() is not None


def claim() -> bool:
    """Mark this process as the watcher; False when one is already running."""
    if watcher_running():
        return False
    path = pid_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - read only runtime directory
        log.debug("cannot write %s: %s", path, exc)
    return True


def release() -> None:
    """Drop the pid file, but only when it is ours."""
    path = pid_path()
    try:
        if watcher_pid() != os.getpid() and path.is_file():
            return
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - defensive
        pass


def command(*extra: str) -> list[str]:
    """How to start a watcher: the installed launcher, or this interpreter.

    scripts/install.sh links `dcs` into ~/.local/bin, which is what the
    autostart entry uses too.  From a source checkout there is no launcher, and
    the interpreter that is running this process is the one that can import the
    package, so `-m dcs` is the right fallback.
    """
    launcher = shutil.which("dcs") or shutil.which(
        "dcs", path=os.path.join(os.path.expanduser("~"), ".local", "bin")
    )
    argv = [launcher] if launcher else [sys.executable, "-m", "dcs"]
    return [*argv, "watch", *extra]


def start(*, allow_gui: bool = False) -> tuple[bool, str]:
    """Start a detached watcher unless one is already running.

    Returns (started_now, message).  The process is detached from this one with
    its own session, so it outlives the window that started it; while an
    interface is open it stays quiet on its own (see dcs.core.session).
    """
    if watcher_running():
        return False, f"already watching (pid {watcher_pid()})"

    log_path().parent.mkdir(parents=True, exist_ok=True)
    argv = command()
    try:
        with log_path().open("a", encoding="utf-8") as stream:
            stream.write(f"\n--- starting {' '.join(argv)} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            # the command is built here, never from user input
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        return False, f"cannot start the watcher: {exc}"

    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if watcher_running():
            return True, f"watching for new media (pid {watcher_pid()})"
        time.sleep(0.05)
    return False, f"the watcher started but did not register; see {log_path()}"


def stop() -> tuple[bool, str]:
    """Stop a running watcher; returns (stopped, message)."""
    pid = watcher_pid()
    if pid is None:
        return False, "no watcher is running"
    try:
        os.kill(pid, 15)
    except OSError as exc:
        return False, f"cannot stop the watcher: {exc}"
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if not watcher_running():
            return True, f"stopped the watcher (pid {pid})"
        time.sleep(0.05)
    with contextlib.suppress(OSError):  # pragma: no cover - only if SIGTERM is ignored
        os.kill(pid, 9)
    return True, f"stopped the watcher (pid {pid})"


__all__ = [
    "LOG_NAME",
    "PID_NAME",
    "claim",
    "command",
    "log_path",
    "pid_path",
    "release",
    "start",
    "stop",
    "watcher_pid",
    "watcher_running",
]
