"""The update check and the installation it can trigger.

Nothing here reaches GitHub: `updates.http_json` and `updates.http_stream`
are replaced, the package manager is faked, and pip is faked with a Popen that
prints what a real one would.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from dcs.core import installer
from dcs.core import updates as mod
from dcs.core.config import UpdateConfig
from dcs.core.updates import (
    Asset,
    InstallResult,
    InstallTarget,
    Release,
    UpdateCheck,
    UpdateError,
    auto_check_due,
    build_deb_plan,
    check_for_update,
    detect_install,
    download,
    fetch_latest,
    install_deb,
    install_wheel,
    is_newer,
    other_installs,
    parse_version,
    read_state,
    restart_argv,
    write_state,
)

CURRENT = "0.2.2"


# -- versions --------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.2.2", (0, 2, 2)),
        ("v0.2.2", (0, 2, 2)),
        ("V1.0", (1, 0, 0)),
        ("0.3", (0, 3, 0)),
        ("1.2.3.4", None),
        ("banana", None),
        ("", None),
    ],
)
def test_versions_are_parsed(text: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("0.2.3", "0.2.2", True),
        ("v0.2.3", "0.2.2", True),
        ("1.0.0", "0.2.2", True),
        ("0.2.2", "0.2.2", False),
        ("0.2.1", "0.2.2", False),
        ("banana", "0.2.2", False),
        ("0.2.3", "banana", False),
    ],
)
def test_version_comparison(candidate: str, current: str, expected: bool) -> None:
    assert is_newer(candidate, current) is expected


# -- the release API -------------------------------------------------------
def payload(version: str = "v0.2.3", *, assets: bool = True) -> dict[str, Any]:
    return {
        "tag_name": version,
        "name": f"Ducky Can Shoot {version.lstrip('v')}",
        "html_url": f"https://github.com/Linux-Alex/ducky-can-shoot/releases/tag/{version}",
        "published_at": "2026-10-20T10:00:00Z",
        "body": "### Added\n- something",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "ducky-can-shoot_0.2.3-1_all.deb",
                "browser_download_url": "https://example.invalid/pkg.deb",
                "size": 762178,
            },
            {
                "name": "ducky_can_shoot-0.2.3-py3-none-any.whl",
                "browser_download_url": "https://example.invalid/pkg.whl",
                "size": 581157,
            },
        ]
        if assets
        else [],
    }


def test_a_release_payload_becomes_a_release() -> None:
    release = Release.from_payload(payload())

    assert release.version == "0.2.3"
    assert release.tag == "v0.2.3"
    assert release.name == "Ducky Can Shoot 0.2.3"
    assert len(release.assets) == 2
    assert release.asset_for("deb").name.endswith(".deb")
    assert release.asset_for("venv").name.endswith(".whl")
    assert release.asset_for("source") is None


def test_fetch_latest_reads_the_newest_release(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_http(method: str, url: str, **kwargs: Any) -> tuple[int, bytes]:
        seen.append(url)
        return 200, json.dumps(payload()).encode()

    monkeypatch.setattr(mod, "http_json", fake_http)

    release = fetch_latest()

    assert release.version == "0.2.3"
    assert seen == [mod.api_url()]


def test_a_repository_without_releases_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "http_json", lambda *a, **k: (404, b"{}"))

    with pytest.raises(UpdateError, match="no published release"):
        fetch_latest()


def test_a_rate_limited_api_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "http_json", lambda *a, **k: (403, b"{}"))

    with pytest.raises(UpdateError, match="rate limit"):
        fetch_latest()


def test_a_prerelease_is_never_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    body = payload()
    body["prerelease"] = True
    monkeypatch.setattr(mod, "http_json", lambda *a, **k: (200, json.dumps(body).encode()))

    with pytest.raises(UpdateError, match="pre-release"):
        fetch_latest()


def test_a_garbage_answer_is_an_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "http_json", lambda *a, **k: (200, b"<html>"))

    with pytest.raises(UpdateError, match="not JSON"):
        fetch_latest()


def test_a_network_failure_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise UpdateError("no route to host")

    monkeypatch.setattr(mod, "http_json", boom)

    with pytest.raises(UpdateError, match="no route"):
        fetch_latest()


# -- the check -------------------------------------------------------------
def test_a_newer_deb_release_can_be_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mod, "detect_install", lambda **_: InstallTarget("deb", "system package 0.2.2", needs_root=True)
    )

    check = check_for_update(current=CURRENT, release=Release.from_payload(payload()))

    assert check.newer is True
    assert check.can_install is True
    assert check.asset is not None and check.asset.name.endswith(".deb")
    assert "0.2.3 is available" in check.message
    info = check.to_dict()
    assert info["install"]["needsRoot"] is True
    assert info["releaseUrl"].endswith("v0.2.3")


def test_a_venv_release_installs_a_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "detect_install", lambda **_: InstallTarget("venv", "venv at /tmp/v"))

    check = check_for_update(current=CURRENT, release=Release.from_payload(payload()))

    assert check.asset is not None and check.asset.name.endswith(".whl")
    assert check.can_install is True


def test_a_checkout_is_told_to_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mod, "detect_install", lambda **_: InstallTarget("source", "git checkout at /src", path="/src")
    )

    check = check_for_update(current=CURRENT, release=Release.from_payload(payload()))

    assert check.newer is True
    assert check.can_install is False
    assert "git pull" in check.message
    assert "/src" in check.message


def test_the_same_version_is_not_an_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "detect_install", lambda **_: InstallTarget("deb"))

    check = check_for_update(current="0.2.3", release=Release.from_payload(payload()))

    assert check.newer is False
    assert check.can_install is False
    assert "newest release" in check.message


def test_a_failed_check_carries_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs: Any) -> Release:
        raise UpdateError("GitHub answered HTTP 500")

    monkeypatch.setattr(mod, "fetch_latest", boom)

    check = check_for_update(current=CURRENT)

    assert check.newer is False
    assert check.error == "GitHub answered HTTP 500"
    assert check.message == "GitHub answered HTTP 500"


def test_a_release_without_an_asset_says_which_kind_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "detect_install", lambda **_: InstallTarget("deb"))

    check = check_for_update(current=CURRENT, release=Release.from_payload(payload(assets=False)))

    assert check.can_install is False
    assert "no package" in check.message


# -- which copy is running -------------------------------------------------
def test_a_checkout_is_detected(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "src" / "dcs").mkdir(parents=True)
    package = tmp_path / "src" / "dcs" / "__init__.py"
    package.write_text("")

    target = detect_install(package=package)

    assert target.kind == "source"
    assert target.can_install is False


def test_a_virtualenv_is_detected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package = tmp_path / "lib" / "site-packages" / "dcs" / "__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("")
    monkeypatch.setattr(sys, "prefix", "/tmp/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")

    target = detect_install(package=package)

    assert target.kind == "venv"
    assert target.needs_root is False


def test_a_distribution_package_is_detected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package = tmp_path / "dist-packages" / "dcs" / "__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("")
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(mod, "installed_deb_version", lambda: "0.2.2-1")

    target = detect_install(package=package)

    assert target.kind == "deb"
    assert target.needs_root is True
    assert "0.2.2-1" in target.detail


def test_other_installs_lists_the_system_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "installed_deb_version", lambda: "0.2.1-1")

    found = other_installs(InstallTarget("venv", "venv at /tmp/v"))

    assert any("system package 0.2.1-1" in item for item in found)


# -- remembering the last check --------------------------------------------
def test_the_state_round_trips_and_respects_the_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "state_path", lambda: tmp_path / "state.json")

    check = check_for_update(current=CURRENT, release=Release.from_payload(payload()))
    write_state(check)

    state = read_state()
    assert state["latest"] == "0.2.3"
    assert state["newer"] is True
    assert auto_check_due(UpdateConfig(check_interval_hours=6)) is False
    assert auto_check_due(UpdateConfig(check_interval_hours=0)) is True


def test_a_fresh_machine_checks_at_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "state_path", lambda: tmp_path / "nothing.json")

    assert auto_check_due(UpdateConfig()) is True


def test_the_check_can_be_switched_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "state_path", lambda: tmp_path / "nothing.json")

    assert auto_check_due(UpdateConfig(enabled=False)) is False
    assert auto_check_due(UpdateConfig(check_on_start=False)) is False


# -- download --------------------------------------------------------------
class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers = {"content-length": str(len(body))}
        self._sent = 0

    def read(self, size: int) -> bytes:
        block = self._body[self._sent : self._sent + size]
        self._sent += len(block)
        return block

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def test_a_download_writes_the_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "http_stream", lambda url, timeout=300.0: FakeResponse(b"x" * 4096))
    seen: list[tuple[int, int]] = []

    path = download(
        Asset("ducky_can_shoot-0.2.3-py3-none-any.whl", "https://example.invalid/pkg.whl"),
        tmp_path,
        on_progress=lambda got, total: seen.append((got, total)),
    )

    assert path.read_bytes() == b"x" * 4096
    assert seen and seen[-1] == (4096, 4096)


def test_a_failed_download_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, timeout: float = 300.0) -> None:
        raise OSError("connection reset")

    monkeypatch.setattr(mod, "http_stream", boom)

    with pytest.raises(UpdateError, match="could not download"):
        download(Asset("pkg.whl", "https://example.invalid/pkg.whl"), tmp_path)

    assert list(tmp_path.iterdir()) == []


# -- installing ------------------------------------------------------------
class FakePopen:
    """Stands in for pip: prints two lines and exits 0."""

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.returncode = 0
        self.stdout = iter(["Processing ./pkg.whl\n", "Successfully installed ducky-can-shoot-0.2.3\n"])

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def test_installing_a_wheel_runs_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheel = tmp_path / "ducky_can_shoot-0.2.3-py3-none-any.whl"
    wheel.write_bytes(b"PK")
    calls: list[list[str]] = []

    def fake_popen(argv: list[str], **kwargs: Any) -> FakePopen:
        calls.append(argv)
        return FakePopen(argv, **kwargs)

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    lines: list[str] = []

    result = install_wheel(wheel, on_output=lines.append)

    assert result.ok is True
    assert result.needs_restart is True
    assert calls[0][1:] == ["-m", "pip", "install", "--quiet", "--upgrade", str(wheel)]
    assert any("Successfully installed" in line for line in lines)


def test_pip_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheel = tmp_path / "pkg.whl"
    wheel.write_bytes(b"PK")

    class Failing(FakePopen):
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            super().__init__(argv, **kwargs)
            self.returncode = 1
            self.stdout = iter(["ERROR: no matching distribution\n"])

    monkeypatch.setattr(mod.subprocess, "Popen", Failing)

    result = install_wheel(wheel)

    assert result.ok is False
    assert "pip exited with code 1" in result.message


def apt_manager() -> installer.PackageManager:
    return installer.PackageManager(
        id="apt", label="apt", install=(("apt-get", "install", "-y", "clamav"),)
    )


def test_the_deb_plan_uses_apt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = apt_manager()
    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: manager)
    monkeypatch.setattr(installer, "running_as_root", lambda: True)
    deb = tmp_path / "ducky-can-shoot_0.2.3-1_all.deb"

    plan = build_deb_plan(deb)

    assert plan.possible is True
    assert plan.command_lines() == [f"apt install -y {deb}"]
    assert plan.method == "root"
    assert "update installation plan" in plan.script


def test_a_non_deb_system_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = installer.PackageManager(id="dnf", label="dnf", install=(("dnf", "install", "-y", "x"),))
    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: manager)

    plan = build_deb_plan(tmp_path / "pkg.deb")

    assert plan.possible is False
    assert "ships a .deb" in plan.blocker


def test_install_deb_reports_an_impossible_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "detect_package_manager", lambda **_: None)

    result = install_deb(tmp_path / "pkg.deb")

    assert result.ok is False
    assert "package manager" in result.message


def test_install_release_refuses_an_unknown_install(monkeypatch: pytest.MonkeyPatch) -> None:
    check = UpdateCheck(
        current=CURRENT,
        release=Release.from_payload(payload()),
        target=InstallTarget("unknown", "somewhere"),
    )

    result = mod.install_release(check, Path("/tmp/pkg"))

    assert result.ok is False
    assert "cannot update it" in result.message


# -- restart ---------------------------------------------------------------
def test_the_restart_uses_the_same_entry_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launcher = tmp_path / "dcs"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(launcher), "gui"])

    assert restart_argv() == [str(launcher), "gui"]


def test_the_restart_falls_back_to_the_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["/nonexistent/__main__.py"])

    argv = restart_argv()

    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "dcs"]


# -- the command line ------------------------------------------------------
def test_the_cli_says_when_there_is_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from dcs import cli

    check = UpdateCheck(
        current=CURRENT, release=Release.from_payload(payload()), target=InstallTarget("deb")
    )
    monkeypatch.setattr(cli.updates, "check_for_update", lambda **_: check)
    monkeypatch.setattr(cli, "load_config", lambda path=None: __import__("dcs.core.config", fromlist=["AppConfig"]).AppConfig())

    args = argparse.Namespace(config=None, check=True, yes=False, json=False)
    code = cli.cmd_update(args)

    assert code == cli.EXIT_THREAT, "an update is available"
    assert "0.2.3 is available" in capsys.readouterr().out


def test_the_cli_installs_only_with_yes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import argparse

    from dcs import cli

    check = UpdateCheck(
        current="0.2.1",
        release=Release.from_payload(payload()),
        target=InstallTarget("venv", "venv at /tmp/v"),
    )
    monkeypatch.setattr(cli.updates, "check_for_update", lambda **_: check)
    monkeypatch.setattr(cli, "load_config", lambda path=None: __import__("dcs.core.config", fromlist=["AppConfig"]).AppConfig())
    installed: list[str] = []
    package = tmp_path / "ducky_can_shoot-0.2.3-py3-none-any.whl"
    package.write_bytes(b"PK")
    monkeypatch.setattr(cli.updates, "download", lambda asset, directory, **kwargs: package)
    def fake_install(check: Any, package: Path, **kwargs: Any) -> InstallResult:
        installed.append(str(package))
        return InstallResult(True, "done", needs_restart=True)

    monkeypatch.setattr(cli.updates, "install_release", fake_install)
    monkeypatch.setattr(cli.updates, "restart_watcher", lambda: "started")

    args = argparse.Namespace(config=None, check=False, yes=True, json=False)
    code = cli.cmd_update(args)

    assert code == cli.EXIT_CLEAN
    assert installed == [str(package)]
    assert "start the interface again" in capsys.readouterr().out
