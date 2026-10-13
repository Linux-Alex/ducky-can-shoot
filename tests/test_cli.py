"""CLI tests: the commands, their exit codes and their output streams.

main() is called in process, so stdout is captured with capsys.  Every test
passes --config with a file inside tmp_path (or relies on the XDG redirection
of the autouse fixture), so nothing is written into the developer's home.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

import dcs.cli
from dcs.cli import exit_code_for, main
from dcs.core.device import BlockDevice
from dcs.version import __version__

PASSWORD = "hunter2"


def write_config(tmp_path: Path, *, email_extra: str = "") -> Path:
    """Write a self contained config file whose paths all live in tmp_path."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[general]\n"
        f'output_dir = "{tmp_path / "out"}"\n'
        "\n"
        "[report]\n"
        f'directory = "{tmp_path / "reports"}"\n'
        "\n"
        "[email]\n"
        "enabled = true\n"
        'smtp_host = "smtp.example.test"\n'
        "smtp_port = 587\n"
        'from_addr = "dcs@example.test"\n'
        'to_addrs = ["ops@example.test"]\n'
        'dry_run_dir = "' + str(tmp_path / "outbox") + '"\n'
        f"{email_extra}",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """A valid configuration file that keeps every write inside tmp_path."""
    return write_config(tmp_path)


@pytest.fixture
def stick() -> BlockDevice:
    """One mounted, removable USB partition as lsblk would report it."""
    return BlockDevice(
        name="sdb1",
        path="/dev/sdb1",
        type="part",
        size=7_600_000_000,
        fstype="vfat",
        label="HOLIDAY",
        uuid="1234-ABCD",
        mountpoints=["/media/aleks/HOLIDAY"],
        transport="usb",
        removable=True,
        hotplug=True,
        model="Ultra Fit",
        vendor="SanDisk",
    )


@pytest.fixture
def infected_tree(tmp_path: Path, sample_builder: Any) -> Path:
    """A two file medium whose only interesting file is the EICAR test pattern."""
    root = tmp_path / "usb"
    root.mkdir()
    sample_builder.write(root, "eicar.com", sample_builder.eicar())
    sample_builder.write(root, "notes.txt", "A harmless note next to the test file.\n")
    return root


def scan_argv(config: Path, path: Path, *extra: str) -> list[str]:
    """Build a scan invocation that never needs ClamAV."""
    return ["--config", str(config), "scan", str(path), "--engines", "heuristic", *extra]


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


def test_providers_lists_every_registry_id_in_order(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """dcs providers prints exactly the ids provider_ids() advertises, in order."""
    from dcs.core.upload import provider_catalog, provider_ids

    code = main(["--config", str(config_file), "providers"])

    assert code == 0
    output = capsys.readouterr().out
    printed = [
        line.split()[0]
        for line in output.splitlines()
        if line.strip() and not line.startswith(" ")
    ]
    assert printed == provider_ids()
    assert len(printed) == 7
    assert "0x0" not in printed
    for entry in provider_catalog():
        assert entry["label"] in output, entry["id"]
        assert "API key required" if entry["requiresKey"] else "no account needed" in output


def test_providers_json_matches_the_catalog(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The JSON form carries the same entries as provider_catalog()."""
    from dcs.core.upload import provider_catalog

    code = main(["--config", str(config_file), "providers", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == provider_catalog()
    assert [entry["id"] for entry in payload] == [entry["id"] for entry in provider_catalog()]


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------


def test_devices_without_medium(
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty device list is reported in plain words and exits cleanly."""
    monkeypatch.setattr(dcs.cli, "list_removable_devices", lambda **kwargs: [])

    code = main(["--config", str(config_file), "devices"])
    captured = capsys.readouterr()

    assert code == 0
    assert "No removable medium detected" in captured.out
    assert captured.err == ""


def test_devices_table_lists_node_label_and_mount(
    config_file: Path,
    stick: BlockDevice,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The table shows the node, the label, the size and the mount point."""
    monkeypatch.setattr(dcs.cli, "list_removable_devices", lambda **kwargs: [stick])

    code = main(["--config", str(config_file), "devices"])
    lines = capsys.readouterr().out.splitlines()

    assert code == 0
    assert lines[0].startswith("NODE")
    assert "LABEL" in lines[0]
    assert "/dev/sdb1" in lines[1]
    assert "HOLIDAY" in lines[1]
    assert "7.1 GiB" in lines[1]
    assert "vfat" in lines[1]
    assert "USB" in lines[1]
    assert "/media/aleks/HOLIDAY" in lines[1]
    assert len(lines) == 2


def test_devices_json(
    config_file: Path,
    stick: BlockDevice,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json prints the device dictionaries, one per detected medium."""
    monkeypatch.setattr(dcs.cli, "list_removable_devices", lambda **kwargs: [stick])

    code = main(["--config", str(config_file), "devices", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert isinstance(payload, list)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["path"] == "/dev/sdb1"
    assert entry["label"] == "HOLIDAY"
    assert entry["displayName"] == "HOLIDAY"
    assert entry["mountPath"] == "/media/aleks/HOLIDAY"
    assert entry["mountpoints"] == ["/media/aleks/HOLIDAY"]
    assert entry["size"] == 7_600_000_000
    assert entry["sizeText"] == "7.1 GiB"
    assert entry["removable"] is True
    assert entry["scannable"] is True


def test_devices_json_without_medium(
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json with nothing plugged in is an empty list, not a sentence."""
    monkeypatch.setattr(dcs.cli, "list_removable_devices", lambda **kwargs: [])

    code = main(["--config", str(config_file), "devices", "--json"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def test_scan_json_reports_an_infected_medium(
    config_file: Path,
    infected_tree: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An infected medium exits with 1 and carries its findings in the JSON."""
    code = main(scan_argv(config_file, infected_tree, "--json"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["verdict"] == "infected"
    assert payload["maxSeverity"] == "critical"
    assert isinstance(payload["findings"], list) and payload["findings"]

    threats = {finding["threat"] for finding in payload["findings"]}
    assert "EICAR-AV-Test-File" in threats
    assert all(finding["path"].endswith("eicar.com") for finding in payload["findings"])
    assert all(finding["engine"] == "heuristic" for finding in payload["findings"])
    assert all(finding["detail"] for finding in payload["findings"])
    assert max(finding["severityRank"] for finding in payload["findings"]) == 4
    assert payload["infectedCount"] >= 1
    assert payload["stats"]["filesScanned"] == 2
    assert payload["stats"]["bytesScanned"] > 0
    assert payload["limitedCoverage"] is True
    assert [engine["id"] for engine in payload["engines"]] == ["heuristic"]
    assert payload["root"] == str(infected_tree)


def test_scan_json_reports_a_clean_medium(
    config_file: Path,
    clean_tree: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean medium exits with 0 and has no findings at all."""
    code = main(scan_argv(config_file, clean_tree, "--json"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["verdict"] == "clean"
    assert payload["infectedCount"] == 0
    assert payload["maxSeverity"] == "info"
    assert payload["findings"] == []
    assert payload["findingCount"] == 0
    assert payload["stats"]["filesScanned"] > 0
    assert payload["limitedCoverage"] is True


def test_scan_prints_findings_on_stderr(
    config_file: Path,
    infected_tree: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The human readable run prints the findings to stderr, the verdict to stdout."""
    code = main(scan_argv(config_file, infected_tree))
    captured = capsys.readouterr()

    assert code == 1
    assert "VERDICT: INFECTED" in captured.out
    assert "finding(s) in 2 files" in captured.out
    assert "EICAR-AV-Test-File" in captured.err
    assert "eicar.com" in captured.err
    assert "EICAR" not in captured.out


def test_scan_quiet_suppresses_the_finding_lines(
    config_file: Path,
    infected_tree: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--quiet keeps every finding line off both streams but keeps the exit code."""
    code = main(scan_argv(config_file, infected_tree, "--quiet"))
    captured = capsys.readouterr()

    assert code == 1
    assert "VERDICT: INFECTED" in captured.out
    assert "EICAR" not in captured.out
    assert "EICAR" not in captured.err
    assert "eicar.com" not in captured.err


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_path_prints_the_selected_file(
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """config path echoes exactly the file given with --config."""
    code = main(["--config", str(config_file), "config", "path"])

    assert code == 0
    assert capsys.readouterr().out.strip() == str(config_file)


def test_config_show_masks_the_password_by_default(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default dump is valid TOML with every secret masked."""
    config_file = write_config(tmp_path, email_extra=f'password = "{PASSWORD}"\n')

    code = main(["--config", str(config_file), "config", "show"])
    output = capsys.readouterr().out
    parsed = tomllib.loads(output)

    assert code == 0
    assert PASSWORD not in output
    assert parsed["email"]["password"] == "***"
    assert parsed["email"]["smtp_host"] == "smtp.example.test"
    assert parsed["email"]["to_addrs"] == ["ops@example.test"]
    assert parsed["general"]["output_dir"] == str(tmp_path / "out")
    assert parsed["report"]["directory"] == str(tmp_path / "reports")
    assert parsed["scan"]["engines"] == ["clamav", "heuristic"]
    assert parsed["email"]["enabled"] is True


def test_config_show_secrets_reveals_the_password(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--show-secrets prints the literal password from the file."""
    config_file = write_config(tmp_path, email_extra=f'password = "{PASSWORD}"\n')

    code = main(["--config", str(config_file), "config", "show", "--show-secrets"])
    output = capsys.readouterr().out
    parsed = tomllib.loads(output)

    assert code == 0
    assert PASSWORD in output
    assert parsed["email"]["password"] == PASSWORD


# ---------------------------------------------------------------------------
# doctor and version
# ---------------------------------------------------------------------------


def check_names(output: str) -> list[str]:
    """The check names of a doctor --json run."""
    return [entry["check"] for entry in json.loads(output)]


def test_doctor_json_checks_have_the_documented_shape(
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every doctor check is a dict with check, ok and detail."""
    code = main(["--config", str(config_file), "doctor", "--offline", "--json"])
    checks = json.loads(capsys.readouterr().out)

    # ClamAV is deliberately absent on this machine, so 1 is the expected code
    assert code in (0, 1)
    assert checks
    for entry in checks:
        assert isinstance(entry, dict)
        assert set(entry) >= {"check", "ok", "detail"}
        assert isinstance(entry["check"], str) and entry["check"]
        assert isinstance(entry["ok"], bool)
        assert isinstance(entry["detail"], str)

    names = [entry["check"] for entry in checks]
    assert "python" in names
    assert "config file" in names
    assert "engine clamav" in names
    assert "engine heuristic" in names
    assert "upload provider" not in names, "--offline must skip the network check"


def test_doctor_text_output_mentions_every_check(
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The table shows one line per check, including every scan engine."""
    main(["--config", str(config_file), "doctor", "--offline", "--json"])
    names = check_names(capsys.readouterr().out)

    code = main(["--config", str(config_file), "doctor", "--offline"])
    output = capsys.readouterr().out

    assert code in (0, 1)
    assert "doctor" in output
    assert [name for name in names if name not in output] == []
    assert "python" in output
    assert str(config_file) in output
    # one line per configured scan engine, with its state on that line
    engine_lines = [line for line in output.splitlines() if "engine " in line]
    assert len(engine_lines) == 2
    # the engine may or may not be installed on the machine running the suite,
    # so only the presence of a state marker is contractual
    assert any(
        "engine clamav" in line and ("FAIL" in line or " ok " in line) for line in engine_lines
    )
    assert any("engine heuristic" in line and "ok" in line for line in engine_lines)


def test_version_prints_the_package_version(
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """version prints the installed version, in text and in JSON."""
    code = main(["--config", str(config_file), "version"])
    output = capsys.readouterr().out

    assert code == 0
    assert __version__ in output

    code = main(["--config", str(config_file), "version", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["version"] == __version__
    assert "license" in payload


# ---------------------------------------------------------------------------
# exit codes
# ---------------------------------------------------------------------------


def test_scan_exit_codes_follow_the_verdict(
    config_file: Path,
    clean_tree: Path,
    infected_tree: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0 clean, 1 infected, 2 when the path cannot be scanned."""
    assert main(scan_argv(config_file, clean_tree, "--json")) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "clean"

    assert main(scan_argv(config_file, infected_tree, "--json")) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "infected"

    missing = tmp_path / "definitely-not-here"
    code = main(scan_argv(config_file, missing, "--json"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["verdict"] == "error"
    assert payload["stats"]["filesScanned"] == 0
    assert payload["findings"] == []
    assert payload["unreadableCount"] >= 1


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("clean", 0),
        ("suspicious", 1),
        ("infected", 1),
        ("error", 2),
        ("cancelled", 2),
        ("something-else", 2),
    ],
)
def test_exit_code_for(verdict: str, expected: int) -> None:
    """The exit code table of the CLI does not change by accident."""
    assert exit_code_for(verdict) == expected


def test_unknown_subcommand_is_rejected(
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """argparse refuses an unknown command instead of silently starting the GUI."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--config", str(config_file), "frobnicate"])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err
    assert "frobnicate" in captured.err
