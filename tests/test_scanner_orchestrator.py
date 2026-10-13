"""Tests for the scan orchestrator (dcs.core.scanner).

These tests drive the real two pass scanner over real directories; only the
engines themselves are configured (heuristic only, or the fake ClamAV front
ends), so the suite never needs a signature database or a network.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from dcs.core.config import ScanConfig
from dcs.core.events import (
    CollectingSink,
    Finding,
    Severity,
    Stage,
    StageState,
    Verdict,
    severity_rank,
)
from dcs.core.scanner import FILE_LIST_LIMIT, Scanner, compute_verdict
from dcs.core.util import walk_files

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def heuristic_only(**kwargs: object) -> ScanConfig:
    """ScanConfig that runs the built-in engine only."""
    config = ScanConfig(engines=["heuristic"], **kwargs)  # type: ignore[arg-type]
    return config


def finding(severity: Severity, path: str = "/usb/file.bin") -> Finding:
    return Finding(path=path, threat="test threat", engine="heuristic", severity=severity)


def file_count(root: Path) -> int:
    return sum(1 for _ in walk_files(root, include_hidden=True))


# ---------------------------------------------------------------------------
# engine statuses
# ---------------------------------------------------------------------------


def test_statuses_includes_the_builtin_engine() -> None:
    statuses = Scanner(heuristic_only()).statuses()
    assert len(statuses) == 1
    status = statuses[0]
    assert status.engine_id == "heuristic"
    assert status.label == "DCS built-in heuristics"
    assert status.available is True
    assert status.version == "1.0"
    assert "HID payload scripts" in status.detail


def test_statuses_reports_clamav_unavailable_without_the_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    statuses = Scanner(ScanConfig(engines=["clamav", "heuristic"])).statuses()
    assert [status.engine_id for status in statuses] == ["clamav", "heuristic"]
    clamav = statuses[0]
    assert clamav.available is False
    assert "not installed" in clamav.detail
    assert statuses[1].available is True


def test_statuses_uses_the_fake_clamav_when_it_is_on_path(fake_clamav: Path) -> None:
    statuses = Scanner(ScanConfig(engines=["clamav"])).statuses()
    assert statuses[0].available is True
    assert statuses[0].version.startswith("ClamAV 1.5.4-fake")


def test_unknown_engine_ids_are_ignored() -> None:
    scanner = Scanner(ScanConfig(engines=["clamav-ish", "heuristic"]))
    assert [status.engine_id for status in scanner.statuses()] == ["heuristic"]
    assert Scanner(ScanConfig(engines=["nope"])).statuses() == []


# ---------------------------------------------------------------------------
# a full run over the sample medium
# ---------------------------------------------------------------------------


def test_run_over_the_sample_tree_reports_every_stage(sample_tree: Path) -> None:
    sink = CollectingSink()
    result = Scanner(heuristic_only()).run(str(sample_tree), sink=sink)

    assert result.root == str(sample_tree)
    assert result.verdict is Verdict.INFECTED
    assert result.stats.files_total == file_count(sample_tree)
    assert result.stats.files_scanned == result.stats.files_total
    assert result.stats.bytes_total == sum(
        entry.size for entry in walk_files(sample_tree, include_hidden=True)
    )
    assert result.stats.errors == 0
    assert result.stats.engines == ["DCS built-in heuristics 1.0"]
    assert result.started_at and result.finished_at
    assert result.cancelled is False

    # the EICAR file must be reported by both the marker check and the hash db
    eicar = [f for f in result.findings if f.path.endswith("eicar.com")]
    threats = {f.threat for f in eicar}
    assert {"EICAR-AV-Test-File", "known malware hash: EICAR-AV-Test-File"} <= threats
    assert "executable in the root of the drive" in threats  # .com in the drive root
    critical = {"EICAR-AV-Test-File", "known malware hash: EICAR-AV-Test-File"}
    assert all(
        f.severity is Severity.CRITICAL for f in eicar if f.threat in critical
    )

    # stage events: running first, done last
    assert sink.stages[0] == (Stage.SCAN, StageState.RUNNING, "engines: DCS built-in heuristics 1.0")
    assert sink.stages[-1][0] is Stage.SCAN
    assert sink.stages[-1][1] is StageState.DONE
    assert "threatening file(s) found" in sink.stages[-1][2]

    # progress reaches exactly 100 percent
    assert sink.max_percent[Stage.SCAN] == pytest.approx(1.0)
    final = sink.progress[-1]
    assert final.state is StageState.DONE
    assert final.percent == 1.0
    assert final.processed == result.stats.files_scanned
    assert final.total == result.stats.files_total
    assert final.bytes_processed == result.stats.bytes_scanned
    assert sink.progress[0].percent < 1.0

    assert result.findings
    assert all(f.engine == "heuristic" for f in result.findings)
    assert result.to_dict()["verdict"] == "infected"


def test_run_marks_findings_with_a_sha256(sample_tree: Path) -> None:
    sink = CollectingSink()
    scanner = Scanner(heuristic_only(hash_findings=True), hash_db={})
    result = scanner.run(str(sample_tree), sink=sink)
    eicar = next(f for f in result.findings if f.threat == "EICAR-AV-Test-File")
    assert len(eicar.sha256) == 64
    assert eicar.sha256 == "275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"


def test_run_can_skip_hashing_findings(sample_tree: Path) -> None:
    scanner = Scanner(heuristic_only(hash_findings=False), hash_db={})
    result = scanner.run(str(sample_tree))
    eicar = next(f for f in result.findings if f.threat == "EICAR-AV-Test-File")
    assert eicar.sha256 == ""


def test_run_streams_findings_and_logs_to_the_sink(sample_tree: Path) -> None:
    sink = CollectingSink()
    result = Scanner(heuristic_only()).run(str(sample_tree), sink=sink)
    assert len(sink.findings) == len(result.findings)
    assert sink.logs
    assert any(level == "warning" for level, _ in sink.logs)
    assert any("scan finished: infected" in message for _, message in sink.logs)


def test_run_over_a_clean_tree_finds_nothing(clean_tree: Path) -> None:
    sink = CollectingSink()
    result = Scanner(heuristic_only()).run(str(clean_tree), sink=sink)
    assert result.verdict is Verdict.CLEAN
    assert result.findings == []
    assert result.max_severity is Severity.INFO
    assert sink.stages[-1][1] is StageState.DONE
    assert sink.stages[-1][2] == "no threats found"
    assert sink.progress[-1].percent == 1.0


def test_run_over_an_empty_directory_warns(clean_tree: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    sink = CollectingSink()
    result = Scanner(heuristic_only()).run(str(empty), sink=sink)
    assert result.verdict is Verdict.CLEAN
    assert result.stats.files_total == 0
    assert "the medium is empty or unreadable" in result.warnings
    assert any(level == "warning" for level, _ in sink.logs)


def test_run_with_the_fake_clamav_engine(tmp_path: Path, fake_clamav: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "eicar.com").write_bytes(b"X5O!P%@AP[4EICAR-STANDARD-ANTIVIRUS-TEST-FILE")
    (root / "pua-tool.txt").write_bytes(b"greyware\n")
    (root / "clean.txt").write_text("hello\n")

    sink = CollectingSink()
    result = Scanner(ScanConfig(engines=["clamav"])).run(str(root), sink=sink)

    threats = {f.threat: f.severity for f in result.findings}
    assert threats == {
        "Eicar-Test-Signature": Severity.CRITICAL,
        "PUA.Win.Tool.Fake": Severity.MEDIUM,
    }
    assert result.verdict is Verdict.INFECTED  # the EICAR file is critical
    assert result.limited_coverage is False
    assert {f.engine for f in result.findings} == {"clamav"}
    assert result.stats.engines == ["ClamAV " + result.engines[0].version]

    # a PUA alone is only suspicious, never blocking
    only_pua = tmp_path / "only-pua"
    only_pua.mkdir()
    (only_pua / "pua-tool.txt").write_bytes(b"greyware\n")
    pua = Scanner(ScanConfig(engines=["clamav"])).run(str(only_pua))
    assert pua.verdict is Verdict.SUSPICIOUS
    assert pua.infected_files == []


def test_limited_coverage_is_true_without_clamav(sample_tree: Path) -> None:
    result = Scanner(heuristic_only()).run(str(sample_tree))
    assert result.limited_coverage is True
    assert result.to_dict()["limitedCoverage"] is True


# ---------------------------------------------------------------------------
# optional file list
# ---------------------------------------------------------------------------


def test_file_list_is_empty_unless_collect_files_is_on(clean_tree: Path) -> None:
    """Collecting the file list is opt in, because it costs memory."""
    assert Scanner(heuristic_only()).run(str(clean_tree)).file_list == []
    assert Scanner(heuristic_only(), collect_files=False).run(str(clean_tree)).file_list == []
    assert FILE_LIST_LIMIT == 20000


def test_collect_files_fills_the_file_list_with_relative_paths(clean_tree: Path) -> None:
    """collect_files=True records every scanned file relative to the scan root."""
    result = Scanner(heuristic_only(), collect_files=True).run(str(clean_tree))

    expected = sorted(
        os.path.relpath(entry.path, clean_tree)
        for entry in walk_files(clean_tree, include_hidden=True)
    )
    assert sorted(result.file_list) == expected
    assert "readme.txt" in result.file_list
    assert all(not os.path.isabs(name) for name in result.file_list)
    assert result.to_dict()["fileList"] == result.file_list


def test_file_list_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A medium with more files than FILE_LIST_LIMIT fills only the first slice."""
    monkeypatch.setattr("dcs.core.scanner.FILE_LIST_LIMIT", 5)
    root = tmp_path / "many"
    root.mkdir()
    for index in range(12):
        (root / f"file{index:02d}.txt").write_text("hello\n")

    result = Scanner(heuristic_only(), collect_files=True).run(str(root))

    assert result.stats.files_scanned == 12
    assert len(result.file_list) == 5


# ---------------------------------------------------------------------------
# verdict computation
# ---------------------------------------------------------------------------


def test_compute_verdict_clean_when_there_is_nothing_to_report() -> None:
    assert compute_verdict([]) is Verdict.CLEAN
    assert compute_verdict([finding(Severity.INFO), finding(Severity.LOW)]) is Verdict.CLEAN


def test_compute_verdict_suspicious_for_medium_only() -> None:
    assert compute_verdict([finding(Severity.MEDIUM)]) is Verdict.SUSPICIOUS
    assert compute_verdict([finding(Severity.LOW), finding(Severity.MEDIUM)]) is Verdict.SUSPICIOUS


def test_compute_verdict_infected_for_high_and_critical() -> None:
    assert compute_verdict([finding(Severity.HIGH)]) is Verdict.INFECTED
    assert compute_verdict([finding(Severity.CRITICAL)]) is Verdict.INFECTED


def test_compute_verdict_honours_blocking_severity() -> None:
    high = [finding(Severity.HIGH)]
    assert compute_verdict(high, blocking_severity="critical") is Verdict.SUSPICIOUS
    assert compute_verdict(high, blocking_severity="high") is Verdict.INFECTED
    # a low threshold can never lower the bar below high
    assert compute_verdict(high, blocking_severity="low") is Verdict.INFECTED
    assert compute_verdict([finding(Severity.CRITICAL)], blocking_severity="critical") is Verdict.INFECTED
    assert compute_verdict([finding(Severity.MEDIUM)], blocking_severity="critical") is Verdict.SUSPICIOUS


def test_compute_verdict_error_and_cancelled() -> None:
    assert compute_verdict([], errors=3, files_scanned=0) is Verdict.ERROR
    # errors alone do not mask a real scan that produced files
    assert compute_verdict([], errors=3, files_scanned=10) is Verdict.CLEAN
    assert compute_verdict([finding(Severity.CRITICAL)], cancelled=True) is Verdict.CANCELLED
    assert compute_verdict([], errors=1, files_scanned=0, cancelled=True) is Verdict.CANCELLED


def test_blocking_severity_from_the_config_is_used(tmp_path: Path, sample_builder: object) -> None:
    medium = tmp_path / "medium"
    medium.mkdir()
    # a double extension executable: HIGH masquerading plus MEDIUM placement
    (medium / "invoice.pdf.exe").write_bytes(sample_builder.pe(size=2048))  # type: ignore[attr-defined]

    default = Scanner(heuristic_only()).run(str(medium))
    assert default.verdict is Verdict.INFECTED
    assert default.max_severity is Severity.HIGH

    strict = Scanner(heuristic_only(blocking_severity="critical")).run(str(medium))
    assert strict.verdict is Verdict.SUSPICIOUS
    assert strict.max_severity is Severity.HIGH
    assert len(strict.infected_files) == 1  # still reported, just not blocking


# ---------------------------------------------------------------------------
# cancellation, limits and the no-engine path
# ---------------------------------------------------------------------------


def test_run_stops_immediately_when_cancelled_before_the_scan(sample_tree: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    sink = CollectingSink()
    result = Scanner(heuristic_only()).run(str(sample_tree), sink=sink, cancel=cancel)

    assert result.cancelled is True
    assert result.verdict is Verdict.CANCELLED
    assert result.stats.files_scanned == 0
    assert result.findings == []
    assert sink.stages[-1] == (Stage.SCAN, StageState.CANCELLED, "scan cancelled")
    assert sink.progress[-1].state is StageState.CANCELLED
    assert sink.progress[-1].percent == 0.0


def test_cancellation_raised_mid_scan_is_honoured(sample_tree: Path) -> None:
    cancel = threading.Event()
    sink = CollectingSink()
    original = sink.on_finding

    def stop_after_first(found: Finding) -> None:
        original(found)
        cancel.set()

    sink.on_finding = stop_after_first  # type: ignore[method-assign]
    result = Scanner(heuristic_only()).run(str(sample_tree), sink=sink, cancel=cancel)

    assert result.cancelled is True
    assert result.verdict is Verdict.CANCELLED
    assert 0 < len(result.findings) < 20
    assert sink.stages[-1][1] is StageState.CANCELLED


def test_max_findings_stops_the_scan(sample_tree: Path) -> None:
    sink = CollectingSink()
    result = Scanner(heuristic_only(max_findings=3)).run(str(sample_tree), sink=sink)
    assert len(result.findings) == 3
    assert any("stopped after 3 findings" in warning for warning in result.warnings)
    assert result.cancelled is True
    assert result.verdict is Verdict.CANCELLED


def test_max_file_size_mb_skips_large_files(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    big = root / "big-eicar.bin"
    big.write_bytes(b"X5O!P%@AP[4" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" + b"\x00" * (2 * 1024 * 1024))
    small = root / "small.txt"
    small.write_bytes(b"just a normal file\n")

    result = Scanner(heuristic_only(max_file_size_mb=1)).run(str(root))

    assert result.skipped_large == [(str(big), big.stat().st_size)]
    assert result.stats.files_skipped == 1
    assert result.findings == []
    assert result.verdict is Verdict.CLEAN
    assert result.stats.files_total == 2
    assert result.to_dict()["skippedLarge"][0]["path"] == str(big)


def test_max_files_limits_the_walk(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    for index in range(5):
        (root / f"file{index}.txt").write_text("nothing\n")
    result = Scanner(heuristic_only(max_files=2)).run(str(root))
    assert result.stats.files_total == 2


def test_run_without_a_usable_engine_fails_the_scan_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    root = tmp_path / "media"
    root.mkdir()
    (root / "readme.txt").write_text("hello\n")
    sink = CollectingSink()

    result = Scanner(ScanConfig(engines=["clamav"])).run(str(root), sink=sink)

    assert result.verdict is Verdict.ERROR
    assert result.stats.files_scanned == 0
    assert result.stats.files_total == 0
    assert result.findings == []
    assert "no usable scan engine: the drive cannot be scanned" in result.warnings
    assert sink.stages == [
        (Stage.SCAN, StageState.FAILED, "no usable scan engine: the drive cannot be scanned")
    ]
    assert any(level == "error" for level, _ in sink.logs)
    assert result.engines and result.engines[0].available is False
    assert any(warning.startswith("ClamAV unavailable:") for warning in result.warnings)


def test_run_with_an_unknown_engine_id_fails_the_scan_stage(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "readme.txt").write_text("hello\n")
    result = Scanner(ScanConfig(engines=["bogus"])).run(str(root))
    assert result.verdict is Verdict.ERROR
    assert result.engines == []


def test_engine_status_dicts_are_exposed_in_the_result(sample_tree: Path) -> None:
    result = Scanner(heuristic_only()).run(str(sample_tree))
    payload = result.to_dict()
    assert payload["engines"][0]["id"] == "heuristic"
    assert payload["engines"][0]["available"] is True
    assert payload["stats"]["filesScanned"] == payload["stats"]["filesTotal"]
    assert payload["maxSeverity"] == "critical"
    assert payload["infectedCount"] >= 1
    assert severity_rank(payload["maxSeverity"]) == severity_rank(Severity.CRITICAL)
    assert payload["cancelled"] is False
    assert payload["unreadableCount"] == 0
