"""Tests for the local job history (0.3.1).

The history is an index, not a copy: these tests pin that it records what a
person searches for (labels, verdicts, counts, paths and hashes), that it never
grows forever, and that it never becomes a second copy of a report.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dcs.core.config import AppConfig, HistoryConfig
from dcs.core.events import Finding, ScanStats, Severity, Verdict
from dcs.core.history import SCHEMA_VERSION, JobHistory, open_history
from dcs.core.pipeline import JobRequest, Pipeline
from dcs.core.result import JobResult, StageTiming
from dcs.core.scanner import ScanResult
from dcs.core.upload.base import UploadResult


def make_result(
    *,
    job_id: str = "abc123def456",
    label: str = "SANDISK16",
    verdict: Verdict = Verdict.CLEAN,
    started_at: str = "",
    findings: list[Finding] | None = None,
    encrypted: bool = False,
    report_dir: Path | None = None,
) -> JobResult:
    result = JobResult(
        job_id=job_id,
        source_path="/run/media/test/SANDISK16",
        source_label=label,
        verdict=verdict,
        started_at=started_at or datetime.now(UTC).isoformat(timespec="seconds"),
        finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
        duration=12.5,
        app_version="0.2.4",
    )
    found = findings or []
    result.scan = ScanResult(
        root=result.source_path,
        verdict=verdict,
        findings=found,
        stats=ScanStats(files_scanned=42, bytes_scanned=1024 * 1024, duration=3.0),
    )
    result.scan.engines = []  # type: ignore[assignment]
    result.timings = [StageTiming("scan", "done", 3.0, "42 files")]
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        html = report_dir / "report.html"
        html.write_text("<html>a report body that must not be copied</html>", encoding="utf-8")
        result.report_paths = {"html": str(html), "text": "", "csv": "", "json": ""}
    return result


@pytest.fixture
def store(tmp_path: Path) -> JobHistory:
    return JobHistory(tmp_path / "history.db", config=HistoryConfig())


# ---------------------------------------------------------------------------
# the round trip
# ---------------------------------------------------------------------------


def test_record_and_read_back_one_job(store: JobHistory) -> None:
    finding = Finding(
        path="eicar.com",
        threat="Eicar-Test-Signature",
        engine="clamav",
        severity=Severity.CRITICAL,
        size=68,
        sha256="ab" * 32,
    )
    result = make_result(verdict=Verdict.INFECTED, findings=[finding])

    assert store.record(result) == result.job_id
    assert store.count() == 1

    entry = store.get(result.job_id)
    assert entry is not None
    assert entry.label == "SANDISK16"
    assert entry.verdict == "infected"
    assert entry.files_scanned == 42
    assert entry.finding_count == 1
    assert entry.threat_count == 1
    assert entry.app_version == "0.2.4"
    assert entry.findings[0]["path"] == "eicar.com"
    assert entry.findings[0]["sha256"] == "ab" * 32


def test_recording_the_same_job_twice_replaces_it(store: JobHistory) -> None:
    result = make_result()
    store.record(result)
    result.finished_at = datetime.now(UTC).isoformat()
    result.warnings.append("re-recorded")
    store.record(result)

    assert store.count() == 1


def test_archive_and_upload_fields_are_kept(store: JobHistory) -> None:
    from dcs.core.archiver import ArchivePart, ArchiveResult

    result = make_result(encrypted=True)
    result.archive = ArchiveResult(
        ok=True,
        parts=[ArchivePart(path="/tmp/DCS_usb.7z", size=2048)],
        tool="7z",
        encrypted=True,
        encryption="AES-256",
        headers_encrypted=True,
    )
    result.archive_sha256 = "cd" * 32
    result.share_link = "https://example.test/f/1"
    result.uploads = [UploadResult(ok=True, link="https://example.test/f/1", provider="fake")]

    store.record(result)
    entry = store.get(result.job_id)

    assert entry is not None
    assert entry.archive_encrypted is True
    assert entry.archive_path.endswith(".7z")
    assert entry.archive_bytes == 2048
    assert entry.archive_sha256 == "cd" * 32
    assert entry.share_link == "https://example.test/f/1"
    assert entry.upload_provider == "fake"


# ---------------------------------------------------------------------------
# searching
# ---------------------------------------------------------------------------


def test_search_covers_label_verdict_path_and_hash(store: JobHistory) -> None:
    finding = Finding(
        path="photos/invoice.pdf.exe",
        threat="Double extension",
        engine="heuristic",
        severity=Severity.HIGH,
        sha256="deadbeef" + "00" * 28,
    )
    store.record(
        make_result(job_id="job-one", label="HOLIDAY", verdict=Verdict.INFECTED, findings=[finding])
    )
    store.record(make_result(job_id="job-two", label="WORKSTICK", verdict=Verdict.CLEAN))

    assert [entry.job_id for entry in store.list_jobs("holiday")] == ["job-one"]
    assert [entry.job_id for entry in store.list_jobs("invoice.pdf.exe")] == ["job-one"]
    assert [entry.job_id for entry in store.list_jobs("deadbeef")] == ["job-one"]
    assert [entry.job_id for entry in store.list_jobs("", verdict="clean")] == ["job-two"]
    assert store.list_jobs("nothing-with-this-name") == []


def test_list_is_newest_first_and_honours_the_limit(store: JobHistory) -> None:
    base = datetime.now(UTC)
    for index in range(5):
        started = (base - timedelta(days=index)).isoformat(timespec="seconds")
        store.record(make_result(job_id=f"job-{index}", label=f"stick-{index}", started_at=started))

    newest = store.list_jobs(limit=2)
    assert [entry.job_id for entry in newest] == ["job-0", "job-1"]

    # the same query is what the CLI prints, and it is JSON serialisable
    payload = [entry.to_dict() for entry in store.list_jobs(limit=10)]
    assert json.loads(json.dumps(payload))[0]["label"] == "stick-0"


# ---------------------------------------------------------------------------
# deleting and retention
# ---------------------------------------------------------------------------


def test_delete_and_clear_report_what_they_removed(store: JobHistory) -> None:
    store.record(make_result(job_id="a"))
    store.record(make_result(job_id="b"))

    assert store.delete("a") is True
    assert store.delete("a") is False
    assert store.count() == 1
    assert store.clear() == 1
    assert store.count() == 0


def test_retention_drops_the_oldest_jobs(tmp_path: Path) -> None:
    config = HistoryConfig(max_jobs=2)
    store = JobHistory(tmp_path / "history.db", config=config)
    base = datetime.now(UTC)
    for index in range(4):
        started = (base - timedelta(minutes=index)).isoformat(timespec="seconds")
        store.record(make_result(job_id=f"job-{index}", started_at=started))

    remaining = [entry.job_id for entry in store.list_jobs(limit=10)]
    assert remaining == ["job-0", "job-1"]


def test_retention_drops_jobs_past_the_age_limit(tmp_path: Path) -> None:
    config = HistoryConfig(max_age_days=30)
    store = JobHistory(tmp_path / "history.db", config=config)
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat(timespec="seconds")
    fresh = datetime.now(UTC).isoformat(timespec="seconds")

    # the age limit is applied by prune(), which is also what record() calls
    config.max_age_days = 0
    store.record(make_result(job_id="old", started_at=old))
    store.record(make_result(job_id="fresh", started_at=fresh))
    config.max_age_days = 30

    assert store.prune() == 1
    assert [entry.job_id for entry in store.list_jobs(limit=10)] == ["fresh"]


# ---------------------------------------------------------------------------
# privacy and schema
# ---------------------------------------------------------------------------


def test_the_database_holds_paths_not_report_bodies(tmp_path: Path, store: JobHistory) -> None:
    report_dir = tmp_path / "reports"
    result = make_result(report_dir=report_dir)
    store.record(result)

    blob = (tmp_path / "history.db").read_bytes()
    assert b"a report body that must not be copied" not in blob
    entry = store.get(result.job_id)
    assert entry is not None
    assert entry.report_html == str(report_dir / "report.html")


def test_the_database_is_private_to_its_owner(store: JobHistory) -> None:
    store.record(make_result())
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600


def test_the_schema_is_versioned_and_reopening_migrates_nothing_on_top(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    JobHistory(path).record(make_result(job_id="first"))

    import sqlite3

    with sqlite3.connect(path) as conn:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    assert int(version) == SCHEMA_VERSION

    # a second store over the same file sees the same job and does not reset it
    reopened = JobHistory(path)
    assert reopened.count() == 1
    assert reopened.get("first") is not None


def test_remove_database_deletes_the_index(tmp_path: Path) -> None:
    store = JobHistory(tmp_path / "history.db")
    store.record(make_result())
    assert store.remove_database() is True
    assert not store.path.exists()


# ---------------------------------------------------------------------------
# the pipeline writes the history
# ---------------------------------------------------------------------------


def test_pipeline_records_a_finished_job(
    tmp_config: AppConfig, clean_tree: Path
) -> None:
    result = Pipeline(tmp_config).run(
        JobRequest(source_path=str(clean_tree), label="histtest", email_dry_run=True, recipients=[]),
    )

    store = open_history(tmp_config)
    entry = store.get(result.job_id)
    assert entry is not None
    assert entry.label == "histtest"
    assert entry.verdict == "clean"
    assert entry.report_html and Path(entry.report_html).exists()


def test_pipeline_skips_the_history_when_it_is_disabled(
    tmp_config: AppConfig, clean_tree: Path
) -> None:
    tmp_config.history.enabled = False
    tmp_config.archive.enabled = False

    result = Pipeline(tmp_config).run(
        JobRequest(source_path=str(clean_tree), label="nohist", email_dry_run=True, recipients=[]),
    )

    store = open_history(tmp_config)
    assert store.get(result.job_id) is None


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


@pytest.fixture
def history_config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[history]",
                f'database = "{tmp_path / "history.db"}"',
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_cli_history_list_show_and_delete(
    history_config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dcs.cli import main

    # the CLI reads the database named by the config file it is given
    target = JobHistory(_history_path(history_config_file))
    target.record(make_result(job_id="job-cli", label="clistick"))

    code = main(["--config", str(history_config_file), "history", "list"])
    captured = capsys.readouterr()
    assert code == 0
    assert "job-cli" in captured.out
    assert "clistick" in captured.out

    code = main(["--config", str(history_config_file), "history", "show", "job-cli", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["label"] == "clistick"
    assert payload["jobId"] == "job-cli"

    code = main(["--config", str(history_config_file), "history", "delete", "job-cli", "--yes"])
    captured = capsys.readouterr()
    assert code == 0
    assert "Deleted" in captured.out
    assert target.get("job-cli") is None


def test_cli_history_list_does_not_create_an_empty_database(
    history_config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dcs.cli import main

    database = _history_path(history_config_file)
    assert not database.exists()

    code = main(["--config", str(history_config_file), "history", "list"])
    captured = capsys.readouterr()

    assert code == 0
    assert "No jobs in the history yet" in captured.out
    assert not database.exists(), "listing must not create the index"


def test_cli_history_clear_needs_confirmation_without_a_terminal(
    history_config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dcs.cli import main

    target = JobHistory(_history_path(history_config_file))
    target.record(make_result(job_id="keeplater"))

    code = main(["--config", str(history_config_file), "history", "clear"])
    captured = capsys.readouterr()

    assert code == 2
    assert target.count() == 1
    assert "clear" in captured.err.lower()

    code = main(["--config", str(history_config_file), "history", "clear", "--yes"])
    assert code == 0
    assert target.count() == 0


def test_cli_history_search_and_retention(
    history_config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dcs.cli import main

    target = JobHistory(_history_path(history_config_file))
    target.record(make_result(job_id="findme", label="BLUE"))
    target.record(make_result(job_id="other", label="GREEN"))

    code = main(["--config", str(history_config_file), "history", "list", "--query", "blue"])
    captured = capsys.readouterr()
    assert code == 0
    assert "findme" in captured.out
    assert "other" not in captured.out

    code = main(["--config", str(history_config_file), "history", "prune"])
    assert code == 0


# ---------------------------------------------------------------------------
# small helpers for the CLI tests
# ---------------------------------------------------------------------------


def _history_path(config_file: Path) -> Path:
    text = config_file.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("database = "):
            return Path(line.split("=", 1)[1].strip().strip('"'))
    raise AssertionError("the test config file has no database line")
