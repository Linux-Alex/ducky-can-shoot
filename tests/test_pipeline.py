"""Pipeline tests: the stage order, the verdict gates and the side effects.

Every test here replaces dcs.core.pipeline.build_provider with a local fake, so
no cloud host is ever contacted.  All output, report and outbox paths come from
the tmp_config fixture and therefore live inside tmp_path.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from dcs.core.config import AppConfig
from dcs.core.events import STAGE_ORDER, CollectingSink, Stage, StageState, Verdict
from dcs.core.pipeline import JobRequest, Pipeline
from dcs.core.result import JobResult
from dcs.core.upload.base import UploadResult

FAKE_LINK = "https://example.test/f/1"


class FakeProvider:
    """Cloud provider stand-in that records what the pipeline asked it to do."""

    provider_id = "fake"
    label = "Fake Host"
    requires_key = False
    max_bytes = 0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def availability(self) -> tuple[bool, str]:
        """Always usable, so the pipeline never stops at the availability probe."""
        return True, ""

    def upload(
        self,
        path: str,
        *,
        progress: Any = None,
        cancel: Any = None,
        part_label: str = "",
    ) -> UploadResult:
        """Record one call and pretend the file was published."""
        size = os.path.getsize(path)
        self.calls.append({"path": path, "size": size, "part_label": part_label})
        return UploadResult(
            ok=True,
            link=FAKE_LINK,
            provider=self.provider_id,
            path=path,
            size=size,
        )

    def folder_link(self) -> str:
        """No folder grouping: the tests always produce a single volume."""
        return ""


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    """Replace the provider factory used by the pipeline with a local fake."""
    provider = FakeProvider()
    monkeypatch.setattr("dcs.core.pipeline.build_provider", lambda *args, **kwargs: provider)
    return provider


def run_job(
    config: AppConfig,
    source: Path,
    *,
    cancel: threading.Event | None = None,
    label: str = "testusb",
    **overrides: Any,
) -> tuple[JobResult, CollectingSink]:
    """Run one pipeline job over *source* and return (result, sink)."""
    sink = CollectingSink()
    request = JobRequest(
        source_path=str(source),
        label=label,
        email_dry_run=True,
        recipients=["ops@example.test"],
        **overrides,
    )
    return Pipeline(config).run(request, sink=sink, cancel=cancel), sink


def files_in(root: Path) -> int:
    """Number of regular files below *root*, as the scanner would count them."""
    return sum(1 for path in root.rglob("*") if path.is_file())


def archives_in(directory: Path) -> list[Path]:
    """Every zip archive the pipeline may have left inside *directory*."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.zip"))


def skipped_reasons(sink: CollectingSink) -> dict[Stage, str]:
    """The reason attached to the last SKIPPED event of each skipped stage."""
    return {
        stage: message
        for stage, state, message in sink.stages
        if state is StageState.SKIPPED
    }


def test_clean_medium_runs_every_stage(
    tmp_config: AppConfig,
    clean_tree: Path,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> None:
    """A clean medium is scanned, archived, uploaded, reported and mailed."""
    tmp_config.upload.enabled = True

    result, sink = run_job(tmp_config, clean_tree)

    # the sink announces the seven pending stages first, in pipeline order
    assert sink.stages[:7] == [(stage, StageState.PENDING, "") for stage in STAGE_ORDER]
    assert sink.stage_states() == dict.fromkeys(STAGE_ORDER, StageState.DONE)

    assert result.verdict is Verdict.CLEAN
    assert result.safe is True
    assert result.cancelled is False
    assert result.scan is not None and result.scan.findings == []
    assert result.files_scanned == files_in(clean_tree)

    # archive: a real, non empty zip inside the configured output directory
    assert result.archive is not None and result.archive.ok is True
    archive = Path(result.archive.primary_path)
    assert archive.is_file()
    assert archive.stat().st_size > 0
    assert archive.parent == tmp_path / "out"
    assert result.archive_bytes == archive.stat().st_size
    assert len(result.archive_sha256) == 64

    # upload: exactly one call, with that very archive, no part label
    assert fake_provider.calls == [
        {"path": str(archive), "size": archive.stat().st_size, "part_label": ""}
    ]
    assert result.upload_links == [FAKE_LINK]
    assert result.share_link == FAKE_LINK
    assert result.upload_skipped_reason == ""

    # report: html, text, csv and json next to each other
    assert set(result.report_paths) == {"html", "text", "csv", "json"}
    for kind, path in result.report_paths.items():
        assert Path(path).is_file(), kind
        assert Path(path).parent == tmp_path / "reports"

    # email: a real .eml in the dry run directory, never actually sent
    assert result.email is not None
    assert result.email.ok is True
    assert result.email.dry_run is True
    assert result.email.sent is False
    assert result.email.recipients == ["ops@example.test"]
    message = Path(result.email.message_path)
    assert message.suffix == ".eml"
    assert message.is_file()
    assert message.parent == tmp_path / "outbox"
    # whichever advisory the job produced names the directory it triaged, so the
    # source is always identifiable from the mail alone
    assert str(clean_tree) in result.email.body_preview

    # the summary handed to the sink is the serialised result
    assert sink.final_summary["verdict"] == "clean"
    assert sink.final_summary["shareLink"] == FAKE_LINK
    assert sink.final_summary["uploadSkippedReason"] == ""

    # one timing per stage, in pipeline order, every one of them finished
    assert [timing.stage for timing in result.timings] == [stage.value for stage in STAGE_ORDER]
    assert {timing.state for timing in result.timings} == {"done"}
    assert all(timing.seconds >= 0.0 for timing in result.timings)


def test_json_report_is_rewritten_with_every_stage_and_the_email(
    tmp_config: AppConfig,
    clean_tree: Path,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> None:
    """The JSON on disk is refreshed at the end, so it holds all seven stages."""
    tmp_config.upload.enabled = True
    tmp_config.report.write_json = True

    result, _ = run_job(tmp_config, clean_tree)

    document = json.loads(Path(result.report_paths["json"]).read_text(encoding="utf-8"))

    assert [timing["stage"] for timing in document["timings"]] == [
        stage.value for stage in STAGE_ORDER
    ]
    assert {timing["state"] for timing in document["timings"]} == {"done"}, (
        "no stage may be left pending in the final report"
    )
    assert document["email"]["ok"] is True
    assert document["email"]["dryRun"] is True
    assert document["email"]["messagePath"] == result.email.message_path
    assert document["reports"]["json"] == result.report_paths["json"]
    assert document["verdict"] == "clean"
    assert document["shareLink"] == FAKE_LINK
    assert document["jobId"] == result.job_id


def test_infected_medium_is_neither_archived_nor_uploaded(
    tmp_config: AppConfig,
    sample_tree: Path,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> None:
    """Malware must never leave the machine: no archive, no upload."""
    tmp_config.upload.enabled = True

    result, sink = run_job(tmp_config, sample_tree, label="infected")

    assert result.verdict is Verdict.INFECTED
    assert result.findings, "the sample medium must produce findings"
    assert result.threat_count >= 1
    assert result.files_scanned == files_in(sample_tree)

    states = sink.stage_states()
    assert states[Stage.SCAN] is StageState.DONE
    assert states[Stage.ARCHIVE] is StageState.SKIPPED
    assert states[Stage.UPLOAD] is StageState.SKIPPED
    assert states[Stage.REPORT] is StageState.DONE
    assert states[Stage.EMAIL] is StageState.DONE

    reasons = skipped_reasons(sink)
    assert "infected" in reasons[Stage.ARCHIVE]
    assert "no archive was created" in reasons[Stage.ARCHIVE]
    # the upload keeps the archive reason instead of inventing a generic one
    assert reasons[Stage.UPLOAD] == reasons[Stage.ARCHIVE]
    assert result.upload_skipped_reason == reasons[Stage.ARCHIVE]

    assert result.archive is None
    assert result.archive_bytes == 0
    assert result.uploads == []
    assert result.share_link == ""
    assert fake_provider.calls == []
    assert archives_in(tmp_path / "out") == []


def test_infected_medium_upload_needs_confirmation(
    tmp_config: AppConfig,
    sample_tree: Path,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> None:
    """upload_when = always still asks for --yes before publishing malware."""
    tmp_config.upload.enabled = True
    tmp_config.upload.upload_when = "always"
    tmp_config.archive.when = "always"

    result, sink = run_job(tmp_config, sample_tree, label="infected")

    assert result.verdict is Verdict.INFECTED
    assert result.archive is not None and result.archive.ok is True
    archive = Path(result.archive.primary_path)
    assert archive.is_file()
    assert archive.parent == tmp_path / "out"

    states = sink.stage_states()
    assert states[Stage.ARCHIVE] is StageState.DONE
    assert states[Stage.UPLOAD] is StageState.SKIPPED

    reason = skipped_reasons(sink)[Stage.UPLOAD]
    assert "confirmation" in reason
    assert result.upload_skipped_reason == reason
    assert fake_provider.calls == []
    assert result.uploads == []
    assert result.share_link == ""


def test_infected_medium_upload_proceeds_with_assume_yes(
    tmp_config: AppConfig,
    sample_tree: Path,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> None:
    """With --yes the infected image is archived and uploaded on purpose."""
    tmp_config.upload.enabled = True
    tmp_config.upload.upload_when = "always"
    tmp_config.archive.when = "always"

    result, sink = run_job(tmp_config, sample_tree, label="infected", assume_yes=True)

    assert result.verdict is Verdict.INFECTED
    assert result.archive is not None and result.archive.ok is True
    archive = Path(result.archive.primary_path)
    assert archive.is_file()

    states = sink.stage_states()
    assert states[Stage.ARCHIVE] is StageState.DONE
    assert states[Stage.UPLOAD] is StageState.DONE

    assert len(fake_provider.calls) == 1
    assert fake_provider.calls[0]["path"] == str(archive)
    assert fake_provider.calls[0]["size"] == archive.stat().st_size
    assert result.upload_skipped_reason == ""
    assert result.share_link == FAKE_LINK
    assert archives_in(tmp_path / "out") == [archive]


def test_upload_is_skipped_when_the_configuration_disables_it(
    tmp_config: AppConfig,
    clean_tree: Path,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> None:
    """A clean run with upload.enabled = false still archives and reports."""
    assert tmp_config.upload.enabled is False

    result, sink = run_job(tmp_config, clean_tree)

    states = sink.stage_states()
    assert states[Stage.ARCHIVE] is StageState.DONE
    assert states[Stage.UPLOAD] is StageState.SKIPPED
    assert skipped_reasons(sink)[Stage.UPLOAD] == "uploading is disabled in the configuration"
    assert result.upload_skipped_reason == "uploading is disabled in the configuration"
    assert result.archive is not None and result.archive.ok is True
    assert len(archives_in(tmp_path / "out")) == 1
    assert fake_provider.calls == []
    assert result.share_link == ""


def test_email_stage_is_skipped_when_notifications_are_disabled(
    tmp_config: AppConfig,
    clean_tree: Path,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> None:
    """email.enabled = false skips the stage and writes no message at all."""
    tmp_config.email.enabled = False

    result, sink = run_job(tmp_config, clean_tree)

    states = sink.stage_states()
    assert states[Stage.REPORT] is StageState.DONE
    assert states[Stage.EMAIL] is StageState.SKIPPED
    assert "configuration" in skipped_reasons(sink)[Stage.EMAIL]

    assert result.email is None
    outbox = tmp_path / "outbox"
    assert not outbox.exists() or list(outbox.glob("*.eml")) == []


def test_missing_source_fails_inspect_and_still_writes_a_failure_report(
    tmp_config: AppConfig,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> None:
    """A source that is not a directory fails the run cleanly and is reported."""
    missing = tmp_path / "not-here"

    result, sink = run_job(tmp_config, missing, label="missing")

    assert result.verdict is Verdict.ERROR
    assert result.scan is None
    assert result.files_scanned == 0
    assert result.archive is None
    assert result.uploads == []
    assert result.email is None
    assert fake_provider.calls == []
    assert result.share_link == ""

    # the failure is attributed to the inspect stage, in the result ...
    assert [timing.stage for timing in result.failed_stages()] == [Stage.INSPECT.value]
    assert result.stage_states[Stage.INSPECT.value] == "failed"
    # ... and surfaced through the sink
    assert any(
        stage is Stage.INSPECT and state is StageState.FAILED
        for stage, state, _ in sink.stages
    )
    assert any(level == "error" and "internal error" in message for level, message in sink.logs)
    assert any("internal error" in warning for warning in result.warnings)

    # no archive and no upload side effect
    assert archives_in(tmp_path / "out") == []
    assert not (tmp_path / "outbox").exists()

    # the finally block still writes the failure report
    assert set(result.report_paths) == {"html", "text", "csv", "json"}
    for path in result.report_paths.values():
        assert Path(path).is_file()
    payload = json.loads(Path(result.report_paths["json"]).read_text(encoding="utf-8"))
    assert payload["verdict"] == "error"
    assert payload["jobId"] == result.job_id


def test_cancel_before_the_scan_leaves_the_medium_unscanned(
    tmp_config: AppConfig,
    clean_tree: Path,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> None:
    """An already set cancel event stops the run before the scan starts."""
    cancel = threading.Event()
    cancel.set()

    result, sink = run_job(tmp_config, clean_tree, cancel=cancel, label="cancelled")

    assert result.cancelled is True
    assert result.scan is None
    assert result.files_scanned == 0

    states = sink.stage_states()
    assert states[Stage.INSPECT] is StageState.DONE
    assert states[Stage.SCAN] is StageState.SKIPPED
    assert states[Stage.VERDICT] is StageState.CANCELLED
    verdict_events = [message for stage, state, message in sink.stages if stage is Stage.VERDICT]
    assert verdict_events[-1] == "the run was cancelled"
    # archive, upload and email never ran either and say why
    assert states[Stage.ARCHIVE] is StageState.SKIPPED
    assert states[Stage.UPLOAD] is StageState.SKIPPED
    assert states[Stage.EMAIL] is StageState.SKIPPED
    reasons = skipped_reasons(sink)
    assert reasons[Stage.SCAN] == "the run was cancelled"
    assert reasons[Stage.ARCHIVE] == reasons[Stage.SCAN]
    assert reasons[Stage.UPLOAD] == reasons[Stage.SCAN]
    assert reasons[Stage.EMAIL] == reasons[Stage.SCAN]

    # the timeline of the finished job names every stage, the skipped ones too
    assert result.stage_states[Stage.SCAN.value] == "skipped"
    assert [timing.stage for timing in result.timings] == [stage.value for stage in STAGE_ORDER]

    assert result.archive is None
    assert result.uploads == []
    assert result.email is None
    assert fake_provider.calls == []
    assert archives_in(tmp_path / "out") == []
    # a run stopped before the scan reports "cancelled", never "error": the
    # report and the email must not claim the medium could not be evaluated
    assert result.verdict is Verdict.CANCELLED
    # the failure report is still written
    assert set(result.report_paths) == {"html", "text", "csv", "json"}


def test_cancelled_run_should_report_the_cancelled_verdict(
    tmp_config: AppConfig,
    clean_tree: Path,
    fake_provider: FakeProvider,
) -> None:
    """Cancelling before the scan should produce Verdict.CANCELLED, not ERROR."""
    cancel = threading.Event()
    cancel.set()

    result, _ = run_job(tmp_config, clean_tree, cancel=cancel, label="cancelled")

    assert result.cancelled is True
    assert result.verdict is Verdict.CANCELLED


def test_job_request_from_dict_ignores_unknown_keys(tmp_path: Path) -> None:
    """Only the declared fields survive, everything else is dropped."""
    request = JobRequest.from_dict(
        {
            "source_path": str(tmp_path),
            "label": "stick",
            "device_path": "/dev/sdb1",
            "assume_yes": True,
            "not_a_field": "ignored",
            "recipients": ["ops@example.test"],
        }
    )

    assert request == JobRequest(
        source_path=str(tmp_path),
        label="stick",
        device_path="/dev/sdb1",
        assume_yes=True,
        recipients=["ops@example.test"],
    )
    assert not hasattr(request, "not_a_field")


def test_job_request_from_dict_keeps_the_defaults_but_needs_a_source() -> None:
    """Unknown keys are dropped, defaults are kept and source_path is required."""
    request = JobRequest.from_dict({"source_path": "/media/usb", "unknown": 1})

    assert request == JobRequest(source_path="/media/usb")
    assert request.label == ""
    assert request.do_scan is True
    assert request.do_archive is None
    assert request.do_upload is None
    assert request.do_email is None
    assert request.email_dry_run is False
    assert request.assume_yes is False
    assert request.recipients == []

    with pytest.raises(TypeError):
        JobRequest.from_dict({"unknown": 1})
