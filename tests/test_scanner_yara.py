"""The YARA engine: rule discovery, match parsing and the verdict it drives.

The binary under test is tests/fakes/yara, which mirrors the command line
surface the engine builds and derives its matches from file names.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from dcs.core.config import ScanConfig, YaraConfig
from dcs.core.events import Severity, Verdict
from dcs.core.scanner import Scanner
from dcs.core.scanner.yara import YaraEngine, probe, rule_files

REPO = Path(__file__).resolve().parent.parent
FAKES = REPO / "tests" / "fakes"


@pytest.fixture
def fake_yara(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put tests/fakes first on PATH so "yara" resolves to the fake."""
    binary = FAKES / "yara"
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(FAKES) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("DCS_FAKE_YARA_SLEEP", raising=False)
    return binary


@pytest.fixture
def rules(tmp_path: Path) -> Path:
    folder = tmp_path / "rules"
    folder.mkdir()
    (folder / "dcs.yar").write_text("rule Duckyscript_Payload { condition: true }\n")
    return folder


def yara_config(rules: Path, **overrides: object) -> YaraConfig:
    values: dict[str, object] = {"enabled": True, "rules": [str(rules)]}
    values.update(overrides)
    return YaraConfig(**values)  # type: ignore[arg-type]


def scan(root: Path, config: ScanConfig) -> tuple[Verdict, list]:
    result = Scanner(config).run(str(root))
    return result.verdict, result.findings


# -- discovery ------------------------------------------------------------
def test_rule_directories_are_expanded_recursively(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.yar").write_text("rule a { condition: true }")
    (tmp_path / "nested" / "b.yara").write_text("rule b { condition: true }")
    (tmp_path / "nested" / "notes.txt").write_text("not a rule")

    files = rule_files(yara_config(tmp_path))

    assert [Path(item).name for item in files] == ["a.yar", "b.yara"]


def test_a_single_rule_file_is_accepted(tmp_path: Path) -> None:
    single = tmp_path / "one.yar"
    single.write_text("rule one { condition: true }")

    assert rule_files(YaraConfig(rules=[str(single)])) == [str(single)]


def test_without_a_binary_the_engine_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent/bin")

    result = probe(YaraConfig(binary="yara"))

    assert result.usable is False
    assert "not installed" in result.problem


def test_yara_x_is_reported_instead_of_run_blindly(monkeypatch: pytest.MonkeyPatch) -> None:
    """yr has its own command line; running it with yara's flags would report
    an empty pass as coverage, which is worse than saying what is missing."""
    fake = FAKES / "yara-x-would-be"
    fake.write_text("#!/bin/sh\necho 'YARA-X 1.0.0'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(FAKES) + os.pathsep + "/usr/bin:/bin")
    monkeypatch.setattr("dcs.core.scanner.yara.which", lambda name: str(fake) if name == "yr" else None)

    result = probe(YaraConfig())

    assert result.usable is False
    assert "only YARA-X" in result.problem


def test_without_rules_the_engine_is_unavailable(
    fake_yara: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(FAKES) + os.pathsep + "/usr/bin:/bin")

    result = probe(YaraConfig())  # no rules configured, no default rule dir

    assert result.usable is False
    assert "no YARA rules" in result.problem


# -- scanning -------------------------------------------------------------
def test_a_match_becomes_a_finding(fake_yara: Path, rules: Path, tmp_path: Path) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "ducky payload.txt").write_text("STRING powershell -enc AAA")
    (medium / "holiday.txt").write_text("nothing to see")

    config = ScanConfig(engines=["yara"], yara=yara_config(rules))
    verdict, findings = scan(medium, config)

    assert [finding.threat for finding in findings] == ["Duckyscript_Payload"]
    assert findings[0].engine == "yara"
    assert findings[0].category == "yara"
    assert findings[0].severity is Severity.CRITICAL
    assert "tags: malware, hid" in findings[0].detail
    assert verdict is Verdict.INFECTED


def test_the_verdict_stays_clean_without_a_match(fake_yara: Path, rules: Path, tmp_path: Path) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "holiday photos").mkdir()
    (medium / "holiday photos" / "beach.txt").write_text("sun")

    verdict, findings = scan(medium, ScanConfig(engines=["yara"], yara=yara_config(rules)))

    assert findings == []
    assert verdict is Verdict.CLEAN


def test_a_path_with_spaces_is_parsed(fake_yara: Path, rules: Path, tmp_path: Path) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    target = medium / "my ducky payload.txt"
    target.write_text("GUI r")

    _, findings = scan(medium, ScanConfig(engines=["yara"], yara=yara_config(rules)))

    assert [finding.path for finding in findings] == [str(target)]


def test_the_namespace_and_the_tags_are_recorded(
    fake_yara: Path, rules: Path, tmp_path: Path
) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "suspicious script.txt").write_text("IEX")

    _, findings = scan(medium, ScanConfig(engines=["yara"], yara=yara_config(rules)))

    assert findings[0].threat == "Suspicious_PowerShell"
    assert findings[0].severity is Severity.MEDIUM  # the tag wins over the default
    assert "namespace: dcs" in findings[0].detail
    assert "tags: suspicious" in findings[0].detail


def test_an_informational_tag_keeps_the_verdict_clean(
    fake_yara: Path, rules: Path, tmp_path: Path
) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "info binary.dll").write_text("MZ")

    verdict, findings = scan(medium, ScanConfig(engines=["yara"], yara=yara_config(rules)))

    assert findings[0].severity is Severity.INFO
    assert verdict is Verdict.CLEAN


def test_the_configured_severity_is_the_default(fake_yara: Path, rules: Path, tmp_path: Path) -> None:
    """A rule without a severity tag is as bad as the configuration says."""
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "ducky.txt").write_text("STRING")

    engine = YaraEngine(yara_config(rules, severity="medium"))
    assert engine._severity([]) is Severity.MEDIUM
    engine_high = YaraEngine(yara_config(rules))
    assert engine_high._severity([]) is Severity.HIGH


def test_a_rule_error_is_reported_and_does_not_stop_the_scan(
    fake_yara: Path, rules: Path, tmp_path: Path
) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "badrule.yar").write_text("rule broken")
    (medium / "ducky.txt").write_text("STRING")

    result = Scanner(ScanConfig(engines=["yara"], yara=yara_config(rules))).run(str(medium))

    # the fake exits 1 to imitate a compile error, but the good file still matched
    assert [finding.threat for finding in result.findings] == ["Duckyscript_Payload"]
    assert any("did not compile" in warning for warning in result.warnings), result.warnings
    assert result.stats.errors >= 1


def test_cancelling_stops_the_pass(fake_yara: Path, rules: Path, tmp_path: Path) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    for index in range(40):
        (medium / f"ducky-{index}.txt").write_text("STRING")

    engine = YaraEngine(yara_config(rules, ))
    ctx = make_context(medium)
    cancel = threading.Event()
    ctx.cancel = cancel
    os.environ["DCS_FAKE_YARA_SLEEP"] = "0.05"
    try:
        findings = []
        for finding in engine.scan_tree(str(medium), ctx):
            findings.append(finding)
            if len(findings) >= 2:
                cancel.set()
    finally:
        del os.environ["DCS_FAKE_YARA_SLEEP"]

    assert 2 <= len(findings) < 40


def test_the_engine_reports_what_it_loaded(fake_yara: Path, rules: Path) -> None:
    status = YaraEngine(yara_config(rules)).status()

    assert status.available is True
    assert status.version.startswith("4.5.1")
    assert "1 rule file" in status.detail


def make_context(root: Path):
    from dcs.core.scanner.base import ScanContext

    return ScanContext(root=str(root), config=ScanConfig())


def test_a_single_rule_file_is_flagged_as_thin_coverage(fake_yara: Path, rules: Path) -> None:
    status = YaraEngine(yara_config(rules)).status()

    assert any("only one rule file" in warning for warning in status.warnings)


def test_yara_runs_beside_the_batch_engines(fake_yara: Path, rules: Path, tmp_path: Path) -> None:
    """The heuristic engine ticks the progress bar while YARA finds matches."""
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "ducky payload.txt").write_text("STRING powershell -enc AAA")
    (medium / "holiday.txt").write_text("sun")

    config = ScanConfig(engines=["heuristic", "yara"], yara=yara_config(rules))
    result = Scanner(config).run(str(medium))

    threats = {finding.threat for finding in result.findings}
    assert "Duckyscript_Payload" in threats
    assert any(finding.engine == "heuristic" for finding in result.findings)
    assert result.stats.files_scanned == 2
    assert any(name.startswith("YARA rules") for name in result.stats.engines)


def test_a_missing_binary_is_skipped_with_a_warning(tmp_path: Path) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "holiday.txt").write_text("sun")

    config = ScanConfig(
        engines=["heuristic", "yara"],
        yara=YaraConfig(enabled=True, binary="/nonexistent/yara", rules=["/nonexistent.yar"]),
    )
    result = Scanner(config).run(str(medium))

    assert result.verdict is Verdict.CLEAN
    assert any("YARA rules unavailable" in warning for warning in result.warnings)
