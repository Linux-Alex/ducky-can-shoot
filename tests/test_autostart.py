"""The autostart entry and the "who is running" marker.

Both exist so that exactly one thing reacts when a stick is plugged in: the
window when it is open, the background watcher when it is not.  The tests keep
everything inside the temporary XDG directories of the autouse fixture.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dcs.core import autostart, session
from dcs.version import APP_ID, APP_NAME

# ---------------------------------------------------------------------------
# the autostart entry
# ---------------------------------------------------------------------------


def test_the_entry_lives_in_the_xdg_autostart_directory(tmp_path: Path) -> None:
    path = autostart.autostart_path()
    assert path.parent == Path(os.environ["XDG_CONFIG_HOME"]) / "autostart"
    assert path.name == autostart.AUTOSTART_FILE


def test_enable_writes_a_desktop_entry_that_starts_the_watcher() -> None:
    assert autostart.is_enabled() is False
    path = autostart.enable("/usr/bin/dcs-probe")

    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]")
    assert "Exec=/usr/bin/dcs-probe watch" in text
    assert f"Icon={APP_ID}" in text
    assert APP_NAME in text
    assert "Terminal=false" in text
    assert autostart.is_enabled() is True


def test_disable_removes_it_and_says_whether_it_did() -> None:
    autostart.enable("/usr/bin/dcs-probe")
    assert autostart.disable() is True
    assert autostart.is_enabled() is False
    assert autostart.disable() is False, "removing what is not there is not a change"


def test_sync_only_reports_a_change_it_made() -> None:
    assert autostart.sync(True, "/usr/bin/dcs-probe") is True
    assert autostart.sync(True, "/usr/bin/dcs-probe") is True  # explicit rewrite
    assert autostart.sync(True) is False, "an entry that starts the watcher is kept"
    assert autostart.sync(False) is True
    assert autostart.sync(False) is False


def test_sync_keeps_an_entry_written_by_the_installer() -> None:
    autostart.enable("/opt/dcs/bin/dcs")

    assert autostart.sync(True) is False
    assert "Exec=/opt/dcs/bin/dcs watch" in autostart.autostart_path().read_text(encoding="utf-8")


def test_sync_repairs_a_file_that_cannot_start_the_watcher() -> None:
    autostart.autostart_path().parent.mkdir(parents=True, exist_ok=True)
    autostart.autostart_path().write_text("[Desktop Entry]\nExec=/wrong thing\n", encoding="utf-8")

    assert autostart.sync(True) is True
    assert autostart.starts_the_watcher(
        autostart.autostart_path().read_text(encoding="utf-8")
    )
    assert autostart.sync(True) is False


def test_the_default_command_is_a_launcher_or_this_interpreter() -> None:
    line = autostart.default_exec()
    assert line.endswith(" -m dcs") or line.endswith("/dcs")


def test_a_source_checkout_starts_the_watcher_through_the_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without a launcher the entry has to help the interpreter find the package.

    DCS run from a checkout is not in site-packages, so 'python -m dcs' would die
    with ModuleNotFoundError before the watcher ever looked at a medium.
    """
    monkeypatch.setattr(autostart.shutil, "which", lambda name, path=None: None)
    monkeypatch.setenv("HOME", str(tmp_path))  # and no ~/.local/bin/dcs either

    line = autostart.default_exec()

    assert line.endswith(" -m dcs")
    assert line.startswith("env PYTHONPATH="), "the checkout is put on the path"
    assert autostart.default_exec() in autostart.desktop_entry()


def test_the_launcher_in_the_home_directory_is_found_without_a_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """~/.local/bin is not always on the PATH of whatever writes the entry."""
    launcher = tmp_path / ".local" / "bin" / "dcs"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    def which(name: str, path: str | None = None) -> str | None:
        if path is None:
            return None  # not on the PATH of whatever wrote the entry
        candidate = Path(path) / name
        return str(candidate) if candidate.is_file() else None

    monkeypatch.setattr(autostart.shutil, "which", which)

    assert autostart.default_exec() == str(launcher)


# ---------------------------------------------------------------------------
# the "is the interface running?" marker
# ---------------------------------------------------------------------------


def test_the_lock_lives_in_the_runtime_directory() -> None:
    path = session.lock_path()
    assert path.name == session.LOCK_NAME
    assert session.RUNTIME_SUBDIR in str(path)


def test_claiming_the_marker_makes_the_interface_running() -> None:
    assert session.gui_running() is False
    session.claim()
    assert session.gui_running() is True
    assert session.gui_pid() == os.getpid()
    session.release()
    assert session.gui_running() is False


def test_a_stale_marker_is_ignored() -> None:
    path = session.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("999999999\n", encoding="utf-8")
    assert session.gui_pid() is None
    assert session.gui_running() is False


def test_a_malformed_marker_is_ignored() -> None:
    path = session.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a pid\n", encoding="utf-8")
    assert session.gui_running() is False


def test_release_leaves_someone_elses_marker_alone() -> None:
    path = session.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # a live process that is not this one: the test runner's parent shell
    path.write_text(f"{os.getppid()}\n", encoding="utf-8")
    session.release()
    assert path.is_file()
