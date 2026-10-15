"""The watcher as a service: the pid file, the log file and starting it.

Everything here is hermetic: XDG_RUNTIME_DIR and XDG_STATE_HOME point into the
temporary tree of the conftest fixture, and nothing ever starts a real watcher -
the process spawner is replaced by a fake that writes the pid file the way the
real one would.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from dcs.core import service


@pytest.fixture
def fake_spawn(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace the real spawn: record the command, pretend the watcher started."""
    started: list[list[str]] = []

    class FakeProcess:
        def __init__(self, argv: list[str]) -> None:
            self.argv = argv
            started.append(argv)
            # what the real watcher does as its first act
            service.pid_path().parent.mkdir(parents=True, exist_ok=True)
            service.pid_path().write_text(f"{os.getpid()}\n", encoding="utf-8")

    def fake_popen(argv, **kwargs):
        return FakeProcess(list(argv))

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    return started


def test_nothing_is_running_at_first() -> None:
    assert service.watcher_pid() is None
    assert service.watcher_running() is False


def test_claim_takes_the_role_when_nothing_is_running() -> None:
    assert service.claim() is True
    assert service.watcher_pid() == os.getpid()

    service.release()
    assert service.watcher_running() is False


def test_claim_refuses_a_second_watcher() -> None:
    service.pid_path().parent.mkdir(parents=True, exist_ok=True)
    # this very process plays the watcher that is already there
    service.pid_path().write_text(f"{os.getpid()}\n", encoding="utf-8")

    assert service.claim() is False, "a second watcher must stand down"


def test_release_leaves_a_foreign_pid_file_alone() -> None:
    service.pid_path().parent.mkdir(parents=True, exist_ok=True)
    service.pid_path().write_text(f"{os.getppid()}\n", encoding="utf-8")

    service.release()

    assert service.pid_path().is_file(), "not ours to remove"


def test_a_stale_pid_file_is_ignored() -> None:
    service.pid_path().parent.mkdir(parents=True, exist_ok=True)
    service.pid_path().write_text("999999999\n", encoding="utf-8")

    assert service.watcher_pid() is None
    assert service.watcher_running() is False


def test_the_paths_live_under_the_session_and_the_state_directory() -> None:
    assert service.pid_path().name == service.PID_NAME
    assert "ducky-can-shoot" in str(service.pid_path())
    assert service.log_path().name == service.LOG_NAME
    assert str(service.log_path()).endswith(str(Path("dcs") / service.LOG_NAME))


def test_the_command_uses_the_installed_launcher_when_there_is_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda name, path=None: "/usr/bin/dcs")
    assert service.command() == ["/usr/bin/dcs", "watch"]
    assert service.command("--mode", "notify") == ["/usr/bin/dcs", "watch", "--mode", "notify"]


def test_the_command_falls_back_to_this_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda name, path=None: None)

    assert service.command() == [sys.executable, "-m", "dcs", "watch"]


def test_start_spawns_a_watcher_and_reports_it(fake_spawn: list[list[str]]) -> None:
    started, message = service.start()

    assert started is True
    assert "watching for new media" in message
    assert fake_spawn and fake_spawn[0][-1] == "watch"
    assert service.watcher_running() is True
    assert service.log_path().is_file(), "the spawn is logged"


def test_start_is_idempotent(fake_spawn: list[list[str]]) -> None:
    service.start()
    started, message = service.start()

    assert started is False
    assert "already watching" in message
    assert len(fake_spawn) == 1, "a second watcher must not be started"


def test_start_reports_a_watcher_that_never_registers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service.subprocess, "Popen", lambda argv, **kwargs: object())
    monkeypatch.setattr(service, "START_TIMEOUT_S", 0.2)

    started, message = service.start()

    assert started is False
    assert "did not register" in message


def test_start_reports_a_spawn_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def exploding(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(service.subprocess, "Popen", exploding)

    started, message = service.start()

    assert started is False
    assert "cannot start the watcher" in message


def test_stop_kills_the_watcher(fake_spawn: list[list[str]]) -> None:
    service.start()
    pid = service.watcher_pid()
    assert pid is not None

    killed: list[tuple[int, int]] = []
    real_kill = os.kill

    def fake_kill(target: int, sig: int) -> None:
        killed.append((target, sig))
        if target == os.getpid():
            service.pid_path().unlink(missing_ok=True)
        else:  # pragma: no cover - only the watcher pid reaches here
            real_kill(target, sig)

    import dcs.core.service as module

    module.os.kill = fake_kill  # type: ignore[assignment]
    try:
        stopped, message = service.stop()
    finally:
        module.os.kill = real_kill  # type: ignore[assignment]

    assert stopped is True
    assert "stopped the watcher" in message
    assert (pid, 15) in killed, "the watcher is asked to stop, not killed outright"


def test_stop_without_a_watcher_says_so() -> None:
    stopped, message = service.stop()

    assert stopped is False
    assert message == "no watcher is running"
