"""The single result object a finished job produces.

Everything the report, the email, the CLI and the GUI show is derived from
this structure, so there is exactly one place that decides what "the result of
a job" means.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from dcs.core.archiver import ArchiveResult
from dcs.core.events import Severity, Verdict, severity_rank
from dcs.core.scanner import ScanResult
from dcs.core.upload.base import UploadResult
from dcs.core.util import human_bytes, human_duration


@dataclass
class EmailResult:
    """Outcome of the notification stage."""

    ok: bool = False
    sent: bool = False
    dry_run: bool = False
    recipients: list[str] = field(default_factory=list)
    subject: str = ""
    server: str = ""
    duration: float = 0.0
    error: str = ""
    message_path: str = ""
    body_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sent": self.sent,
            "dryRun": self.dry_run,
            "recipients": list(self.recipients),
            "subject": self.subject,
            "server": self.server,
            "duration": self.duration,
            "error": self.error,
            "messagePath": self.message_path,
            "bodyPreview": self.body_preview,
        }


@dataclass
class StageTiming:
    """How long one stage took, for the timeline in the report."""

    stage: str
    state: str = "pending"
    seconds: float = 0.0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "state": self.state,
            "seconds": round(self.seconds, 3),
            "secondsText": human_duration(self.seconds),
            "message": self.message,
        }


@dataclass
class JobResult:
    """Everything that happened during one USB triage run."""

    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source_path: str = ""
    source_label: str = ""
    device: dict[str, Any] = field(default_factory=dict)
    verdict: Verdict = Verdict.CLEAN
    scan: ScanResult | None = None
    archive: ArchiveResult | None = None
    archive_sha256: str = ""
    uploads: list[UploadResult] = field(default_factory=list)
    upload_skipped_reason: str = ""
    share_link: str = ""
    report_paths: dict[str, str] = field(default_factory=dict)
    email: EmailResult | None = None
    warnings: list[str] = field(default_factory=list)
    timings: list[StageTiming] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration: float = 0.0
    cancelled: bool = False
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    app_version: str = ""

    # -- derived ----------------------------------------------------------
    @property
    def safe(self) -> bool:
        return self.verdict is Verdict.CLEAN

    @property
    def findings(self) -> list[Any]:
        return list(self.scan.findings) if self.scan else []

    @property
    def findings_sorted(self) -> list[Any]:
        return sorted(
            self.findings,
            key=lambda f: (-severity_rank(f.severity), f.path),
        )

    @property
    def threat_count(self) -> int:
        return sum(1 for f in self.findings if severity_rank(f.severity) >= severity_rank(Severity.HIGH))

    @property
    def files_scanned(self) -> int:
        return self.scan.stats.files_scanned if self.scan else 0

    @property
    def bytes_scanned(self) -> int:
        return self.scan.stats.bytes_scanned if self.scan else 0

    @property
    def archive_bytes(self) -> int:
        return self.archive.archive_bytes if self.archive else 0

    @property
    def upload_links(self) -> list[str]:
        return [u.link for u in self.uploads if u.ok and u.link]

    @property
    def stage_states(self) -> dict[str, str]:
        return {timing.stage: timing.state for timing in self.timings}

    def failed_stages(self) -> list[StageTiming]:
        return [t for t in self.timings if t.state == "failed"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "appVersion": self.app_version,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "duration": self.duration,
            "durationText": human_duration(self.duration),
            "sourcePath": self.source_path,
            "sourceLabel": self.source_label,
            "device": dict(self.device),
            "verdict": str(self.verdict),
            "safe": self.safe,
            "threatCount": self.threat_count,
            "findingCount": len(self.findings),
            "filesScanned": self.files_scanned,
            "bytesScanned": self.bytes_scanned,
            "bytesScannedText": human_bytes(self.bytes_scanned),
            "archive": self.archive.to_dict() if self.archive else None,
            "archiveBytes": self.archive_bytes,
            "archiveBytesText": human_bytes(self.archive_bytes),
            "archiveSha256": self.archive_sha256,
            "uploads": [u.to_dict() for u in self.uploads],
            "uploadLinks": self.upload_links,
            "shareLink": self.share_link,
            "uploadSkippedReason": self.upload_skipped_reason,
            "reports": dict(self.report_paths),
            "email": self.email.to_dict() if self.email else None,
            "warnings": list(self.warnings),
            "timings": [t.to_dict() for t in self.timings],
            "cancelled": self.cancelled,
            "scan": self.scan.to_dict() if self.scan else None,
        }
