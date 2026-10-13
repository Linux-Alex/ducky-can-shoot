"""Engine interface shared by every scanner backend."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from dcs.core.config import ScanConfig
from dcs.core.events import EventSink, Finding, NullSink
from dcs.core.log import get_logger
from dcs.core.util import FileEntry


@dataclass(slots=True)
class EngineStatus:
    """Whether an engine can run, and why not."""

    engine_id: str
    label: str
    available: bool
    version: str = ""
    detail: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.engine_id,
            "label": self.label,
            "available": self.available,
            "version": self.version,
            "detail": self.detail,
            "warnings": list(self.warnings),
        }


#: callback used for fine grained progress: (file_size, path)
TickFn = Callable[[int, str], None]


@dataclass
class ScanContext:
    """Everything an engine needs while scanning one root."""

    root: str
    config: ScanConfig
    cancel: threading.Event = field(default_factory=threading.Event)
    sink: EventSink = field(default_factory=NullSink)
    #: called with (file_size, path) for every finished file, for the progress bar.
    #: The orchestrator only installs it on one engine per batch, so that two
    #: engines cannot count the same file twice.
    tick: TickFn | None = None

    def tick_file(self, size: int, path: str) -> None:
        """Report one processed file to the progress tracker (never raises)."""
        if self.tick is None:
            return
        # a progress bug must not stop a scan
        with contextlib.suppress(Exception):
            self.tick(size, path)

    def log(self, message: str, level: str = "info") -> None:
        # a broken sink must never stop a scan
        with contextlib.suppress(Exception):
            self.sink.on_log(level, message)
        get_logger("scan").log(
            {"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 20), message
        )

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()


@runtime_checkable
class ScanEngine(Protocol):
    """A pluggable scanner backend."""

    engine_id: str
    label: str

    def status(self) -> EngineStatus:
        """Report availability (binary present, daemon reachable, ...)."""

    def scan_batch(self, entries: Sequence[FileEntry], ctx: ScanContext) -> Iterable[Finding]:
        """Scan a batch of files and yield findings."""
