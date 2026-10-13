"""Tests for the ClamAV front end (dcs.core.scanner.clamav).

Real ClamAV is not installed on this machine on purpose: every test drives the
fake front ends in tests/fakes, which answer from the file names they are
handed.  That keeps the suite hermetic while still exercising the real parsing,
probing and error accounting code paths.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from dcs.core.config import ClamAVConfig
from dcs.core.events import Severity
from dcs.core.scanner.base import EngineStatus, ScanContext
from dcs.core.scanner.clamav import (
    ClamAVEngine,
    ClamAVProbe,
    DatabaseInfo,
    clamav_status_line,
    database_warning,
    probe,
)
from dcs.core.util import FileEntry

FAKE_VERSION = "ClamAV 1.5.4-fake/27000/Tue Jan  1 00:00:00 2026"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_entry(path: Path, root: Path) -> FileEntry:
    """Build the FileEntry the walker would produce for *path*."""
    info = path.stat()
    return FileEntry(
        path=str(path),
        size=int(info.st_size),
        relative=os.path.relpath(path, root),
        mtime=float(info.st_mtime),
    )


@pytest.fixture
def media(tmp_path: Path) -> Path:
    """A tiny tree with one file per fake ClamAV outcome."""
    root = tmp_path / "media"
    root.mkdir()
    (root / "eicar.com").write_bytes(b"X5O!P%@AP[4" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE")
    (root / "pua-tool.txt").write_bytes(b"potentially unwanted application\n")
    (root / "lockme.bin").write_bytes(b"access denied on read\n")
    (root / "clean.txt").write_bytes(b"nothing to see here\n")
    return root


@pytest.fixture
def ticks() -> list[tuple[int, str]]:
    return []


@pytest.fixture
def context(media: Path, ticks: list[tuple[int, str]]) -> ScanContext:
    """ScanContext whose tick callback records every processed file."""
    def tick(size: int, path: str) -> None:
        ticks.append((size, path))

    return ScanContext(root=str(media), config=ClamAVConfig(), tick=tick)


def collect(engine: ClamAVEngine, entries: list[FileEntry], context: ScanContext) -> list[Any]:
    return list(engine.scan_batch(entries, context))


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_prefers_the_daemon_when_it_pings(fake_clamav: Path) -> None:
    result = probe(ClamAVConfig())
    assert result.usable is True
    assert result.mode == "clamdscan"
    assert result.daemon_ok is True
    assert result.binary == str(fake_clamav / "clamdscan")
    assert result.version == FAKE_VERSION
    assert result.problem == ""


def test_probe_falls_back_to_clamscan_when_the_daemon_is_down(
    fake_clamav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DCS_FAKE_CLAMD_DOWN", "1")
    result = probe(ClamAVConfig())
    assert result.usable is True
    assert result.mode == "clamscan"
    assert result.daemon_ok is False
    assert result.binary == str(fake_clamav / "clamscan")
    assert result.version == FAKE_VERSION
    assert "Could not connect to clamd" in result.problem


def test_probe_ignores_the_daemon_when_prefer_daemon_is_false(fake_clamav: Path) -> None:
    result = probe(ClamAVConfig(prefer_daemon=False))
    assert result.mode == "clamscan"
    assert result.daemon_ok is False


def test_probe_without_any_binary_is_unavailable_with_a_helpful_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = probe(ClamAVConfig())
    assert result.usable is False
    assert result.binary == ""
    assert result.mode == ""
    assert "not installed" in result.problem
    assert "freshclam" in result.problem

    status = ClamAVEngine(ClamAVConfig()).status()
    assert isinstance(status, EngineStatus)
    assert status.available is False
    assert status.engine_id == "clamav"
    assert status.label == "ClamAV"
    assert status.detail == result.problem


def test_status_warns_about_the_slow_standalone_scanner(
    fake_clamav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DCS_FAKE_CLAMD_DOWN", "1")
    monkeypatch.setattr(
        "dcs.core.scanner.clamav._newest_database",
        lambda: DatabaseInfo(path="/var/lib/clamav/main.cvd", modified=time.time()),
    )
    status = ClamAVEngine(ClamAVConfig()).status()
    assert status.available is True
    assert status.version == FAKE_VERSION
    assert any("reloads its database" in warning for warning in status.warnings)
    assert status.warnings


def test_status_reports_a_stale_signature_database(
    fake_clamav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = time.time() - 30 * 86400
    monkeypatch.setattr(
        "dcs.core.scanner.clamav._newest_database",
        lambda: DatabaseInfo(path="/var/lib/clamav/main.cvd", modified=old),
    )
    status = ClamAVEngine(ClamAVConfig(max_database_age_days=7)).status()
    assert any("30 days old" in warning for warning in status.warnings)


def test_database_warning_and_age() -> None:
    assert database_warning(ClamAVConfig(), DatabaseInfo()) == (
        "ClamAV signature database not found; install it with freshclam"
    )
    assert DatabaseInfo().age_days == -1.0
    fresh = DatabaseInfo(path="/x/main.cvd", modified=time.time())
    assert fresh.age_days == pytest.approx(0.0, abs=0.01)
    assert database_warning(ClamAVConfig(max_database_age_days=7), fresh) == ""
    assert fresh.to_dict()["newestSignature"] == "/x/main.cvd"


def test_clamav_status_line_for_both_states(fake_clamav: Path) -> None:
    usable = probe(ClamAVConfig())
    line = clamav_status_line(usable)
    assert line.startswith("ClamAV: clamdscan ")
    unavailable = clamav_status_line(ClamAVProbe(problem="ClamAV is not installed"))
    assert unavailable == "ClamAV: not available (ClamAV is not installed)"


# ---------------------------------------------------------------------------
# command building
# ---------------------------------------------------------------------------


def test_build_command_uses_fdpass_for_the_daemon(fake_clamav: Path) -> None:
    engine = ClamAVEngine(ClamAVConfig(extra_args=["--alert-exceeds-max=yes"]))
    argv = engine.build_command("/tmp/list.lst")
    assert argv[0].endswith("clamdscan")
    assert "--fdpass" in argv
    assert "--file-list" in argv and "/tmp/list.lst" in argv
    assert argv[-1] == "--alert-exceeds-max=yes"


def test_build_command_without_fdpass_for_the_standalone_scanner(
    fake_clamav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DCS_FAKE_CLAMD_DOWN", "1")
    argv = ClamAVEngine(ClamAVConfig()).build_command("/tmp/list.lst")
    assert argv[0].endswith("clamscan")
    assert "--fdpass" not in argv
    assert "--verbose" in argv and "--no-summary" in argv


# ---------------------------------------------------------------------------
# line parsing and severity mapping
# ---------------------------------------------------------------------------


def test_found_ok_and_error_lines_are_parsed(
    fake_clamav: Path, media: Path, context: ScanContext, ticks: list[tuple[int, str]]
) -> None:
    engine = ClamAVEngine(ClamAVConfig())
    entries = [
        make_entry(media / "eicar.com", media),
        make_entry(media / "clean.txt", media),
        make_entry(media / "lockme.bin", media),
    ]
    findings = collect(engine, entries, context)

    assert [finding.path for finding in findings] == [str(media / "eicar.com")]
    finding = findings[0]
    assert finding.threat == "Eicar-Test-Signature"
    assert finding.severity is Severity.CRITICAL
    assert finding.category == "antivirus"
    assert finding.engine == "clamav"
    assert finding.size == (media / "eicar.com").stat().st_size
    assert "via clamdscan" in finding.detail
    assert FAKE_VERSION in finding.detail

    # every file was ticked exactly once, including the one that failed to open
    assert sorted(path for _, path in ticks) == sorted(entry.path for entry in entries)
    assert {size for size, _ in ticks} == {entry.size for entry in entries}


def test_errors_are_counted_on_the_engine(
    fake_clamav: Path, media: Path, context: ScanContext
) -> None:
    engine = ClamAVEngine(ClamAVConfig())
    findings = collect(engine, [make_entry(media / "lockme.bin", media)], context)
    assert findings == []
    assert len(engine.errors) == 1
    assert "Access denied" in engine.errors[0]
    assert str(media / "lockme.bin") in engine.errors[0]


def test_pua_signature_is_only_medium(
    fake_clamav: Path, media: Path, context: ScanContext
) -> None:
    engine = ClamAVEngine(ClamAVConfig())
    findings = collect(engine, [make_entry(media / "pua-tool.txt", media)], context)
    assert len(findings) == 1
    assert findings[0].threat == "PUA.Win.Tool.Fake"
    assert findings[0].severity is Severity.MEDIUM
    assert engine.errors == []


def test_normal_signature_next_to_a_pua_signature(
    fake_clamav: Path, media: Path, context: ScanContext
) -> None:
    engine = ClamAVEngine(ClamAVConfig())
    entries = [
        make_entry(media / "eicar.com", media),
        make_entry(media / "pua-tool.txt", media),
    ]
    findings = {finding.threat: finding.severity for finding in collect(engine, entries, context)}
    assert findings == {
        "Eicar-Test-Signature": Severity.CRITICAL,
        "PUA.Win.Tool.Fake": Severity.MEDIUM,
    }


def test_engine_errors_surface_when_the_binary_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "not-a-binary"
    engine = ClamAVEngine(ClamAVConfig())
    monkeypatch.setattr(
        engine, "probe", ClamAVProbe(binary=str(missing), mode="clamscan", version="x")
    )
    target = tmp_path / "file.txt"
    target.write_text("hello")
    context = ScanContext(root=str(tmp_path), config=ClamAVConfig())
    findings = collect(engine, [make_entry(target, tmp_path)], context)
    assert findings == []
    assert engine.errors and "cannot start" in engine.errors[0]


# ---------------------------------------------------------------------------
# odd file names
# ---------------------------------------------------------------------------


def test_a_file_name_with_a_newline_is_reported_not_skipped(
    fake_clamav: Path, media: Path, ticks: list[tuple[int, str]]
) -> None:
    weird = media / "bad\nname.txt"
    weird.write_bytes(b"content that cannot go into a file list\n")
    clean = media / "clean.txt"
    context = ScanContext(
        root=str(media), config=ClamAVConfig(), tick=lambda size, path: ticks.append((size, path))
    )

    engine = ClamAVEngine(ClamAVConfig())
    findings = collect(engine, [make_entry(weird, media), make_entry(clean, media)], context)

    assert len(findings) == 1
    report = findings[0]
    assert report.path == str(weird)
    assert report.threat == "file name contains a newline: not scannable by ClamAV"
    assert report.severity is Severity.MEDIUM
    assert report.category == "engine-limitation"
    assert report.size == weird.stat().st_size
    # the odd file is still ticked so the progress bar stays honest
    assert (weird.stat().st_size, str(weird)) in ticks


def test_batch_without_scannable_entries_yields_nothing(
    fake_clamav: Path, media: Path, context: ScanContext
) -> None:
    weird = media / "only\rname.txt"
    weird.write_bytes(b"x")
    engine = ClamAVEngine(ClamAVConfig())
    findings = collect(engine, [make_entry(weird, media)], context)
    assert [finding.category for finding in findings] == ["engine-limitation"]


def test_scan_batch_is_a_no_op_without_a_usable_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "eicar.com"
    target.write_bytes(b"X5O!P%@AP[4EICAR-STANDARD-ANTIVIRUS-TEST-FILE")
    engine = ClamAVEngine(ClamAVConfig())
    monkeypatch.setattr(engine, "probe", ClamAVProbe())
    context = ScanContext(root=str(tmp_path), config=ClamAVConfig())
    assert collect(engine, [make_entry(target, tmp_path)], context) == []
    assert engine.errors == []


def test_scan_batch_with_no_entries_does_nothing(
    fake_clamav: Path, media: Path, context: ScanContext
) -> None:
    assert collect(ClamAVEngine(ClamAVConfig()), [], context) == []
