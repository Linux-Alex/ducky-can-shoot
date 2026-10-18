"""Scan orchestration: walk the medium, run the engines, decide a verdict.

The scanner is the heart of DCS.  It is intentionally Qt-free so that the CLI,
the tests and the GUI all execute exactly the same code path.

Two passes over the drive
-------------------------
1. an enumeration pass that only counts files and bytes (so the progress bar
   knows what 100% means), then
2. a streaming scan pass that batches files and hands them to the engines.

The file list is never held in memory, which matters on a 1 TB stick with a
million small files.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from dcs.core.config import ScanConfig
from dcs.core.events import (
    EventSink,
    Finding,
    NullSink,
    ProgressMeter,
    ScanStats,
    Severity,
    Stage,
    StageProgress,
    StageState,
    Verdict,
    severity_rank,
)
from dcs.core.log import get_logger
from dcs.core.scanner.base import EngineStatus, ScanContext
from dcs.core.scanner.clamav import ClamAVEngine
from dcs.core.scanner.heuristic import HeuristicEngine, load_hash_db
from dcs.core.scanner.intel import IntelEngine
from dcs.core.scanner.yara import YaraEngine
from dcs.core.util import FileEntry, human_bytes, iso_now, walk_files

log = get_logger("scanner")

#: an unbounded list would blow up the report on a drive with a million files
FILE_LIST_LIMIT = 20000

#: how long the scan waits for a tree engine (YARA, hash intelligence) before it
#: gives up on it.  Bounded on purpose: a provider that stops answering must not
#: hold a finished scan hostage.
TREE_ENGINE_GRACE_S = 900

#: display names for every engine id, including the ones a run does not use
ENGINE_LABELS = {
    "clamav": ClamAVEngine.label,
    "heuristic": HeuristicEngine.label,
    "yara": YaraEngine.label,
    "intel": IntelEngine.label,
}

__all__ = [
    "ENGINE_LABELS",
    "ClamAVEngine",
    "EngineStatus",
    "HeuristicEngine",
    "IntelEngine",
    "ScanResult",
    "Scanner",
    "YaraEngine",
    "compute_verdict",
]


@dataclass
class ScanResult:
    """Everything the scan learned, in a form the report can paste."""

    root: str
    verdict: Verdict = Verdict.CLEAN
    findings: list[Finding] = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)
    engines: list[EngineStatus] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    skipped_large: list[tuple[str, int]] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    cancelled: bool = False
    #: relative paths of the scanned files, only filled when collect_files is on
    file_list: list[str] = field(default_factory=list)

    @property
    def max_severity(self) -> Severity:
        if not self.findings:
            return Severity.INFO
        return max(self.findings, key=lambda f: severity_rank(f.severity)).severity

    @property
    def infected_files(self) -> list[Finding]:
        return [f for f in self.findings if severity_rank(f.severity) >= severity_rank(Severity.HIGH)]

    @property
    def limited_coverage(self) -> bool:
        """True when no real signature engine took part in the scan."""
        return not any(
            status.available and status.engine_id in ("clamav", "yara", "intel")
            for status in self.engines
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "verdict": str(self.verdict),
            "maxSeverity": str(self.max_severity),
            "findings": [f.to_dict() for f in self.findings],
            "findingCount": len(self.findings),
            "infectedCount": len(self.infected_files),
            "stats": self.stats.to_dict(),
            "engines": [status.to_dict() for status in self.engines],
            "warnings": list(self.warnings),
            "limitedCoverage": self.limited_coverage,
            "unreadableCount": len(self.unreadable),
            "skippedLarge": [
                {"path": path, "size": size, "sizeText": human_bytes(size)}
                for path, size in self.skipped_large[:50]
            ],
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "cancelled": self.cancelled,
            "fileList": list(self.file_list),
        }


def compute_verdict(
    findings: Sequence[Finding],
    *,
    blocking_severity: str = "high",
    cancelled: bool = False,
    files_scanned: int = 0,
    errors: int = 0,
) -> Verdict:
    """Turn findings into the single word that goes into the email subject."""
    if cancelled:
        return Verdict.CANCELLED
    if errors and files_scanned == 0:
        return Verdict.ERROR
    if not findings:
        return Verdict.CLEAN
    blocking = severity_rank(blocking_severity)
    highest = max(severity_rank(finding.severity) for finding in findings)
    if highest >= max(blocking, severity_rank(Severity.HIGH)):
        return Verdict.INFECTED
    if highest >= severity_rank(Severity.MEDIUM):
        return Verdict.SUSPICIOUS
    return Verdict.CLEAN


class Scanner:
    """Runs the configured engines over one root directory."""

    def __init__(
        self,
        config: ScanConfig,
        *,
        hash_db: dict[str, str] | None = None,
        collect_files: bool = False,
        ui_interval_ms: int = 100,
    ) -> None:
        self.config = config
        self._hash_db = hash_db if hash_db is not None else load_hash_db()
        #: keep the list of scanned files so the report can show what was covered
        self.collect_files = collect_files
        #: minimum gap between two progress events (general.ui_refresh_ms)
        self.ui_interval_ms = max(0, ui_interval_ms)

    # -- engines ----------------------------------------------------------
    def build_engines(self) -> list[Any]:
        engines: list[Any] = []
        for engine_id in self.config.engines:
            if engine_id == "clamav":
                if not self.config.clamav.enabled:
                    log.info("ClamAV is switched off in the configuration")
                    continue
                engines.append(ClamAVEngine(self.config.clamav))
            elif engine_id == "heuristic":
                engines.append(HeuristicEngine(self._hash_db))
            elif engine_id == "yara":
                if not self.config.yara.enabled:
                    log.info("YARA is switched off in the configuration")
                    continue
                engines.append(YaraEngine(self.config.yara))
            elif engine_id == "intel":
                if not self.config.intel.enabled:
                    log.info("hash intelligence is switched off in the configuration")
                    continue
                engines.append(
                    IntelEngine(
                        self.config.intel,
                        follow_symlinks=self.config.follow_symlinks,
                        include_hidden=self.config.include_hidden,
                        exclude_globs=self.config.exclude_globs,
                        max_file_size_mb=self.config.max_file_size_mb,
                    )
                )
            else:
                log.warning("ignoring unknown scan engine %r", engine_id)
        return engines

    def statuses(self) -> list[EngineStatus]:
        return [engine.status() for engine in self.build_engines()]

    # -- the scan ---------------------------------------------------------
    def run(
        self,
        root: str,
        *,
        sink: EventSink | None = None,
        cancel: threading.Event | None = None,
        device: dict[str, Any] | None = None,
    ) -> ScanResult:
        sink = sink or NullSink()
        cancel = cancel or threading.Event()
        root = os.path.abspath(root)
        result = ScanResult(root=root, started_at=iso_now())

        engines = self.build_engines()
        result.engines = [engine.status() for engine in engines]
        usable = [
            engine
            for engine, status in zip(engines, result.engines, strict=True)
            if status.available
        ]
        tree_engines = [engine for engine in usable if hasattr(engine, "scan_tree")]
        batch_engines = [engine for engine in usable if not hasattr(engine, "scan_tree")]
        for status in result.engines:
            if not status.available:
                result.warnings.append(f"{status.label} unavailable: {status.detail}")
            for warning in status.warnings:
                result.warnings.append(f"{status.label}: {warning}")

        if not usable:
            message = "no usable scan engine: the drive cannot be scanned"
            sink.on_log("error", message)
            result.warnings.append(message)
            result.verdict = Verdict.ERROR
            result.stats.duration = 0.0
            result.finished_at = iso_now()
            sink.on_stage(Stage.SCAN, StageState.FAILED, message)
            return result

        sink.on_stage(
            Stage.SCAN,
            StageState.RUNNING,
            "engines: " + ", ".join(f"{s.label} {s.version}".strip() for s in result.engines if s.available),
        )

        meter = ProgressMeter(min_interval=self.ui_interval_ms / 1000.0)
        started = time.monotonic()
        max_bytes = self.config.max_file_size_mb * 1024 * 1024

        # ---- pass 1: how much work is there? ---------------------------
        # Two pairs of counters: everything the medium holds, and the subset
        # that will actually be handed to the engines (files above the size
        # limit are reported as a coverage gap, not as scanned).
        total_files = 0
        total_bytes = 0
        scannable_files = 0
        scannable_bytes = 0
        enumeration_started = time.monotonic()
        for entry in self._walk(root, result):
            total_files += 1
            total_bytes += entry.size
            if not (max_bytes and entry.size > max_bytes and not entry.is_special):
                scannable_files += 1
                scannable_bytes += entry.size
            if cancel.is_set():
                break
            if total_files % 2000 == 0:
                snapshot = meter.snapshot(
                    Stage.SCAN,
                    processed=0,
                    total=0,
                    bytes_processed=0,
                    bytes_total=0,
                    current=entry.path,
                    message=f"indexing: {total_files} files, {human_bytes(total_bytes)}",
                    force=True,
                )
                if snapshot is not None:
                    sink.on_progress(snapshot)
        log.info(
            "indexed %d files (%s) in %.1fs",
            total_files,
            human_bytes(total_bytes),
            time.monotonic() - enumeration_started,
        )

        result.stats.files_total = total_files
        result.stats.bytes_total = total_bytes
        if total_files == 0:
            result.warnings.append("the medium is empty or unreadable")
            sink.on_log("warning", f"nothing to scan in {root}")

        # ---- pass 2: scan ---------------------------------------------
        batch_size = max(32, self.config.clamav.batch_size)
        processed_files = 0
        processed_bytes = 0
        seen: set[tuple[str, str]] = set()

        def advance(size: int, path: str) -> None:
            nonlocal processed_files, processed_bytes
            processed_files += 1
            processed_bytes += size
            if self.collect_files and len(result.file_list) < FILE_LIST_LIMIT:
                try:
                    result.file_list.append(os.path.relpath(path, root))
                except ValueError:  # pragma: no cover - different drives
                    result.file_list.append(path)
            snapshot = meter.snapshot(
                Stage.SCAN,
                processed=processed_files,
                total=scannable_files,
                bytes_processed=processed_bytes,
                bytes_total=scannable_bytes,
                current=path,
                message=(
                    f"{processed_files} / {scannable_files} files · "
                    f"{human_bytes(processed_bytes)} / {human_bytes(scannable_bytes)}"
                ),
            )
            if snapshot is not None:
                sink.on_progress(snapshot)

        ctx = ScanContext(root=root, config=self.config, cancel=cancel, sink=sink)

        def record(finding: Finding) -> None:
            """Admit one finding from any engine: de-duplicate, announce, count."""
            key = (finding.path, finding.threat)
            if key in seen:
                return
            seen.add(key)
            self._finalise_finding(finding)
            result.findings.append(finding)
            sink.on_finding(finding)
            sink.on_log(
                "warning"
                if severity_rank(finding.severity) >= severity_rank(Severity.MEDIUM)
                else "info",
                f"{finding.threat}: {finding.path}",
            )
            if self.config.max_findings and len(result.findings) >= self.config.max_findings:
                result.warnings.append(
                    f"stopped after {self.config.max_findings} findings (scan.max_findings)"
                )
                cancel.set()

        # The tree engines (YARA, hash intelligence) scan the whole root in one
        # pass and may talk to the network, so they run beside the batch engines
        # instead of inside the batch loop.  They only hand findings over
        # through a queue that this thread drains, which keeps the models and
        # the counters on one thread.
        arrivals: queue.Queue[tuple[str, Any, Any]] = queue.Queue()
        workers: list[threading.Thread] = []

        def run_tree_engine(engine: Any) -> None:
            try:
                for finding in engine.scan_tree(root, ctx):
                    arrivals.put(("finding", engine, finding))
                    if cancel.is_set():
                        break
            except Exception as exc:  # pragma: no cover - an engine crash must not lose the run
                log.exception("%s failed", engine.label)
                arrivals.put(("error", engine, exc))
            finally:
                arrivals.put(("done", engine, None))

        for engine in tree_engines:
            worker = threading.Thread(
                target=run_tree_engine,
                args=(engine,),
                name=f"dcs-{engine.engine_id}",
                daemon=True,
            )
            worker.start()
            workers.append(worker)

        def drain() -> None:
            while True:
                try:
                    kind, engine, payload = arrivals.get_nowait()
                except queue.Empty:
                    return
                if kind == "finding":
                    record(payload)
                elif kind == "error":
                    message = f"{engine.label} failed: {payload}"
                    result.warnings.append(message)
                    sink.on_log("error", message)

        drain()

        for batch, _batch_bytes in self._batches(root, batch_size, max_bytes, result):
            if cancel.is_set():
                break
            for index, engine in enumerate(batch_engines):
                # only the last engine reports fine grained progress, otherwise
                # two engines would count the same file twice
                ctx.tick = advance if index == len(batch_engines) - 1 else None
                try:
                    for finding in engine.scan_batch(batch, ctx):
                        record(finding)
                        if cancel.is_set():
                            break
                except Exception as exc:  # pragma: no cover - engine crash must not lose the run
                    message = f"{engine.label} failed on a batch: {exc}"
                    log.exception(message)
                    result.warnings.append(message)
                    sink.on_log("error", message)
                if cancel.is_set():
                    break
            drain()
            if cancel.is_set():
                break

            snapshot = meter.snapshot(
                Stage.SCAN,
                processed=processed_files,
                total=scannable_files,
                bytes_processed=processed_bytes,
                bytes_total=scannable_bytes,
                current=batch[-1].path if batch else "",
                message=(
                    f"{processed_files} / {scannable_files} files · "
                    f"{human_bytes(processed_bytes)} / {human_bytes(scannable_bytes)}"
                ),
                force=True,
            )
            if snapshot is not None:
                sink.on_progress(snapshot)

        # Wait for the tree engines, then take whatever they left in the queue.
        for worker in workers:
            worker.join(timeout=TREE_ENGINE_GRACE_S)
            if worker.is_alive():  # pragma: no cover - a hung engine or provider
                message = f"{worker.name} did not finish within {TREE_ENGINE_GRACE_S}s"
                result.warnings.append(message)
                sink.on_log("warning", message)
                cancel.set()
        drain()
        for engine in tree_engines:
            for note in getattr(engine, "notes", []) or []:
                result.warnings.append(f"{engine.label}: {note}")

        # Engines do not always tick once per file (ClamAV reports per output
        # line, and a batch can fail), so a completed scan is reconciled against
        # the scannable totals instead of leaving the bar at 97%.
        if not cancel.is_set():
            processed_files = max(processed_files, scannable_files)
            processed_bytes = max(processed_bytes, scannable_bytes)
        processed_files = min(processed_files, scannable_files)
        processed_bytes = min(processed_bytes, scannable_bytes)

        result.stats.files_scanned = processed_files
        result.stats.bytes_scanned = processed_bytes
        result.stats.files_skipped = len(result.skipped_large)
        result.stats.errors = sum(
            len(getattr(engine, "errors", [])) for engine in usable
        ) + len(result.unreadable)
        result.stats.engines = [f"{s.label} {s.version}".strip() for s in result.engines if s.available]
        result.stats.duration = time.monotonic() - started
        result.cancelled = cancel.is_set()
        result.unreadable = result.unreadable[:200]
        result.verdict = compute_verdict(
            result.findings,
            blocking_severity=self.config.blocking_severity,
            cancelled=result.cancelled,
            files_scanned=result.stats.files_scanned,
            errors=result.stats.errors,
        )
        result.finished_at = iso_now()

        final = StageProgress(
            stage=Stage.SCAN,
            state=StageState.CANCELLED if result.cancelled else StageState.DONE,
            processed=result.stats.files_scanned,
            total=total_files,
            bytes_processed=result.stats.bytes_scanned,
            bytes_total=total_bytes,
            message=self._verdict_message(result),
            elapsed=result.stats.duration,
            percent=1.0 if not result.cancelled else 0.0,
        )
        sink.on_progress(final)
        sink.on_stage(
            Stage.SCAN,
            StageState.CANCELLED if result.cancelled else StageState.DONE,
            self._verdict_message(result),
        )
        sink.on_log(
            "warning" if result.verdict is not Verdict.CLEAN else "info",
            f"scan finished: {result.verdict.value} "
            f"({len(result.findings)} findings, {result.stats.files_scanned} files, "
            f"{human_bytes(result.stats.bytes_scanned)})",
        )
        return result

    # -- helpers ----------------------------------------------------------
    def _walk(self, root: str, result: ScanResult) -> Iterator[FileEntry]:
        def on_error(path: str, exc: OSError) -> None:
            result.unreadable.append(f"{path}: {exc.strerror or exc}")

        return walk_files(
            root,
            follow_symlinks=self.config.follow_symlinks,
            include_hidden=self.config.include_hidden,
            exclude_globs=self.config.exclude_globs,
            max_files=self.config.max_files,
            on_error=on_error,
        )

    def _batches(
        self,
        root: str,
        batch_size: int,
        max_bytes: int,
        result: ScanResult,
    ) -> Iterator[tuple[list[FileEntry], int]]:
        batch: list[FileEntry] = []
        batch_bytes = 0
        for entry in self._walk(root, result):
            if entry.is_special:
                batch.append(entry)
            elif max_bytes and entry.size > max_bytes:
                result.skipped_large.append((entry.path, entry.size))
                continue
            else:
                batch.append(entry)
            batch_bytes += entry.size
            if len(batch) >= batch_size:
                yield batch, batch_bytes
                batch, batch_bytes = [], 0
        if batch:
            yield batch, batch_bytes

    def _finalise_finding(self, finding: Finding) -> None:
        if not self.config.hash_findings or finding.sha256 or not finding.path:
            return
        from dcs.core.util import sha256_file

        try:
            finding.sha256 = sha256_file(finding.path, limit=64 * 1024 * 1024)
        except OSError:
            finding.sha256 = ""

    @staticmethod
    def _verdict_message(result: ScanResult) -> str:
        infected = len(result.infected_files)
        if result.verdict is Verdict.INFECTED:
            return f"{infected} threatening file(s) found"
        if result.verdict is Verdict.SUSPICIOUS:
            return f"{len(result.findings)} suspicious item(s) found"
        if result.verdict is Verdict.CANCELLED:
            return "scan cancelled"
        if result.verdict is Verdict.ERROR:
            return "scan could not complete"
        return "no threats found"
