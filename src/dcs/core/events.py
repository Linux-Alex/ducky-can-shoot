"""The event model shared by the headless core and the GUI.

The pipeline knows nothing about Qt: it pushes plain dataclasses into an
EventSink.  The GUI bridge implements the same protocol and re-emits the
events as Qt signals, which is why the same code drives the CLI, the tests
and the live progress view without branching.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from dcs.core.util import human_bytes, human_duration, human_rate

# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


class Stage(str, Enum):
    """Pipeline stages, in execution order."""

    INSPECT = "inspect"
    SCAN = "scan"
    VERDICT = "verdict"
    ARCHIVE = "archive"
    UPLOAD = "upload"
    REPORT = "report"
    EMAIL = "email"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


STAGE_ORDER: tuple[Stage, ...] = (
    Stage.INSPECT,
    Stage.SCAN,
    Stage.VERDICT,
    Stage.ARCHIVE,
    Stage.UPLOAD,
    Stage.REPORT,
    Stage.EMAIL,
)

STAGE_TITLES: dict[Stage, str] = {
    Stage.INSPECT: "Inspect device",
    Stage.SCAN: "Antivirus scan",
    Stage.VERDICT: "Evaluate verdict",
    Stage.ARCHIVE: "Create archive image",
    Stage.UPLOAD: "Upload to cloud",
    Stage.REPORT: "Build report",
    Stage.EMAIL: "Send notification",
}

STAGE_DESCRIPTIONS: dict[Stage, str] = {
    Stage.INSPECT: "Collect drive metadata, check free space and pick the engines.",
    Stage.SCAN: "Walk every file and run the enabled antivirus engines.",
    Stage.VERDICT: "Combine findings, decide clean / suspicious / infected.",
    Stage.ARCHIVE: "Compress the drive contents into one image (optionally split).",
    Stage.UPLOAD: "Push the image to the configured free cloud provider.",
    Stage.REPORT: "Render the human readable HTML, text and CSV reports.",
    Stage.EMAIL: "Send the verdict and the share link over SMTP.",
}

STAGE_ICONS: dict[Stage, str] = {
    Stage.INSPECT: "drive-removable-media-usb",
    Stage.SCAN: "security-high",
    Stage.VERDICT: "dialog-ok-apply",
    Stage.ARCHIVE: "package-x-generic",
    Stage.UPLOAD: "cloud-upload",
    Stage.REPORT: "document-export",
    Stage.EMAIL: "mail-send",
}


class StageState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    def __str__(self) -> str:  # keep str() == value for logs, JSON and QML
        return self.value


# ---------------------------------------------------------------------------
# findings and verdicts
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    def __str__(self) -> str:
        return self.value


_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def severity_rank(severity: Severity | str) -> int:
    if isinstance(severity, str):
        try:
            severity = Severity(severity)
        except ValueError:
            return 0
    return _SEVERITY_RANK.get(severity, 0)


class Verdict(str, Enum):
    CLEAN = "clean"
    SUSPICIOUS = "suspicious"
    INFECTED = "infected"
    ERROR = "error"
    CANCELLED = "cancelled"

    def __str__(self) -> str:
        return self.value

    @property
    def is_safe(self) -> bool:
        return self is Verdict.CLEAN


VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.CLEAN: "Safe",
    Verdict.SUSPICIOUS: "Suspicious",
    Verdict.INFECTED: "Infected",
    Verdict.ERROR: "Scan failed",
    Verdict.CANCELLED: "Cancelled",
}


@dataclass(slots=True)
class Finding:
    """One reason to distrust a file."""

    path: str
    threat: str
    engine: str
    severity: Severity = Severity.MEDIUM
    category: str = "generic"
    size: int = 0
    sha256: str = ""
    detail: str = ""
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.path.rsplit("/", 1)[-1],
            "threat": self.threat,
            "engine": self.engine,
            "severity": str(self.severity),
            "severityRank": severity_rank(self.severity),
            "category": self.category,
            "size": self.size,
            "sizeText": human_bytes(self.size),
            "sha256": self.sha256,
            "detail": self.detail,
            "line": self.line,
        }


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StageProgress:
    """Snapshot of one stage's progress, ready for display."""

    stage: Stage
    state: StageState = StageState.RUNNING
    processed: int = 0
    total: int = 0
    bytes_processed: int = 0
    bytes_total: int = 0
    current: str = ""
    message: str = ""
    rate: float = 0.0
    elapsed: float = 0.0
    eta: float | None = None
    percent: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": str(self.stage),
            "title": STAGE_TITLES.get(self.stage, str(self.stage)),
            "icon": STAGE_ICONS.get(self.stage, "dialog-information"),
            "state": str(self.state),
            "processed": self.processed,
            "total": self.total,
            "bytesProcessed": self.bytes_processed,
            "bytesTotal": self.bytes_total,
            "bytesProcessedText": human_bytes(self.bytes_processed),
            "bytesTotalText": human_bytes(self.bytes_total),
            "current": self.current,
            "message": self.message,
            "rate": self.rate,
            "rateText": human_rate(self.rate),
            "elapsed": self.elapsed,
            "elapsedText": human_duration(self.elapsed),
            "eta": -1.0 if self.eta is None else self.eta,
            "etaText": human_duration(self.eta),
            "percent": self.percent,
        }


class ProgressMeter:
    """Turns raw counters into a rate, an ETA and a percentage.

    Smoothing matters here: scanning a USB stick alternates between thousands
    of tiny files and one huge ISO, so an unsmoothed rate jitters wildly.
    """

    def __init__(
        self,
        *,
        window: float = 8.0,
        min_samples: int = 3,
        min_interval: float = 0.05,
    ) -> None:
        self.window = window
        self.min_samples = min_samples
        #: smallest gap between two emitted snapshots (general.ui_refresh_ms)
        self.min_interval = min_interval
        self.started = time.monotonic()
        self._samples: list[tuple[float, int, int]] = []
        self._last_emit = 0.0

    def reset(self) -> None:
        self.started = time.monotonic()
        self._samples.clear()
        self._last_emit = 0.0

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started)

    def snapshot(
        self,
        stage: Stage,
        *,
        processed: int = 0,
        total: int = 0,
        bytes_processed: int = 0,
        bytes_total: int = 0,
        current: str = "",
        message: str = "",
        state: StageState = StageState.RUNNING,
        force: bool = False,
        min_interval: float | None = None,
    ) -> StageProgress | None:
        """Return a progress snapshot, or None when throttled."""
        if min_interval is None:
            min_interval = self.min_interval
        now = time.monotonic()
        self._samples.append((now, bytes_processed or processed, int(total > 0)))
        cutoff = now - self.window
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.pop(0)

        rate = 0.0
        if len(self._samples) >= self.min_samples:
            first_time, first_value, _ = self._samples[0]
            span = now - first_time
            if span > 0.2:
                rate = max(0.0, (bytes_processed or processed) - first_value) / span

        overall = bytes_total or total
        done = bytes_processed or processed
        fraction = 0.0 if overall <= 0 else max(0.0, min(1.0, done / overall))
        eta: float | None = None
        if rate > 1.0 and overall > done:
            eta = (overall - done) / rate

        if not force and now - self._last_emit < min_interval and fraction < 1.0:
            return None
        self._last_emit = now

        return StageProgress(
            stage=stage,
            state=state,
            processed=processed,
            total=total,
            bytes_processed=bytes_processed,
            bytes_total=bytes_total,
            current=current,
            message=message,
            rate=rate,
            elapsed=self.elapsed(),
            eta=eta,
            percent=fraction,
        )


# ---------------------------------------------------------------------------
# sinks
# ---------------------------------------------------------------------------


@runtime_checkable
class EventSink(Protocol):
    """Where pipeline events go."""

    def on_stage(self, stage: Stage, state: StageState, message: str = "") -> None: ...

    def on_progress(self, progress: StageProgress) -> None: ...

    def on_finding(self, finding: Finding) -> None: ...

    def on_log(self, level: str, message: str) -> None: ...

    def on_summary(self, summary: dict[str, Any]) -> None: ...


class NullSink:
    """Sink that throws everything away (used as a default)."""

    def on_stage(self, stage: Stage, state: StageState, message: str = "") -> None:
        pass

    def on_progress(self, progress: StageProgress) -> None:
        pass

    def on_finding(self, finding: Finding) -> None:
        pass

    def on_log(self, level: str, message: str) -> None:
        pass

    def on_summary(self, summary: dict[str, Any]) -> None:
        pass


class CollectingSink:
    """Sink that records everything; the tests assert against it."""

    def __init__(self) -> None:
        self.stages: list[tuple[Stage, StageState, str]] = []
        self.progress: list[StageProgress] = []
        self.findings: list[Finding] = []
        self.logs: list[tuple[str, str]] = []
        self.summaries: list[dict[str, Any]] = []
        self.max_percent: dict[Stage, float] = {}

    def on_stage(self, stage: Stage, state: StageState, message: str = "") -> None:
        self.stages.append((stage, state, message))

    def on_progress(self, progress: StageProgress) -> None:
        self.progress.append(progress)
        self.max_percent[progress.stage] = max(
            self.max_percent.get(progress.stage, 0.0), progress.percent
        )

    def on_finding(self, finding: Finding) -> None:
        self.findings.append(finding)

    def on_log(self, level: str, message: str) -> None:
        self.logs.append((level, message))

    def on_summary(self, summary: dict[str, Any]) -> None:
        self.summaries.append(summary)

    # convenience helpers -------------------------------------------------
    @property
    def final_summary(self) -> dict[str, Any]:
        return self.summaries[-1] if self.summaries else {}

    def stage_states(self) -> dict[Stage, StageState]:
        states: dict[Stage, StageState] = {}
        for stage, state, _ in self.stages:
            states[stage] = state
        return states


class MultiSink:
    """Fan out to several sinks (e.g. log file + GUI)."""

    def __init__(self, *sinks: EventSink) -> None:
        self.sinks: list[EventSink] = [s for s in sinks if s is not None]

    def _each(self, method: str, *args: Any) -> None:
        for sink in self.sinks:
            # a broken sink must not stop the job
            with contextlib.suppress(Exception):
                getattr(sink, method)(*args)

    def on_stage(self, stage: Stage, state: StageState, message: str = "") -> None:
        self._each("on_stage", stage, state, message)

    def on_progress(self, progress: StageProgress) -> None:
        self._each("on_progress", progress)

    def on_finding(self, finding: Finding) -> None:
        self._each("on_finding", finding)

    def on_log(self, level: str, message: str) -> None:
        self._each("on_log", level, message)

    def on_summary(self, summary: dict[str, Any]) -> None:
        self._each("on_summary", summary)


@dataclass(slots=True)
class ScanStats:
    """Accumulated numbers for the scan stage."""

    files_scanned: int = 0
    bytes_scanned: int = 0
    files_skipped: int = 0
    bytes_total: int = 0
    files_total: int = 0
    errors: int = 0
    duration: float = 0.0
    engines: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "filesScanned": self.files_scanned,
            "bytesScanned": self.bytes_scanned,
            "filesSkipped": self.files_skipped,
            "filesTotal": self.files_total,
            "bytesTotal": self.bytes_total,
            "errors": self.errors,
            "duration": self.duration,
            "durationText": human_duration(self.duration),
            "engines": list(self.engines),
            "bytesScannedText": human_bytes(self.bytes_scanned),
            "bytesTotalText": human_bytes(self.bytes_total),
        }
