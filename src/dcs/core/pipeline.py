"""The job orchestrator: inspect, scan, archive, upload, report, notify.

Everything that has to happen between "a USB stick is plugged in" and "the
recipient got an email" lives here.  The pipeline is Qt-free and reports its
progress exclusively through the event sink, which is what lets the GUI, the
CLI and the tests drive the exact same code.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dcs.core.archiver import Archiver, archive_name, plan_source
from dcs.core.config import AppConfig
from dcs.core.device import (
    BlockDevice,
    device_for_path,
    find_device,
    is_removable_mount,
    mount_device,
)
from dcs.core.events import (
    STAGE_ORDER,
    EventSink,
    NullSink,
    ProgressMeter,
    Stage,
    StageProgress,
    StageState,
    Verdict,
)
from dcs.core.log import get_logger
from dcs.core.notify import Notifier
from dcs.core.report import write_reports
from dcs.core.result import JobResult, StageTiming
from dcs.core.scanner import Scanner, ScanResult
from dcs.core.upload import build_provider
from dcs.core.util import atomic_write_text, free_bytes, human_bytes, iso_now, sha256_file
from dcs.version import __version__

log = get_logger("pipeline")


@dataclass
class JobRequest:
    """What the user asked for, independent of how it was asked."""

    source_path: str
    label: str = ""
    #: device node to mount first (empty when source_path is already a directory)
    device_path: str = ""
    #: stage overrides; None means "use the configuration"
    do_scan: bool = True
    do_archive: bool | None = None
    do_upload: bool | None = None
    do_email: bool | None = None
    recipients: list[str] = field(default_factory=list)
    #: write the email to disk instead of sending it
    email_dry_run: bool = False
    #: skip the "are you sure" checks the CLI/GUI would otherwise do
    assume_yes: bool = False
    archive_format: str = ""
    output_dir: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRequest:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class Pipeline:
    """Runs one complete triage job."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------
    def run(
        self,
        request: JobRequest,
        *,
        sink: EventSink | None = None,
        cancel: threading.Event | None = None,
    ) -> JobResult:
        sink = sink or NullSink()
        cancel = cancel or threading.Event()
        started = time.monotonic()

        result = JobResult(
            source_path=request.source_path,
            source_label=request.label,
            started_at=iso_now(),
            app_version=__version__,
            config_snapshot=self.config.to_dict(mask_secrets=True),
        )
        context = _RunContext(pipeline=self, request=request, sink=sink, cancel=cancel, result=result)

        for stage, state, message in context.stages_pending():
            sink.on_stage(stage, state, message)

        try:
            context.inspect()
            if not cancel.is_set() and request.do_scan:
                context.scan()
            context.decide()
            if not cancel.is_set():
                context.archive()
            if not cancel.is_set():
                context.upload()
            context.report()
            if not cancel.is_set():
                context.notify()
        except Exception as exc:
            log.exception("pipeline failed")
            result.warnings.append(f"internal error: {exc}")
            sink.on_log("error", f"internal error: {exc}")
            context.fail(exc)
            if result.verdict is Verdict.CLEAN and result.scan is None:
                result.verdict = Verdict.ERROR
        finally:
            # stages that were never reached must not stay "pending" in the live
            # view: say why they did not run
            context.settle_pending(
                "the run was cancelled" if cancel.is_set() else "the run ended before this stage"
            )
            try:
                if not result.report_paths and context.config.report.enabled:
                    context.report()
            except Exception as exc:
                log.warning("could not write a failure report: %s", exc)
            context.refresh_timings()
            result.cancelled = cancel.is_set()
            result.finished_at = iso_now()
            result.duration = time.monotonic() - started
            summary = result.to_dict()
            _refresh_json_report(result, summary)
            sink.on_summary(summary)
            sink.on_log(
                "info",
                f"job {result.job_id} finished: {result.verdict.value} in {result.duration:.1f}s",
            )
        return result


def _refresh_json_report(result: JobResult, summary: dict[str, Any]) -> None:
    """Rewrite the JSON report so it carries the final state of the job.

    The file is produced during the report stage, which is by definition before
    the email outcome and the last timings exist.  Rewriting it once more at the
    end keeps the machine readable artefact consistent with what the CLI prints.
    """
    path = result.report_paths.get("json")
    if not path:
        return
    try:
        atomic_write_text(path, json.dumps(summary, indent=2, ensure_ascii=False))
    except OSError as exc:  # pragma: no cover - disk full, read only target
        log.warning("could not refresh %s: %s", path, exc)


class _RunContext:
    """Mutable state of one run; one instance per Pipeline.run call."""

    def __init__(
        self,
        *,
        pipeline: Pipeline,
        request: JobRequest,
        sink: EventSink,
        cancel: threading.Event,
        result: JobResult,
    ) -> None:
        self.pipeline = pipeline
        self.config = pipeline.config
        self.request = request
        self.sink = sink
        self.cancel = cancel
        self.result = result
        self.timings: dict[Stage, StageTiming] = {}
        self.device: BlockDevice | None = None
        self.scan_result: ScanResult | None = None
        self._started: dict[Stage, float] = {}
        self._current: Stage | None = None

    # -- stage helpers ---------------------------------------------------
    def stages_pending(self) -> list[tuple[Stage, StageState, str]]:
        order = [Stage.INSPECT, Stage.SCAN, Stage.VERDICT, Stage.ARCHIVE, Stage.UPLOAD, Stage.REPORT, Stage.EMAIL]
        return [(stage, StageState.PENDING, "") for stage in order]

    def _begin(self, stage: Stage, message: str = "") -> None:
        self._started[stage] = time.monotonic()
        self._current = stage
        self.sink.on_stage(stage, StageState.RUNNING, message)
        self._record(stage, "running", message)

    def refresh_timings(self) -> None:
        """Publish the timings list in pipeline order, including the last stages.

        The report stage writes its files while it is still running, so the list
        it captured is refreshed here once every stage has settled.
        """
        self.result.timings = [
            self.timings[stage] for stage in STAGE_ORDER if stage in self.timings
        ]

    def settle_pending(self, reason: str) -> None:
        """Mark every stage that never ran, so the UI shows no stale rows."""
        for stage in STAGE_ORDER:
            if stage not in self.timings:
                self._skip(stage, reason)

    def fail(self, error: Exception | str) -> None:
        """Mark the stage that was running when a fatal error happened."""
        message = str(error)
        stage = self._current or Stage.INSPECT
        self._finish(stage, StageState.FAILED, message)
        self._current = None

    def _finish(
        self,
        stage: Stage,
        state: StageState,
        message: str = "",
        seconds: float | None = None,
    ) -> None:
        started = self._started.get(stage, time.monotonic())
        elapsed = seconds if seconds is not None else time.monotonic() - started
        self._record(stage, state.value, message, elapsed)
        if self._current is stage:
            self._current = None
        self.sink.on_stage(stage, state, message)

    def _record(self, stage: Stage, state: str, message: str, seconds: float | None = None) -> None:
        timing = self.timings.get(stage) or StageTiming(str(stage), state, 0.0, message)
        timing.state = state
        if message:
            timing.message = message
        if seconds is not None:
            timing.seconds = seconds
        self.timings[stage] = timing

    def _skip(self, stage: Stage, reason: str) -> None:
        self._record(stage, "skipped", reason)
        self.sink.on_stage(stage, StageState.SKIPPED, reason)
        self.sink.on_log("info", f"{stage.value} skipped: {reason}")

    # ------------------------------------------------------------------
    # 1. inspect
    # ------------------------------------------------------------------
    def inspect(self) -> None:
        stage = Stage.INSPECT
        self._begin(stage, "collecting device information")
        request = self.request

        node = request.device_path
        if not node and request.source_path.startswith("/dev/"):
            node = request.source_path
            request.source_path = ""

        if node:
            device = find_device(node)
            if device is None:
                raise RuntimeError(f"device {node} does not exist")
            self.device = device
            if not device.mounted:
                self.sink.on_log("info", f"mounting {device.path} through udisks2")
                ok, message = mount_device(device)
                if not ok:
                    raise RuntimeError(f"could not mount {device.path}: {message}")
                device = find_device(node) or device
                self.device = device
            request.source_path = device.mount_path
        elif request.source_path:
            self.device = device_for_path(request.source_path)

        source = os.path.abspath(request.source_path or "")
        if not source or not os.path.isdir(source):
            raise RuntimeError(f"{request.source_path!r} is not a readable directory")
        self.result.source_path = source
        if self.device is not None:
            self.result.device = self.device.to_dict()
            if not self.result.source_label:
                self.result.source_label = self.device.display_name
        if not self.result.source_label:
            self.result.source_label = os.path.basename(source.rstrip("/")) or "root"

        # advisory, never blocking: the tool also works on a plain folder
        if self.device is None:
            self.result.warnings.append(
                "the source is a plain directory, not a removable device: "
                "the report will not contain any drive metadata"
            )
        elif not is_removable_mount(source):
            self.result.warnings.append(
                f"{source} is not on removable media; make sure you really want to triage it"
            )

        output_dir = self.request.output_dir or str(self.config.output_dir)
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            self.result.warnings.append(f"cannot create the output directory {output_dir}: {exc}")
        available = free_bytes(output_dir if os.path.isdir(output_dir) else os.path.dirname(source))
        self.sink.on_log(
            "info",
            f"source {source} ({self.result.source_label}); "
            f"{human_bytes(available)} free in {output_dir} for the archive",
        )
        self._finish(stage, StageState.DONE, f"{self.result.source_label} at {source}")

    # ------------------------------------------------------------------
    # 2. scan
    # ------------------------------------------------------------------
    def scan(self) -> None:
        stage = Stage.SCAN
        scanner = Scanner(
            self.config.scan,
            collect_files=self.config.report.include_file_list,
            ui_interval_ms=self.config.general.ui_refresh_ms,
        )
        statuses = scanner.statuses()
        available = [s for s in statuses if s.available]
        if not available:
            self._skip(stage, "no scan engine is available")
            self.scan_result = ScanResult(root=self.result.source_path, verdict=Verdict.ERROR)
            self.scan_result.engines = statuses
            self.result.scan = self.scan_result
            self.result.verdict = Verdict.ERROR
            self.result.warnings.append("no usable antivirus engine: the medium was not scanned")
            return

        self.scan_result = scanner.run(self.result.source_path, sink=self.sink, cancel=self.cancel)
        self.result.scan = self.scan_result
        self.result.verdict = self.scan_result.verdict
        self._finish(
            stage,
            StageState.CANCELLED if self.cancel.is_set() else StageState.DONE,
            f"{len(self.scan_result.findings)} findings in "
            f"{self.scan_result.stats.files_scanned} files ({self.scan_result.verdict.value})",
            self.scan_result.stats.duration,
        )
        for warning in self.scan_result.warnings:
            if warning not in self.result.warnings:
                self.result.warnings.append(warning)

    # ------------------------------------------------------------------
    # 3. verdict
    # ------------------------------------------------------------------
    def decide(self) -> None:
        stage = Stage.VERDICT
        self._begin(stage, "evaluating the findings")
        if self.scan_result is None:
            self.result.verdict = Verdict.ERROR
            self._finish(stage, StageState.FAILED, "the scan did not run")
            return
        verdict = self.scan_result.verdict
        self.result.verdict = verdict
        detail = {
            Verdict.CLEAN: "no threats found",
            Verdict.SUSPICIOUS: f"{len(self.scan_result.findings)} suspicious item(s)",
            Verdict.INFECTED: f"{self.result.threat_count} threatening file(s)",
            Verdict.ERROR: "the scan could not complete",
            Verdict.CANCELLED: "cancelled by the user",
        }.get(verdict, str(verdict))
        if self.cancel.is_set():
            self._finish(stage, StageState.CANCELLED, "the run was cancelled")
        else:
            self._finish(stage, StageState.DONE, detail)

    # ------------------------------------------------------------------
    # 4. archive
    # ------------------------------------------------------------------
    def _archive_wanted(self) -> tuple[bool, str]:
        config = self.config.archive
        if self.request.do_archive is False:
            return False, "disabled for this run"
        if self.request.do_archive is None and not config.enabled:
            return False, "archiving is disabled in the configuration"
        mode = config.when
        if mode == "never":
            return False, "archive.when is set to never"
        if mode == "clean" and self.result.verdict is not Verdict.CLEAN:
            return False, (
                f"the medium is {self.result.verdict.value}: no archive was created and nothing "
                "was uploaded, so malware never leaves this machine"
            )
        return True, ""

    def archive(self) -> None:
        stage = Stage.ARCHIVE
        wanted, reason = self._archive_wanted()
        if not wanted:
            self.result.upload_skipped_reason = reason
            self._skip(stage, reason)
            return

        fmt = self.request.archive_format or self.config.archive.format
        archiver = Archiver(self.config.archive)
        if fmt == "7z" and not archiver.tool_for("7z") and self.config.archive.format != "7z":
            fmt = "zip"

        self._begin(stage, "indexing the medium")
        total_files, total_bytes, size_map = plan_source(self.result.source_path)
        self.sink.on_log(
            "info",
            f"archive source: {total_files} files, {human_bytes(total_bytes)}",
        )
        if not total_files:
            self.result.upload_skipped_reason = "the medium is empty: nothing to archive"
            self._skip(stage, self.result.upload_skipped_reason)
            return

        warn_bytes = self.config.archive.size_warn_gb * 1024**3
        if warn_bytes and total_bytes > warn_bytes:
            message = (
                f"the medium holds {human_bytes(total_bytes)}, above the configured "
                f"{self.config.archive.size_warn_gb} GiB warning threshold: the archive will be large "
                "and free hosts may refuse it (see archive.split_size_mb)"
            )
            self.result.warnings.append(message)
            self.sink.on_log("warning", message)

        label = self.result.source_label if self.config.archive.use_device_label else "usb"
        name = archive_name(label or "usb", fmt=fmt)
        output_dir = self.request.output_dir or str(self.config.output_dir)
        os.makedirs(output_dir, exist_ok=True)

        archive = archiver.create(
            self.result.source_path,
            output_dir,
            name=name,
            sink=self.sink,
            cancel=self.cancel,
            total_files=total_files,
            total_bytes=total_bytes,
            size_map=size_map,
        )
        self.result.archive = archive
        if not archive.ok:
            self._finish(stage, StageState.FAILED, archive.error or "archive failed")
            self.result.upload_skipped_reason = archive.error or "the archive failed"
            self.result.warnings.append(self.result.upload_skipped_reason)
            return

        self.sink.on_log("info", f"hashing {os.path.basename(archive.primary_path)} for the report")
        try:
            self.result.archive_sha256 = sha256_file(archive.primary_path)
        except OSError as exc:
            log.warning("cannot hash the archive: %s", exc)

        self._finish(
            stage,
            StageState.DONE,
            f"{len(archive.parts)} volume(s), {human_bytes(archive.archive_bytes)}",
            archive.duration,
        )

    # ------------------------------------------------------------------
    # 5. upload
    # ------------------------------------------------------------------
    def _upload_wanted(self) -> tuple[bool, str]:
        config = self.config.upload
        if self.request.do_upload is False:
            return False, "disabled for this run"
        if self.request.do_upload is None and not config.enabled:
            return False, "uploading is disabled in the configuration"
        if self.result.archive is None or not self.result.archive.ok:
            return False, self.result.upload_skipped_reason or "there is no archive to upload"
        mode = config.upload_when
        if mode == "never":
            return False, "upload.upload_when is set to never"
        if mode == "clean" and self.result.verdict is not Verdict.CLEAN:
            return False, (
                f"the medium is {self.result.verdict.value}: nothing was uploaded, "
                "so no link to malicious files can leak"
            )
        if self.result.verdict is not Verdict.CLEAN and not self.request.assume_yes:
            return False, (
                "refusing to publish a suspicious image without confirmation "
                "(pass --yes or set upload.upload_when = always)"
            )
        return True, ""

    def upload(self) -> None:
        stage = Stage.UPLOAD
        wanted, reason = self._upload_wanted()
        if not wanted:
            if not self.result.upload_skipped_reason:
                self.result.upload_skipped_reason = reason
            self._skip(stage, reason)
            return

        assert self.result.archive is not None
        provider = build_provider(
            self.config.upload,
            secrets={
                "gofile_token": self.config.gofile_token(),
                "pixeldrain_api_key": self.config.pixeldrain_api_key(),
            },
        )
        usable, detail = provider.availability()
        if not usable:
            self.result.upload_skipped_reason = f"{provider.label} is not usable: {detail}"
            self._finish(stage, StageState.FAILED, self.result.upload_skipped_reason)
            self.result.warnings.append(self.result.upload_skipped_reason)
            return

        parts = self.result.archive.parts
        self._begin(stage, f"uploading {len(parts)} volume(s) to {provider.label}")
        self.sink.on_log(
            "warning",
            f"the file(s) will be publicly reachable by anyone who has the link ({provider.label})",
        )
        if len(parts) > 1 and hasattr(provider, "create_folder"):
            try:
                prefix = self.config.upload.gofile.folder_prefix or "DCS"
                provider.create_folder(  # type: ignore[attr-defined]
                    f"{prefix} {self.result.source_label} {self.result.job_id}"
                )
            except Exception as exc:
                log.warning("folder grouping unavailable: %s", exc)

        meter = ProgressMeter()
        total = sum(part.size for part in parts)
        uploaded = 0
        started = time.monotonic()

        for index, part in enumerate(parts, start=1):
            if self.cancel.is_set():
                break
            label = f"part {index}/{len(parts)}" if len(parts) > 1 else ""
            done_so_far = uploaded

            def report_progress(
                sent: int,
                _total: int,
                *,
                base: int = done_so_far,
                path: str = part.path,
            ) -> None:
                # an upload has no meaningful file count, so only bytes are reported.
                # base and path are bound as defaults so the closure cannot pick up
                # the next loop iteration's values.
                snapshot = meter.snapshot(
                    Stage.UPLOAD,
                    processed=0,
                    total=0,
                    bytes_processed=base + sent,
                    bytes_total=total,
                    current=os.path.basename(path),
                    message=f"{human_bytes(base + sent)} of {human_bytes(total)} sent",
                )
                if snapshot is not None:
                    self.sink.on_progress(snapshot)

            upload = provider.upload(
                part.path,
                progress=report_progress,
                cancel=self.cancel,
                part_label=label,
            )
            self.result.uploads.append(upload)
            uploaded += part.size if upload.ok else 0
            if upload.ok:
                self.sink.on_log("info", f"uploaded {os.path.basename(part.path)} -> {upload.link}")
            else:
                self.sink.on_log("error", f"upload of {os.path.basename(part.path)} failed: {upload.error}")
                break

        links = self.result.upload_links
        folder_link = provider.folder_link() if hasattr(provider, "folder_link") else ""  # type: ignore[attr-defined]
        if links and len(links) == len(parts) and folder_link and len(parts) > 1:
            self.result.share_link = folder_link
        elif links:
            self.result.share_link = links[0] if len(links) == 1 else "\n".join(links)

        elapsed = time.monotonic() - started
        if links:
            self._finish(stage, StageState.DONE, f"{len(links)} link(s) via {provider.label}", elapsed)
            self.sink.on_progress(
                StageProgress(
                    stage=stage,
                    state=StageState.DONE,
                    processed=0,
                    total=0,
                    bytes_processed=total,
                    bytes_total=total,
                    message=f"{len(links)} link(s) via {provider.label}",
                    elapsed=elapsed,
                    percent=1.0,
                )
            )
        else:
            error = next((u.error for u in self.result.uploads if u.error), "upload failed")
            self.result.upload_skipped_reason = f"upload failed: {error}"
            self.result.warnings.append(self.result.upload_skipped_reason)
            self._finish(stage, StageState.FAILED, self.result.upload_skipped_reason, elapsed)

        remove_source = (
            self.config.upload.delete_archive_after_upload or not self.config.general.keep_archive
        )
        if remove_source and len(links) == len(parts):
            for part in parts:
                try:
                    os.unlink(part.path)
                except OSError as exc:
                    log.warning("cannot delete %s: %s", part.path, exc)

    # ------------------------------------------------------------------
    # 6. report
    # ------------------------------------------------------------------
    def report(self) -> None:
        stage = Stage.REPORT
        if not self.config.report.enabled:
            self._skip(stage, "report generation is disabled")
            return
        self._begin(stage, "rendering the report")
        self.result.timings = [self.timings[key] for key in sorted(self.timings, key=lambda s: list(Stage).index(s))]
        directory = str(self.config.reports_dir)
        started = time.monotonic()
        paths = write_reports(self.result, self.config.report, directory)
        self.result.report_paths = paths.existing()
        if not paths.html:
            self._finish(stage, StageState.FAILED, f"could not write reports into {directory}")
            return
        self._finish(
            stage,
            StageState.DONE,
            f"{len(paths.existing())} file(s) in {directory}",
            time.monotonic() - started,
        )
        self.sink.on_log("info", "report written: " + ", ".join(os.path.basename(p) for p in paths.existing().values()))

    # ------------------------------------------------------------------
    # 7. notify
    # ------------------------------------------------------------------
    def _email_wanted(self) -> tuple[bool, str]:
        config = self.config.email
        if self.request.do_email is False:
            return False, "disabled for this run"
        if self.request.do_email is None and not config.enabled:
            return False, "email notifications are disabled in the configuration"
        recipients = self.request.recipients or config.to_addrs
        if not recipients:
            return False, "no recipient address is configured"
        if not config.always_send and self.result.verdict is Verdict.CLEAN:
            return False, (
                "email.always_send is false and the medium is clean, so no message was sent"
            )
        return True, ""

    def notify(self) -> None:
        stage = Stage.EMAIL
        wanted, reason = self._email_wanted()
        if not wanted:
            self._skip(stage, reason)
            return

        config = self.config.email
        recipients = self.request.recipients or config.to_addrs
        if self.request.recipients:
            config.to_addrs = list(dict.fromkeys(recipients))

        self._begin(stage, f"notifying {', '.join(recipients)}")
        notifier = Notifier(
            config,
            password=config.password if self.request.email_dry_run else self.config.smtp_password(),
            username=self.config.smtp_username(),
        )
        attachments: list[str] = []
        if config.attach_report:
            primary = {
                "text": ["text"],
                "both": ["html", "text"],
            }.get(self.config.report.format, ["html"])
            attachments = [self.result.report_paths.get(kind, "") for kind in primary]
            attachments.append(self.result.report_paths.get("csv", ""))
            attachments = [path for path in attachments if path]
        outcome = notifier.send(self.result, attachments=attachments, dry_run=self.request.email_dry_run)
        self.result.email = outcome

        if outcome.ok and outcome.dry_run:
            self._finish(stage, StageState.DONE, f"dry run message written to {outcome.message_path}")
        elif outcome.ok:
            self._finish(stage, StageState.DONE, f"sent to {', '.join(outcome.recipients)}")
        else:
            self._finish(stage, StageState.FAILED, outcome.error or "delivery failed")
            self.result.warnings.append(f"email not delivered: {outcome.error}")
