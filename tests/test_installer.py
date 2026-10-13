"""Tests for dcs.core.installer.

Nothing here touches the real system: detection is driven through a fake
which(), the plans are built from deliberately harmless commands, and the
script is only ever executed when every step is echo/true/false/sleep.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from dcs.core import installer
from dcs.core.config import AppConfig, ClamAVConfig, ScanConfig
from dcs.core.installer import (
    PACKAGE_MANAGERS,
    Installer,
    InstallPlan,
    InstallStep,
    PackageManager,
    build_plan,
    build_steps,
    detect_package_manager,
    find_terminal,
    manual_instructions,
    plan_summary,
    render_script,
    verify,
)

#: a package manager whose commands are harmless, for exercising the runner
HARMLESS = PackageManager(
    id="test",
    label="Test manager",
    refresh=(("true",),),
    install=(("sh", "-c", "echo installing"),),
    daemon_unit="",
    freshclam=False,
)


def fake_which(names: dict[str, str]):
    """A shutil.which replacement that only knows the given binaries."""

    def which(binary: str) -> str | None:
        return names.get(binary)

    return which


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


def test_detect_prefers_apt_on_a_debian_like_system(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        installer.shutil,
        "which",
        fake_which({"apt-get": "/usr/bin/apt-get", "dnf": "/usr/bin/dnf", "pacman": "/usr/bin/pacman"}),
    )
    manager = detect_package_manager()
    assert manager is not None
    assert manager.id == "apt"


def test_detect_returns_the_first_known_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.shutil, "which", fake_which({"pacman": "/usr/bin/pacman"}))
    manager = detect_package_manager()
    assert manager is not None and manager.id == "pacman"


def test_detect_returns_none_without_a_known_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.shutil, "which", fake_which({}))
    assert detect_package_manager() is None


def test_manual_plan_when_no_manager_is_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(installer.shutil, "which", fake_which({}))
    plan = build_plan(cache_dir=str(tmp_path))
    assert plan.possible is False
    assert "no supported package manager" in plan.blocker
    assert plan.steps == []
    assert manual_instructions(plan) == "No installation commands are known for this system."


# ---------------------------------------------------------------------------
# plan shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("manager_id", sorted(PACKAGE_MANAGERS))
def test_every_manager_yields_a_runnable_plan(manager_id: str) -> None:
    manager = PACKAGE_MANAGERS[manager_id]
    steps = build_steps(manager)
    assert steps, f"{manager_id} produced no steps"
    assert any(step.argv for step in steps)
    for step in steps:
        assert step.title
        assert step.argv and all(isinstance(part, str) and part for part in step.argv)
    script = render_script(steps, distro="Test", manager=manager_id)
    assert script.startswith("#!/bin/bash")
    assert script.rstrip().endswith("exit 0")


def test_apt_plan_installs_clamav_and_starts_the_daemon(tmp_path: Path) -> None:
    steps = build_steps(PACKAGE_MANAGERS["apt"])
    commands = [step.argv for step in steps]
    assert ("apt-get", "update") in commands
    install = next(argv for argv in commands if argv[:2] == ("apt-get", "install"))
    assert {"clamav", "clamav-daemon", "clamav-freshclam"} <= set(install)
    assert any(argv[:1] == ("freshclam",) for argv in commands)
    assert ("systemctl", "enable", "--now", "clamav-daemon") in commands


def test_daemon_steps_can_be_skipped() -> None:
    with_daemon = build_steps(PACKAGE_MANAGERS["apt"], want_daemon=True)
    without = build_steps(PACKAGE_MANAGERS["apt"], want_daemon=False)
    assert len(without) < len(with_daemon)
    assert not any("systemctl" in step.argv for step in without)


def test_optional_steps_do_not_abort_the_script() -> None:
    steps = [
        InstallStep("required", ("true",)),
        InstallStep("optional", ("false",), optional=True),
    ]
    script = render_script(steps)
    assert 'step 1 "required" true || exit 10' in script
    assert 'step 2 "optional" false || true' in script
    assert "export DEBIAN_FRONTEND=noninteractive" in script


def test_step_command_line_quotes_arguments() -> None:
    step = InstallStep("tricky", ("echo", "two words", "a'b"))
    import shlex

    assert step.as_command_line() == shlex.join(("echo", "two words", "a'b"))
    assert "'two words'" in step.as_command_line()


def test_script_is_written_with_owner_only_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: HARMLESS)
    monkeypatch.setattr(installer, "running_as_root", lambda: True)
    plan = build_plan(cache_dir=str(tmp_path))
    assert plan.script_path
    path = Path(plan.script_path)
    assert path.parent == tmp_path
    assert stat.S_IMODE(path.stat().st_mode) == 0o700  # owner only
    assert path.read_text(encoding="utf-8") == plan.script


def test_unwritable_cache_directory_is_reported_as_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: HARMLESS)
    target = tmp_path / "file-not-a-directory"
    target.write_text("x", encoding="utf-8")
    plan = build_plan(cache_dir=str(target / "cache"))
    assert plan.script_path == ""
    assert any("could not be saved" in warning for warning in plan.warnings)


# ---------------------------------------------------------------------------
# privilege method selection
# ---------------------------------------------------------------------------


def test_root_plan_needs_no_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: HARMLESS)
    monkeypatch.setattr(installer, "running_as_root", lambda: True)
    plan = build_plan(write_script=False)
    assert plan.method == "root"
    assert plan.to_dict()["needsPassword"] is False


def test_pkexec_is_used_when_a_polkit_agent_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: HARMLESS)
    monkeypatch.setattr(installer, "running_as_root", lambda: False)
    monkeypatch.setattr(installer, "polkit_agent_running", lambda: True)
    monkeypatch.setattr(installer.shutil, "which", fake_which({"pkexec": "/usr/bin/pkexec"}))
    plan = build_plan(write_script=False)
    assert plan.method == "pkexec"
    assert "password" in plan.method_label


def test_a_terminal_is_used_without_a_polkit_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: HARMLESS)
    monkeypatch.setattr(installer, "running_as_root", lambda: False)
    monkeypatch.setattr(installer, "polkit_agent_running", lambda: False)
    monkeypatch.setattr(
        installer.shutil, "which", fake_which({"konsole": "/usr/bin/konsole", "sudo": "/usr/bin/sudo"})
    )
    plan = build_plan(write_script=False)
    assert plan.method == "terminal"
    assert plan.terminal == "/usr/bin/konsole"
    assert plan.runner[0] == "/usr/bin/konsole"


def test_the_cli_can_prefer_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: HARMLESS)
    monkeypatch.setattr(installer, "running_as_root", lambda: False)
    monkeypatch.setattr(installer.shutil, "which", fake_which({"sudo": "/usr/bin/sudo"}))
    plan = build_plan(prefer="sudo", write_script=False)
    assert plan.method == "sudo"
    assert plan.runner == ["sudo"]


def test_plan_is_blocked_when_nothing_can_ask_for_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: HARMLESS)
    monkeypatch.setattr(installer, "running_as_root", lambda: False)
    monkeypatch.setattr(installer, "polkit_agent_running", lambda: False)
    monkeypatch.setattr(installer.shutil, "which", fake_which({}))
    plan = build_plan(write_script=False)
    assert plan.possible is False
    assert "copy the commands" in plan.blocker
    assert manual_instructions(plan).startswith("# run these as root")


def test_find_terminal_returns_exec_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.shutil, "which", fake_which({"gnome-terminal": "/usr/bin/gnome-terminal"}))
    path, args = find_terminal()
    assert path == "/usr/bin/gnome-terminal"
    assert args == ("--",)


def test_plan_dict_is_json_friendly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import json

    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: PACKAGE_MANAGERS["apt"])
    monkeypatch.setattr(installer, "running_as_root", lambda: True)
    plan = build_plan(cache_dir=str(tmp_path))
    data = plan.to_dict()
    json.dumps(data)  # must not raise
    assert data["manager"] == "apt"
    assert data["steps"] and data["commands"] == [step["command"] for step in data["steps"]]
    assert data["scriptPath"] == plan.script_path
    assert "distribution" in plan_summary(plan)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def run_plan(steps: list[InstallStep], tmp_path: Path) -> tuple[installer.InstallOutcome, list[str]]:
    """Run a plan of harmless commands through the real Installer."""
    script = render_script(steps, distro="Test", manager="test")
    path = tmp_path / "plan.sh"
    path.write_text(script, encoding="utf-8")
    plan = InstallPlan(manager=HARMLESS, steps=steps, method="root", script_path=str(path), script=script)
    lines: list[str] = []
    outcome = Installer(plan).run(on_output=lines.append)
    return outcome, lines


def test_installer_streams_output_and_reports_success(tmp_path: Path) -> None:
    steps = [
        InstallStep("first", ("sh", "-c", "echo hello from step one"), optional=False),
        InstallStep("second", ("sh", "-c", "echo hello from step two"), optional=False),
    ]
    outcome, lines = run_plan(steps, tmp_path)
    assert outcome.ok is True
    assert outcome.exit_code == 0
    assert [step.index for step in outcome.steps] == [1, 2]
    assert all(step.ok for step in outcome.steps)
    assert any("hello from step one" in line for line in lines)
    assert any("hello from step two" in line for line in lines)
    assert any("=== DCS step 2: second ===" in line for line in lines)


def test_installer_reports_the_failing_step(tmp_path: Path) -> None:
    steps = [
        InstallStep("fine", ("true",)),
        InstallStep("broken", ("false",)),
        InstallStep("never runs", ("sh", "-c", "echo should-not-appear"), optional=False),
    ]
    outcome, lines = run_plan(steps, tmp_path)
    assert outcome.ok is False
    assert outcome.failed_step == 2
    assert outcome.exit_code == 20
    assert "step 2 failed" in outcome.error
    assert not any("should-not-appear" in line for line in lines)


def test_optional_failure_does_not_stop_the_install(tmp_path: Path) -> None:
    steps = [
        InstallStep("required", ("true",)),
        InstallStep("optional but broken", ("false",), optional=True),
        InstallStep("still runs", ("sh", "-c", "echo after-optional"), optional=False),
    ]
    outcome, lines = run_plan(steps, tmp_path)
    assert outcome.ok is True
    assert any("after-optional" in line for line in lines)
    broken = next(step for step in outcome.steps if step.index == 2)
    assert broken.ok is False and broken.code == 1


def test_installer_can_be_cancelled(tmp_path: Path) -> None:
    script = render_script([InstallStep("long", ("sleep", "30"))], distro="Test", manager="test")
    path = tmp_path / "plan.sh"
    path.write_text(script, encoding="utf-8")
    plan = InstallPlan(manager=HARMLESS, steps=[InstallStep("long", ("sleep", "30"))], method="root",
                       script_path=str(path), script=script)
    runner = Installer(plan)
    threading.Timer(0.4, runner.cancel).start()
    started = time.monotonic()
    outcome = runner.run()
    assert outcome.cancelled is True
    assert outcome.ok is False
    assert time.monotonic() - started < 20


def test_installer_refuses_a_plan_without_a_script() -> None:
    plan = InstallPlan(manager=HARMLESS, steps=[InstallStep("x", ("true",))], method="root")
    outcome = Installer(plan).run()
    assert outcome.ok is False
    assert "script" in outcome.error


def test_installer_reports_a_blocked_plan() -> None:
    plan = InstallPlan(blocker="nothing to do here", method="manual")
    outcome = Installer(plan).run()
    assert outcome.ok is False
    assert outcome.error == "nothing to do here"
    assert outcome.started is False


def test_installer_reports_a_missing_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = InstallPlan(
        manager=HARMLESS,
        steps=[InstallStep("x", ("true",))],
        method="pkexec",
        script_path="/nonexistent/plan.sh",
    )
    outcome = Installer(plan).run()
    assert outcome.ok is False
    assert "could not be written" in outcome.error


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def test_verify_accepts_every_config_shape() -> None:
    for config in (AppConfig(), ScanConfig(), ClamAVConfig(), None):
        status = verify(config)
        assert set(status) >= {"available", "mode", "problem", "line"}
        assert isinstance(status["available"], bool)


def test_verify_relays_the_probe_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """verify() reports what the scanner module found, whatever that is.

    The suite must pass on a machine with ClamAV installed and on one without,
    so the probe is stubbed in both directions instead of assuming either.
    """
    from dcs.core.scanner import clamav as clamav_module

    def unavailable(config: ClamAVConfig) -> clamav_module.ClamAVProbe:
        return clamav_module.ClamAVProbe(problem="ClamAV is not installed (stubbed)")

    monkeypatch.setattr(clamav_module, "probe", unavailable)
    status = verify(AppConfig())
    assert status["available"] is False
    assert "stubbed" in status["line"]
    assert status["mode"] == ""

    def available(config: ClamAVConfig) -> clamav_module.ClamAVProbe:
        return clamav_module.ClamAVProbe(
            binary="/usr/bin/clamdscan",
            mode="clamdscan",
            version="ClamAV 9.9 (stubbed)",
            daemon_ok=True,
        )

    monkeypatch.setattr(clamav_module, "probe", available)
    status = verify(AppConfig())
    assert status["available"] is True
    assert status["mode"] == "clamdscan"
    assert "ClamAV 9.9" in status["line"]


def test_cancelled_flag_is_visible_before_running(tmp_path: Path) -> None:
    plan = InstallPlan(manager=HARMLESS, steps=[InstallStep("x", ("true",))], method="root")
    runner = Installer(plan)
    assert runner.cancelled is False
    runner.cancel()
    assert runner.cancelled is True


def test_script_marks_each_step_for_the_progress_view() -> None:
    steps = [InstallStep("Install ClamAV", ("apt-get", "install", "-y", "clamav"))]
    script = render_script(steps, distro="Ubuntu", manager="apt")
    assert '# Install ClamAV' in script
    assert 'step 1 "Install ClamAV" apt-get install -y clamav || exit 10' in script
    assert os.linesep not in script or script.endswith(os.linesep)
