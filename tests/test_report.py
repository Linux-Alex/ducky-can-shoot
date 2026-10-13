"""Tests for dcs.core.report: HTML, text, CSV and JSON rendering."""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dcs.core.archiver import ArchivePart, ArchiveResult
from dcs.core.config import ReportConfig
from dcs.core.events import Finding, ScanStats, Severity, Verdict
from dcs.core.report import (
    VERDICT_META,
    ReportPaths,
    render_csv,
    render_html,
    render_text,
    write_reports,
)
from dcs.core.result import JobResult, StageTiming
from dcs.core.scanner import EngineStatus, ScanResult
from dcs.version import APP_HOMEPAGE

STARTED_AT = "2026-02-03T04:05:06+00:00"
FINISHED_AT = "2026-02-03T04:05:20+00:00"
SHARE_LINK = "https://gofile.io/d/AbCdEf"
JOB_ID = "5f2c9a1b7d30"

#: a file name that would break the report if it were not escaped
HOSTILE_NAME = '<script>alert(1)</script> "quoted".exe'
HOSTILE_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt; &quot;quoted&quot;.exe"

#: findings sorted by severity, then by path: that is the order the reports use
SORTED_PATHS = [
    "autorun.inf",
    HOSTILE_NAME,
    "eicar.com",
    "update.ps1",
    "readme.txt",
    "photos/holiday.jpg",
]


def sample_findings() -> list[Finding]:
    """Six findings covering every severity, one with a hostile file name."""
    return [
        Finding(
            path="eicar.com",
            threat="Eicar-Test-Signature",
            engine="clamav",
            severity=Severity.HIGH,
            category="malware",
            size=68,
            sha256="a" * 64,
            detail="matched the EICAR test pattern",
        ),
        Finding(
            path=HOSTILE_NAME,
            threat="Packed executable with process injection APIs",
            engine="heuristic",
            severity=Severity.HIGH,
            category="pe",
            size=8192,
            sha256="c" * 64,
            detail="WriteProcessMemory and CreateRemoteThread are imported",
        ),
        Finding(
            path="autorun.inf",
            threat="Autorun launches an executable",
            engine="heuristic",
            severity=Severity.CRITICAL,
            category="autorun",
            size=52,
            detail="open=setup.exe",
        ),
        Finding(
            path="update.ps1",
            threat="PowerShell download cradle",
            engine="heuristic",
            severity=Severity.MEDIUM,
            category="script",
            size=88,
            detail="DownloadString from 185.11.22.33",
        ),
        Finding(
            path="readme.txt",
            threat="Double extension",
            engine="heuristic",
            severity=Severity.LOW,
            category="name",
            size=61,
        ),
        Finding(
            path="photos/holiday.jpg",
            threat="High entropy payload",
            engine="heuristic",
            severity=Severity.INFO,
            category="entropy",
            size=4096,
        ),
    ]


def sample_warnings() -> list[str]:
    """Two notes that must show up in the HTML and the text report."""
    return ["the archive could not be split", "two files were skipped"]


def sample_timings() -> list[StageTiming]:
    """One timing per stage that ran, for the timeline section."""
    return [
        StageTiming(stage="scan", state="done", seconds=12.5, message="19 files"),
        StageTiming(stage="archive", state="done", seconds=1.5, message="2 volumes"),
    ]


def sample_archive() -> ArchiveResult:
    """A successful split archive with two volumes."""
    return ArchiveResult(
        ok=True,
        tool="zip",
        source_files=19,
        source_bytes=1_234_567,
        duration=2.5,
        parts=[
            ArchivePart(path="/media/out/DCS_holiday.z01", size=1_048_576),
            ArchivePart(path="/media/out/DCS_holiday.zip", size=65_536),
        ],
    )


def make_scan(findings: Sequence[Finding], *, verdict: Verdict = Verdict.INFECTED) -> ScanResult:
    """A finished scan of a 19 file medium with one skipped and one unreadable path."""
    return ScanResult(
        root="/media/usb",
        verdict=verdict,
        findings=list(findings),
        stats=ScanStats(
            files_scanned=19,
            bytes_scanned=1_234_567,
            files_skipped=1,
            bytes_total=2_000_000,
            files_total=21,
            errors=1,
            duration=12.5,
            engines=["heuristic"],
        ),
        warnings=["the medium is nearly full"],
        unreadable=["System Volume Information: Permission denied"],
        skipped_large=[("/media/usb/big.iso", 8 * 1024 ** 3)],
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
    )


def make_result(
    findings: Sequence[Finding] = (),
    *,
    verdict: Verdict = Verdict.INFECTED,
    scan: ScanResult | None = None,
    **overrides: Any,
) -> JobResult:
    """A finished job with a scan, an archive, warnings, timings and a share link."""
    defaults: dict[str, Any] = {
        "job_id": JOB_ID,
        "source_path": "/media/usb",
        "source_label": "holiday-usb",
        "device": {
            "label": "HOLIDAY",
            "path": "/dev/sdb1",
            "fstype": "vfat",
            "fsver": "FAT32",
            "size": 16 * 1024 ** 3,
            "transport": "usb",
            "vendor": "SanDisk",
            "readOnly": False,
        },
        "verdict": verdict,
        "scan": scan if scan is not None else make_scan(findings, verdict=verdict),
        "archive": sample_archive(),
        "archive_sha256": "b" * 64,
        "upload_skipped_reason": "upload is disabled",
        "share_link": SHARE_LINK,
        "warnings": sample_warnings(),
        "timings": sample_timings(),
        "started_at": STARTED_AT,
        "finished_at": FINISHED_AT,
        "duration": 14.0,
        "app_version": "0.1.0",
    }
    defaults.update(overrides)
    return JobResult(**defaults)


@pytest.fixture
def infected_result() -> JobResult:
    """The reference result: infected medium, six findings, split archive."""
    return make_result(sample_findings(), verdict=Verdict.INFECTED)


@pytest.fixture
def clean_result() -> JobResult:
    """A clean medium with nothing to report but a share link."""
    scan = ScanResult(
        root="/media/usb",
        verdict=Verdict.CLEAN,
        findings=[],
        stats=ScanStats(
            files_scanned=5,
            bytes_scanned=4096,
            files_total=5,
            bytes_total=4096,
            duration=0.5,
            engines=["heuristic"],
        ),
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
    )
    return make_result(
        (),
        verdict=Verdict.CLEAN,
        scan=scan,
        warnings=[],
        upload_skipped_reason="",
    )


def findings_table(markup: str) -> str:
    """Return the HTML of the findings table, without the surrounding markup."""
    return markup.split("<h2>Findings", 1)[1].split("</table>", 1)[0]


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------


def test_hostile_file_name_is_escaped(infected_result: JobResult) -> None:
    markup = render_html(infected_result)
    assert "<script>" not in markup
    assert HOSTILE_NAME not in markup
    assert HOSTILE_ESCAPED in markup
    assert html.escape(HOSTILE_NAME) in findings_table(markup)


@pytest.mark.parametrize("verdict", list(Verdict))
def test_verdict_banner_uses_the_meta_table(infected_result: JobResult, verdict: Verdict) -> None:
    headline, css_class, explanation = VERDICT_META[verdict]
    markup = render_html(replace(infected_result, verdict=verdict))
    assert f'<div class="card verdict {css_class}">' in markup
    assert f'<span class="badge {css_class}">{headline}</span>' in markup
    assert explanation in markup


def test_one_row_per_finding_with_its_severity_badge(infected_result: JobResult) -> None:
    markup = render_html(infected_result)
    table = findings_table(markup)
    assert markup.count("<h2>Findings (6)</h2>") == 1
    # the header row plus one row per finding
    assert table.count("<tr>") == 1 + len(infected_result.findings)
    for finding in infected_result.findings:
        assert f'<span class="badge {finding.severity}">{finding.severity.value}</span>' in table
        assert f'<td class="path">{html.escape(finding.path)}</td>' in table
        assert html.escape(finding.threat) in table


def test_findings_are_ordered_by_severity(infected_result: JobResult) -> None:
    table = findings_table(render_html(infected_result))
    paths = re.findall(r'<td class="path">(.*?)</td>', table)
    assert paths == [html.escape(path) for path in SORTED_PATHS]


def test_share_link_is_rendered_as_a_link(clean_result: JobResult) -> None:
    markup = render_html(clean_result)
    assert f'<a href="{SHARE_LINK}">{SHARE_LINK}</a>' in markup
    assert "Safe copy available here" in markup
    # a threatening medium never advertises a safe copy
    infected = render_html(make_result(sample_findings(), verdict=Verdict.INFECTED))
    assert "Safe copy available here" not in infected
    assert f'<a href="{SHARE_LINK}">' not in infected


def test_warnings_and_the_archive_are_in_the_markup(infected_result: JobResult) -> None:
    markup = render_html(infected_result)
    assert "<h2>Notes</h2>" in markup
    for warning in infected_result.warnings:
        assert f'<div class="note">{html.escape(warning)}</div>' in markup
    assert "<tr><th>Volumes</th><td>2</td></tr>" in markup
    assert "<tr><th>Tool</th><td>zip</td></tr>" in markup
    assert f"<td class='path'>{infected_result.archive_sha256}</td>" in markup


def test_markup_is_self_contained(infected_result: JobResult) -> None:
    markup = render_html(infected_result)
    assert markup.startswith("<!DOCTYPE html>")
    assert markup.endswith("</body></html>")
    assert "<style>" in markup and "</style>" in markup
    assert "@media (prefers-color-scheme: dark)" in markup
    assert "<script" not in markup.lower()
    assert "<link" not in markup.lower()
    assert "http://" not in markup
    # the homepage is the only external reference, and it is a link, not an asset
    assert set(re.findall(r"https?://[^\"'<>\s]+", markup)) == {APP_HOMEPAGE}


def test_coverage_gaps_section_lists_skipped_and_unreadable_files(
    infected_result: JobResult,
) -> None:
    markup = render_html(infected_result)
    assert "<h2>Coverage gaps</h2>" in markup
    assert "1 file(s) were larger than the configured limit and were not scanned." in markup
    assert '<li class="path">/media/usb/big.iso (8.0 GiB)</li>' in markup
    assert "1 path(s) could not be read." in markup
    assert '<li class="path">System Volume Information: Permission denied</li>' in markup


def test_no_coverage_gaps_section_for_a_clean_scan(clean_result: JobResult) -> None:
    markup = render_html(clean_result)
    assert "Coverage gaps" not in markup
    assert "Nothing suspicious was found." in markup
    assert "<h2>Findings (0)</h2>" in markup


def test_engines_section_reports_which_engine_ran(infected_result: JobResult) -> None:
    status = EngineStatus(
        engine_id="clamav",
        label="ClamAV",
        available=False,
        version="1.4.3",
        detail="clamscan was not found",
        warnings=["the signature database is 9 days old"],
    )
    result = replace(infected_result, scan=replace(infected_result.scan, engines=[status]))

    markup = render_html(result)

    assert "<h2>Engines</h2>" in markup
    assert (
        "<tr><td>ClamAV</td><td>1.4.3</td><td>not available</td>"
        "<td>clamscan was not found; the signature database is 9 days old</td></tr>"
    ) in markup
    assert "No antivirus engine (ClamAV) took part in this scan" in markup


def test_file_list_section_appears_only_when_the_scan_collected_it(
    infected_result: JobResult,
) -> None:
    assert "Files scanned (" not in render_html(infected_result)

    names = ["readme.txt", "docs/notes.md", HOSTILE_NAME]
    result = replace(infected_result, scan=replace(infected_result.scan, file_list=names))

    markup = render_html(result)
    assert "<h2>Files scanned (3)</h2>" in markup
    assert '<li class="path">readme.txt</li>' in markup
    assert f'<li class="path">{HOSTILE_ESCAPED}</li>' in markup

    text = render_text(result)
    assert "FILES SCANNED (3)" in text
    assert "  docs/notes.md" in text


def test_max_findings_truncates_the_html_rows(infected_result: JobResult) -> None:
    config = ReportConfig()
    config.max_findings = 2
    markup = render_html(infected_result, config)

    assert "<h2>Findings (6)</h2>" in markup
    table = findings_table(markup)
    assert table.count("<tr>") == 1 + 2
    assert table.count('<td class="path">') == 2
    assert f'<td class="path">{html.escape("autorun.inf")}</td>' in table
    assert f'<td class="path">{HOSTILE_ESCAPED}</td>' in table
    assert f'<td class="path">{html.escape("eicar.com")}</td>' not in table
    assert '<p class="sub">4 further findings are in the CSV.</p>' in markup


# ---------------------------------------------------------------------------
# render_text
# ---------------------------------------------------------------------------


def test_render_text_reports_the_verdict(infected_result: JobResult) -> None:
    text = render_text(infected_result)
    headline, _, explanation = VERDICT_META[Verdict.INFECTED]
    assert f"VERDICT      : {headline.upper()} [{Verdict.INFECTED.value}]" in text
    assert "VERDICT      : MALWARE DETECTED [infected]" in text
    assert explanation in text
    assert f"Source       : {infected_result.source_path}" in text
    assert f"Label        : {infected_result.source_label}" in text
    assert "Findings     : 6 (3 threatening)" in text


def test_render_text_lists_the_findings(infected_result: JobResult) -> None:
    text = render_text(infected_result)
    findings = text.split("FINDINGS", 1)[1].split("TIMELINE", 1)[0]
    assert "[CRITICAL] Autorun launches an executable" in findings
    assert "[HIGH    ] Packed executable with process injection APIs" in findings
    for finding in infected_result.findings_sorted:
        assert f"[{str(finding.severity).upper():8}] {finding.threat}" in findings
        assert f"           {finding.path}" in findings


def test_render_text_has_one_timeline_line_per_timing(infected_result: JobResult) -> None:
    text = render_text(infected_result)
    timeline = text.split("TIMELINE", 1)[1]
    # one line per timing: stage padded to 16 columns, duration right aligned,
    # then the state padded to 9 columns and the message
    assert "scan" + " " * 16 + "00:12  done" + " " * 6 + "19 files" in timeline
    assert "archive" + " " * 13 + "00:01  done" + " " * 6 + "2 volumes" in timeline
    for timing in infected_result.timings:
        assert timing.stage in timeline
        assert timing.message in timeline
        assert timing.state in timeline


def test_render_text_has_a_notes_section_with_the_warnings(infected_result: JobResult) -> None:
    text = render_text(infected_result)
    notes = text.split("NOTES", 1)[1]
    for warning in infected_result.warnings:
        assert f"- {warning}" in notes


def test_render_text_says_none_when_there_are_no_findings(clean_result: JobResult) -> None:
    text = render_text(clean_result)
    findings = text.split("FINDINGS", 1)[1].split("TIMELINE", 1)[0]
    assert "\nnone\n" in findings
    assert "VERDICT      : NO THREATS FOUND [clean]" in text
    assert "NOTES" not in text
    assert f"Share link   : {SHARE_LINK}" in text


# ---------------------------------------------------------------------------
# render_csv
# ---------------------------------------------------------------------------


def test_render_csv_writes_a_header_and_one_row_per_finding(infected_result: JobResult) -> None:
    rows = list(csv.reader(io.StringIO(render_csv(infected_result))))
    assert rows[0] == [
        "severity",
        "engine",
        "category",
        "threat",
        "path",
        "size",
        "sha256",
        "detail",
    ]
    # the CSV always holds every finding, it has no max_findings limit
    assert len(rows) == 1 + len(infected_result.findings)
    assert [row[4] for row in rows[1:]] == SORTED_PATHS
    assert [row[0] for row in rows[1:]] == [
        "critical",
        "high",
        "high",
        "medium",
        "low",
        "info",
    ]


def test_render_csv_quotes_a_path_containing_a_comma() -> None:
    path = 'photos/holiday, 2026 "best".jpg'
    finding = Finding(
        path=path,
        threat="Double extension",
        engine="heuristic",
        severity=Severity.LOW,
        category="name",
        size=61,
        detail="also a comma, here",
    )
    text = render_csv(make_result([finding], verdict=Verdict.SUSPICIOUS))

    assert '"photos/holiday, 2026 ""best"".jpg"' in text
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) == 2
    assert rows[1] == [
        "low",
        "heuristic",
        "name",
        "Double extension",
        path,
        "61",
        "",
        "also a comma, here",
    ]


# ---------------------------------------------------------------------------
# write_reports
# ---------------------------------------------------------------------------


def test_write_reports_writes_all_four_files(tmp_path: Path, infected_result: JobResult) -> None:
    directory = tmp_path / "reports"
    paths = write_reports(infected_result, ReportConfig(), str(directory), basename="DCS_holiday")

    assert paths.to_dict() == {
        "html": str(directory / "DCS_holiday.html"),
        "text": str(directory / "DCS_holiday.txt"),
        "csv": str(directory / "DCS_holiday.findings.csv"),
        "json": str(directory / "DCS_holiday.json"),
    }
    assert set(paths.existing()) == {"html", "text", "csv", "json"}
    assert sorted(os.listdir(directory)) == [
        "DCS_holiday.findings.csv",
        "DCS_holiday.html",
        "DCS_holiday.json",
        "DCS_holiday.txt",
    ]
    for path in paths.existing().values():
        assert os.path.getsize(path) > 0

    html_text = Path(paths.html).read_text(encoding="utf-8")
    assert html_text.startswith("<!DOCTYPE html>")
    assert HOSTILE_ESCAPED in html_text
    assert Path(paths.text).read_text(encoding="utf-8").startswith("Ducky Can Shoot")

    data = json.loads(Path(paths.json).read_text(encoding="utf-8"))
    assert data["verdict"] == "infected"
    assert data["findingCount"] == len(infected_result.findings) == 6
    assert data["jobId"] == JOB_ID
    assert data["archive"]["partCount"] == 2


def test_write_reports_sanitises_the_basename(tmp_path: Path, infected_result: JobResult) -> None:
    directory = tmp_path / "reports"
    paths = write_reports(infected_result, ReportConfig(), str(directory), basename="DCS holiday/usb")
    assert os.path.basename(paths.html) == "DCS_holiday_usb.html"
    assert os.path.basename(paths.csv) == "DCS_holiday_usb.findings.csv"


def test_write_reports_without_json_writes_three_files(
    tmp_path: Path, infected_result: JobResult
) -> None:
    config = ReportConfig()
    config.write_json = False
    directory = tmp_path / "reports"

    paths = write_reports(infected_result, config, str(directory), basename="DCS_holiday")

    assert paths.json == ""
    assert set(paths.existing()) == {"html", "text", "csv"}
    assert sorted(os.listdir(directory)) == [
        "DCS_holiday.findings.csv",
        "DCS_holiday.html",
        "DCS_holiday.txt",
    ]


def test_write_reports_disabled_writes_nothing(tmp_path: Path, infected_result: JobResult) -> None:
    config = ReportConfig()
    config.enabled = False
    directory = tmp_path / "reports"

    paths = write_reports(infected_result, config, str(directory), basename="DCS_holiday")

    assert paths.to_dict() == {"html": "", "text": "", "csv": "", "json": ""}
    assert paths.existing() == {}
    assert not directory.exists()


def test_report_paths_existing_keeps_only_files_that_are_there(tmp_path: Path) -> None:
    present = tmp_path / "DCS_holiday.html"
    present.write_text("<html></html>", encoding="utf-8")
    paths = ReportPaths(
        html=str(present),
        text=str(tmp_path / "gone.txt"),
        csv="",
        json=str(tmp_path / "gone.json"),
    )
    assert paths.existing() == {"html": str(present)}
    assert paths.to_dict()["text"] == str(tmp_path / "gone.txt")
