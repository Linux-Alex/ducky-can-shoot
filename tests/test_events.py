"""Tests for the headless event model: stages, findings, progress and sinks.

The ProgressMeter is driven by an injected clock, so every rate, ETA and
throttling decision below is exact and no test ever sleeps.
"""

from __future__ import annotations

from typing import Any

import pytest

from dcs.core import events
from dcs.core.events import (
    STAGE_DESCRIPTIONS,
    STAGE_ICONS,
    STAGE_ORDER,
    STAGE_TITLES,
    VERDICT_LABELS,
    CollectingSink,
    EventSink,
    Finding,
    MultiSink,
    NullSink,
    ProgressMeter,
    Severity,
    Stage,
    StageProgress,
    StageState,
    Verdict,
    severity_rank,
)
from dcs.core.util import human_bytes, human_duration, human_rate


class FakeClock:
    """Stand-in for the time module: monotonic() only moves when told to."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace dcs.core.events.time with a clock the test controls."""
    fake = FakeClock()
    monkeypatch.setattr(events, "time", fake)
    return fake


class BrokenSink:
    """Sink that records the call and then raises, like a crashing GUI bridge."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def on_stage(self, stage: Stage, state: StageState, message: str = "") -> None:
        self.calls.append("on_stage")
        raise RuntimeError("stage bridge is broken")

    def on_progress(self, progress: StageProgress) -> None:
        self.calls.append("on_progress")
        raise RuntimeError("progress bridge is broken")

    def on_finding(self, finding: Finding) -> None:
        self.calls.append("on_finding")
        raise RuntimeError("finding bridge is broken")

    def on_log(self, level: str, message: str) -> None:
        self.calls.append("on_log")
        raise RuntimeError("log bridge is broken")

    def on_summary(self, summary: dict[str, Any]) -> None:
        self.calls.append("on_summary")
        raise RuntimeError("summary bridge is broken")


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


def test_stage_order_is_the_pipeline_sequence() -> None:
    assert STAGE_ORDER == (
        Stage.INSPECT,
        Stage.SCAN,
        Stage.VERDICT,
        Stage.ARCHIVE,
        Stage.UPLOAD,
        Stage.REPORT,
        Stage.EMAIL,
    )
    assert tuple(Stage) == STAGE_ORDER
    assert [stage.value for stage in Stage] == [
        "inspect",
        "scan",
        "verdict",
        "archive",
        "upload",
        "report",
        "email",
    ]
    assert str(Stage.SCAN) == "scan"
    assert str(Stage.EMAIL) == "email"
    assert Stage("archive") is Stage.ARCHIVE


def test_stage_metadata_covers_every_stage() -> None:
    for mapping in (STAGE_TITLES, STAGE_DESCRIPTIONS, STAGE_ICONS):
        assert set(mapping) == set(STAGE_ORDER)
        assert all(isinstance(value, str) and value for value in mapping.values())
    assert STAGE_TITLES[Stage.INSPECT] == "Inspect device"
    assert STAGE_TITLES[Stage.EMAIL] == "Send notification"
    assert STAGE_ICONS[Stage.SCAN] == "security-high"
    assert STAGE_ICONS[Stage.EMAIL] == "mail-send"
    assert STAGE_DESCRIPTIONS[Stage.VERDICT].startswith("Combine findings")


def test_stage_state_strings() -> None:
    assert {str(state) for state in StageState} == {
        "pending",
        "running",
        "done",
        "failed",
        "skipped",
        "cancelled",
    }
    assert str(StageState.PENDING) == "pending"
    assert str(StageState.FAILED) == "failed"
    assert StageState("cancelled") is StageState.CANCELLED


# ---------------------------------------------------------------------------
# verdicts and severity
# ---------------------------------------------------------------------------


def test_verdict_strings_and_safety() -> None:
    assert {str(verdict) for verdict in Verdict} == {
        "clean",
        "suspicious",
        "infected",
        "error",
        "cancelled",
    }
    assert str(Verdict.CLEAN) == "clean"
    assert Verdict.CLEAN.is_safe is True
    for verdict in Verdict:
        if verdict is not Verdict.CLEAN:
            assert verdict.is_safe is False, verdict


def test_verdict_labels_cover_every_member() -> None:
    assert set(VERDICT_LABELS) == set(Verdict)
    assert VERDICT_LABELS[Verdict.CLEAN] == "Safe"
    assert VERDICT_LABELS[Verdict.SUSPICIOUS] == "Suspicious"
    assert VERDICT_LABELS[Verdict.INFECTED] == "Infected"
    assert VERDICT_LABELS[Verdict.ERROR] == "Scan failed"
    assert VERDICT_LABELS[Verdict.CANCELLED] == "Cancelled"
    assert all(isinstance(label, str) and label for label in VERDICT_LABELS.values())


def test_severity_ranks_are_exact() -> None:
    assert severity_rank(Severity.INFO) == 0
    assert severity_rank(Severity.LOW) == 1
    assert severity_rank(Severity.MEDIUM) == 2
    assert severity_rank(Severity.HIGH) == 3
    assert severity_rank(Severity.CRITICAL) == 4


def test_severity_rank_accepts_plain_strings_and_defaults_to_zero() -> None:
    for severity in Severity:
        assert severity_rank(severity.value) == severity_rank(severity)
    assert severity_rank("critical") == 4
    assert severity_rank("low") == 1
    assert severity_rank("") == 0
    assert severity_rank("nonsense") == 0
    assert severity_rank("CRITICAL") == 0


def test_severity_strings() -> None:
    assert str(Severity.INFO) == "info"
    assert str(Severity.LOW) == "low"
    assert str(Severity.MEDIUM) == "medium"
    assert str(Severity.HIGH) == "high"
    assert str(Severity.CRITICAL) == "critical"
    assert {str(severity) for severity in Severity} == {
        "info",
        "low",
        "medium",
        "high",
        "critical",
    }


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def test_finding_to_dict_has_exact_keys_and_values() -> None:
    finding = Finding(
        path="/media/usb/docs/invoice.pdf.exe",
        threat="Win32.Trojan.Agent",
        engine="heuristic",
        severity=Severity.HIGH,
        category="executable",
        size=2048,
        sha256="b" * 64,
        detail="double extension hides an executable",
        line=12,
    )
    data = finding.to_dict()
    assert set(data) == {
        "path",
        "name",
        "threat",
        "engine",
        "severity",
        "severityRank",
        "category",
        "size",
        "sizeText",
        "sha256",
        "detail",
        "line",
    }
    assert data["path"] == "/media/usb/docs/invoice.pdf.exe"
    assert data["name"] == "invoice.pdf.exe"
    assert data["threat"] == "Win32.Trojan.Agent"
    assert data["engine"] == "heuristic"
    assert data["severity"] == "high"
    assert data["severityRank"] == 3
    assert data["category"] == "executable"
    assert data["size"] == 2048
    assert data["sizeText"] == human_bytes(2048) == "2.0 KiB"
    assert data["sha256"] == "b" * 64
    assert data["detail"] == "double extension hides an executable"
    assert data["line"] == 12


def test_finding_defaults_and_a_name_without_a_slash() -> None:
    finding = Finding(path="payload.txt", threat="DuckyScript", engine="heuristic")
    assert finding.severity is Severity.MEDIUM
    data = finding.to_dict()
    assert data["name"] == "payload.txt"
    assert data["severity"] == "medium"
    assert data["severityRank"] == 2
    assert data["category"] == "generic"
    assert data["size"] == 0
    assert data["sizeText"] == "0 B"
    assert data["sha256"] == ""
    assert data["detail"] == ""
    assert data["line"] == 0


# ---------------------------------------------------------------------------
# stage progress
# ---------------------------------------------------------------------------


def test_stage_progress_to_dict_maps_stage_and_formats_numbers() -> None:
    progress = StageProgress(
        stage=Stage.ARCHIVE,
        state=StageState.DONE,
        processed=3,
        total=10,
        bytes_processed=1024,
        bytes_total=4096,
        current="/media/usb/photos",
        message="packing",
        rate=512.0,
        elapsed=61.0,
        eta=None,
        percent=0.25,
    )
    data = progress.to_dict()
    assert set(data) == {
        "stage",
        "title",
        "icon",
        "state",
        "processed",
        "total",
        "bytesProcessed",
        "bytesTotal",
        "bytesProcessedText",
        "bytesTotalText",
        "current",
        "message",
        "rate",
        "rateText",
        "elapsed",
        "elapsedText",
        "eta",
        "etaText",
        "percent",
    }
    assert data["stage"] == "archive"
    assert data["title"] == STAGE_TITLES[Stage.ARCHIVE] == "Create archive image"
    assert data["icon"] == STAGE_ICONS[Stage.ARCHIVE] == "package-x-generic"
    assert data["state"] == "done"
    assert data["processed"] == 3
    assert data["total"] == 10
    assert data["bytesProcessed"] == 1024
    assert data["bytesTotal"] == 4096
    assert data["bytesProcessedText"] == "1.0 KiB"
    assert data["bytesTotalText"] == "4.0 KiB"
    assert data["current"] == "/media/usb/photos"
    assert data["message"] == "packing"
    assert data["rate"] == 512.0
    assert data["rateText"] == human_rate(512.0) == "512 B/s"
    assert data["elapsed"] == 61.0
    assert data["elapsedText"] == human_duration(61.0) == "01:01"
    assert data["eta"] == -1.0
    assert data["etaText"] == human_duration(None) == "--:--"
    assert data["percent"] == 0.25


def test_stage_progress_keeps_a_real_eta() -> None:
    progress = StageProgress(
        stage=Stage.SCAN,
        state=StageState.RUNNING,
        rate=1200.0,
        eta=8.5,
    )
    data = progress.to_dict()
    assert data["stage"] == "scan"
    assert data["title"] == "Antivirus scan"
    assert data["icon"] == "security-high"
    assert data["state"] == "running"
    assert data["eta"] == 8.5
    assert data["etaText"] == human_duration(8.5) == "00:08"
    assert data["rateText"] == human_rate(1200.0) == "1.2 KiB/s"
    assert data["bytesProcessedText"] == "0 B"
    assert data["percent"] == 0.0


# ---------------------------------------------------------------------------
# ProgressMeter
# ---------------------------------------------------------------------------


def test_progress_meter_first_snapshots_have_no_rate(clock: FakeClock) -> None:
    meter = ProgressMeter()
    first = meter.snapshot(Stage.SCAN, processed=0, total=10)
    assert first is not None
    assert first.stage is Stage.SCAN
    assert first.state is StageState.RUNNING
    assert first.percent == 0.0
    assert first.rate == 0.0
    assert first.eta is None
    assert first.elapsed == 0.0

    clock.advance(0.1)
    second = meter.snapshot(Stage.SCAN, processed=1, total=10)
    assert second is not None
    assert second.rate == 0.0
    assert second.eta is None
    assert second.percent == pytest.approx(0.1)


def test_progress_meter_rate_needs_min_samples_and_a_span(clock: FakeClock) -> None:
    meter = ProgressMeter(window=60.0, min_samples=3, min_interval=0.0)
    assert meter.snapshot(Stage.SCAN, processed=0).rate == 0.0
    clock.advance(0.05)
    assert meter.snapshot(Stage.SCAN, processed=50).rate == 0.0
    clock.advance(0.05)
    # three samples, but they only span 0.1 s: the span must exceed 0.2 s
    third = meter.snapshot(Stage.SCAN, processed=100)
    assert third is not None
    assert third.rate == 0.0
    clock.advance(0.15)
    fourth = meter.snapshot(Stage.SCAN, processed=300)
    assert fourth is not None
    assert fourth.rate == pytest.approx(300 / 0.25)
    assert fourth.rate == pytest.approx(1200.0)
    # no total was given, so the percentage stays at zero and there is no ETA
    assert fourth.percent == 0.0
    assert fourth.eta is None


def test_progress_meter_min_samples_is_respected(clock: FakeClock) -> None:
    meter = ProgressMeter(window=60.0, min_samples=4, min_interval=0.0)
    meter.snapshot(Stage.SCAN, processed=0)
    clock.advance(1.0)
    meter.snapshot(Stage.SCAN, processed=100)
    clock.advance(1.0)
    third = meter.snapshot(Stage.SCAN, processed=200)
    assert third is not None
    assert third.rate == 0.0
    clock.advance(1.0)
    fourth = meter.snapshot(Stage.SCAN, processed=300)
    assert fourth is not None
    assert fourth.rate == pytest.approx(100.0)


def test_progress_meter_percent_is_clamped_to_one(clock: FakeClock) -> None:
    meter = ProgressMeter(window=60.0, min_samples=3, min_interval=0.0)
    unknown_total = meter.snapshot(Stage.SCAN, processed=5)
    assert unknown_total is not None
    assert unknown_total.percent == 0.0
    clock.advance(1.0)
    meter.snapshot(Stage.SCAN, processed=50, total=10)
    clock.advance(1.0)
    clamped = meter.snapshot(Stage.SCAN, processed=50, total=10)
    assert clamped is not None
    assert clamped.percent == 1.0  # 50/10 would be 5.0 without the clamp
    assert clamped.rate == pytest.approx(22.5)  # (50 - 5) over the two second window
    assert clamped.eta is None  # nothing is left to do


def test_progress_meter_eta_uses_the_byte_rate(clock: FakeClock) -> None:
    meter = ProgressMeter(window=60.0, min_samples=3, min_interval=0.0)
    meter.snapshot(Stage.ARCHIVE, bytes_processed=0, bytes_total=10_000)
    clock.advance(1.0)
    meter.snapshot(Stage.ARCHIVE, bytes_processed=1_000, bytes_total=10_000)
    clock.advance(1.0)
    snapshot = meter.snapshot(Stage.ARCHIVE, bytes_processed=2_000, bytes_total=10_000)
    assert snapshot is not None
    assert snapshot.rate == pytest.approx(1000.0)
    assert snapshot.percent == pytest.approx(0.2)
    assert snapshot.eta == pytest.approx(8.0)
    assert snapshot.eta == pytest.approx((10_000 - 2_000) / 1000.0)


def test_progress_meter_eta_is_none_while_the_rate_is_slow(clock: FakeClock) -> None:
    meter = ProgressMeter(window=600.0, min_samples=3, min_interval=0.0)
    meter.snapshot(Stage.SCAN, bytes_processed=0, bytes_total=1_000)
    clock.advance(200.0)
    meter.snapshot(Stage.SCAN, bytes_processed=1, bytes_total=1_000)
    clock.advance(200.0)
    snapshot = meter.snapshot(Stage.SCAN, bytes_processed=2, bytes_total=1_000)
    assert snapshot is not None
    assert snapshot.rate == pytest.approx(0.005)
    assert snapshot.rate <= 1.0
    assert snapshot.eta is None
    assert snapshot.percent == pytest.approx(0.002)


def test_progress_meter_drops_samples_older_than_the_window(clock: FakeClock) -> None:
    meter = ProgressMeter(window=1.0, min_samples=3, min_interval=0.0)
    meter.snapshot(Stage.SCAN, processed=0)  # 9 s before the last snapshot
    clock.advance(5.0)
    meter.snapshot(Stage.SCAN, processed=5_000_000)
    clock.advance(3.0)
    meter.snapshot(Stage.SCAN, processed=5_000_000)
    clock.advance(0.5)
    meter.snapshot(Stage.SCAN, processed=5_000_500)
    clock.advance(0.5)
    snapshot = meter.snapshot(Stage.SCAN, processed=5_001_000)
    assert snapshot is not None
    # only the three samples from the last second count: that is 1000 files/s,
    # while the whole history would look like roughly 555666 files/s
    assert snapshot.rate == pytest.approx(1_000.0)
    assert snapshot.rate != pytest.approx(5_001_000 / 9.0)


def test_progress_meter_throttles_within_min_interval(clock: FakeClock) -> None:
    meter = ProgressMeter(min_interval=0.05)
    assert meter.snapshot(Stage.SCAN, processed=0, total=100) is not None
    clock.advance(0.01)
    assert meter.snapshot(Stage.SCAN, processed=1, total=100) is None
    forced = meter.snapshot(Stage.SCAN, processed=1, total=100, force=True)
    assert forced is not None
    assert forced.percent == pytest.approx(0.01)
    clock.advance(0.1)
    assert meter.snapshot(Stage.SCAN, processed=2, total=100) is not None
    clock.advance(0.001)
    assert meter.snapshot(Stage.SCAN, processed=3, total=100) is None
    # an explicit per-call interval of zero disables the throttle for that call
    assert meter.snapshot(Stage.SCAN, processed=3, total=100, min_interval=0.0) is not None


def test_progress_meter_always_reports_a_finished_stage(clock: FakeClock) -> None:
    meter = ProgressMeter(min_interval=5.0)
    assert meter.snapshot(Stage.SCAN, processed=0, total=10) is not None
    clock.advance(0.01)
    assert meter.snapshot(Stage.SCAN, processed=5, total=10) is None
    complete = meter.snapshot(Stage.SCAN, processed=10, total=10)
    assert complete is not None
    assert complete.percent == 1.0
    again = meter.snapshot(Stage.SCAN, processed=10, total=10)
    assert again is not None
    assert again.percent == 1.0


def test_progress_meter_stops_reporting_a_rate_when_every_sample_left_the_window(
    clock: FakeClock,
) -> None:
    meter = ProgressMeter(window=1.0, min_samples=3, min_interval=0.0)
    meter.snapshot(Stage.SCAN, processed=0)
    clock.advance(10.0)
    snapshot = meter.snapshot(Stage.SCAN, processed=10_000)
    assert snapshot is not None
    # the old samples are gone and the meter keeps at least two of them, which
    # is fewer than min_samples: a stale rate is never reported
    assert snapshot.rate == 0.0
    assert snapshot.eta is None


def test_progress_meter_reset_restarts_the_clock_and_clears_samples(clock: FakeClock) -> None:
    meter = ProgressMeter(window=60.0, min_samples=3, min_interval=0.0)
    meter.snapshot(Stage.SCAN, processed=0)
    clock.advance(0.5)
    meter.snapshot(Stage.SCAN, processed=1_000)
    clock.advance(0.5)
    running = meter.snapshot(Stage.SCAN, processed=2_000)
    assert running is not None
    assert running.rate == pytest.approx(2_000.0)
    assert running.elapsed == pytest.approx(1.0)
    assert meter.elapsed() == pytest.approx(1.0)

    clock.advance(30.0)
    meter.reset()
    assert meter.elapsed() == 0.0
    after_reset = meter.snapshot(Stage.SCAN, processed=2_000)
    assert after_reset is not None
    assert after_reset.rate == 0.0
    assert after_reset.elapsed == 0.0
    assert after_reset.percent == 0.0


# ---------------------------------------------------------------------------
# sinks
# ---------------------------------------------------------------------------


def test_null_sink_discards_everything() -> None:
    sink = NullSink()
    finding = Finding(path="/media/usb/eicar.com", threat="Eicar", engine="heuristic")
    progress = StageProgress(stage=Stage.SCAN, percent=0.5)
    assert sink.on_stage(Stage.SCAN, StageState.RUNNING, "scanning") is None
    assert sink.on_progress(progress) is None
    assert sink.on_finding(finding) is None
    assert sink.on_log("info", "hello") is None
    assert sink.on_summary({"verdict": "clean"}) is None


def test_sinks_satisfy_the_event_sink_protocol() -> None:
    assert isinstance(NullSink(), EventSink)
    assert isinstance(CollectingSink(), EventSink)
    assert isinstance(MultiSink(CollectingSink()), EventSink)


def test_collecting_sink_records_every_event() -> None:
    sink = CollectingSink()
    assert sink.stages == []
    assert sink.progress == []
    assert sink.findings == []
    assert sink.logs == []
    assert sink.summaries == []
    assert sink.max_percent == {}
    assert sink.stage_states() == {}
    assert sink.final_summary == {}

    finding = Finding(
        path="/media/usb/eicar.com",
        threat="Eicar-Test-Signature",
        engine="heuristic",
        severity=Severity.HIGH,
    )
    progress = StageProgress(stage=Stage.SCAN, percent=0.4)
    sink.on_stage(Stage.SCAN, StageState.RUNNING, "walking files")
    sink.on_progress(progress)
    sink.on_finding(finding)
    sink.on_log("warning", "skipped an unreadable file")
    sink.on_summary({"verdict": "infected"})

    assert sink.stages == [(Stage.SCAN, StageState.RUNNING, "walking files")]
    assert sink.progress == [progress]
    assert sink.findings == [finding]
    assert sink.logs == [("warning", "skipped an unreadable file")]
    assert sink.summaries == [{"verdict": "infected"}]
    assert sink.final_summary == {"verdict": "infected"}
    assert sink.max_percent == {Stage.SCAN: 0.4}
    assert sink.stage_states() == {Stage.SCAN: StageState.RUNNING}

    # the message defaults to the empty string
    sink.on_stage(Stage.SCAN, StageState.DONE)
    assert sink.stages[-1] == (Stage.SCAN, StageState.DONE, "")


def test_collecting_sink_keeps_max_percent_and_last_stage_state() -> None:
    sink = CollectingSink()
    for percent in (0.1, 0.75, 0.3):
        sink.on_progress(StageProgress(stage=Stage.ARCHIVE, percent=percent))
    sink.on_progress(StageProgress(stage=Stage.SCAN, percent=0.9))
    assert sink.max_percent == {Stage.ARCHIVE: 0.75, Stage.SCAN: 0.9}
    assert [item.percent for item in sink.progress] == [0.1, 0.75, 0.3, 0.9]

    sink.on_stage(Stage.SCAN, StageState.PENDING)
    sink.on_stage(Stage.SCAN, StageState.RUNNING)
    sink.on_stage(Stage.SCAN, StageState.DONE)
    sink.on_stage(Stage.EMAIL, StageState.SKIPPED)
    assert sink.stage_states() == {Stage.SCAN: StageState.DONE, Stage.EMAIL: StageState.SKIPPED}
    assert len(sink.stages) == 4


def test_collecting_sink_final_summary_is_the_last_one() -> None:
    sink = CollectingSink()
    first: dict[str, Any] = {"verdict": "clean"}
    second: dict[str, Any] = {"verdict": "infected", "findings": 3}
    sink.on_summary(first)
    assert sink.final_summary is first
    sink.on_summary(second)
    assert sink.final_summary is second
    assert sink.summaries == [first, second]


def test_multi_sink_fans_out_to_every_sink() -> None:
    first = CollectingSink()
    second = CollectingSink()
    multi = MultiSink(first, second)
    assert multi.sinks == [first, second]

    finding = Finding(path="/media/usb/payload.txt", threat="DuckyScript", engine="heuristic")
    progress = StageProgress(stage=Stage.SCAN, percent=0.5)
    multi.on_stage(Stage.SCAN, StageState.RUNNING, "scanning")
    multi.on_progress(progress)
    multi.on_finding(finding)
    multi.on_log("error", "engine missing")
    multi.on_summary({"verdict": "suspicious"})

    for sink in (first, second):
        assert sink.stages == [(Stage.SCAN, StageState.RUNNING, "scanning")]
        assert sink.progress == [progress]
        assert sink.findings == [finding]
        assert sink.logs == [("error", "engine missing")]
        assert sink.final_summary == {"verdict": "suspicious"}


def test_multi_sink_survives_a_broken_sink() -> None:
    broken = BrokenSink()
    healthy = CollectingSink()
    broken_first = MultiSink(broken, healthy)
    broken_first.on_stage(Stage.SCAN, StageState.RUNNING)
    broken_first.on_progress(StageProgress(stage=Stage.SCAN))
    broken_first.on_finding(Finding(path="x", threat="t", engine="e"))
    broken_first.on_log("info", "message")
    broken_first.on_summary({"verdict": "clean"})

    # the broken sink was really called for each event, and it never stopped the other one
    assert broken.calls == ["on_stage", "on_progress", "on_finding", "on_log", "on_summary"]
    assert len(healthy.stages) == 1
    assert len(healthy.progress) == 1
    assert len(healthy.findings) == 1
    assert healthy.logs == [("info", "message")]
    assert healthy.summaries == [{"verdict": "clean"}]

    broken_last = MultiSink(healthy, broken)
    broken_last.on_log("info", "second round")
    assert healthy.logs[-1] == ("info", "second round")


def test_multi_sink_skips_none_sinks() -> None:
    healthy = CollectingSink()
    multi = MultiSink(None, healthy, None)
    assert multi.sinks == [healthy]
    multi.on_log("info", "message")
    assert healthy.logs == [("info", "message")]

    empty = MultiSink()
    assert empty.sinks == []
    assert empty.on_stage(Stage.SCAN, StageState.RUNNING) is None
    assert empty.on_log("info", "message") is None
    assert MultiSink(None, None).sinks == []
