"""Runs the pipeline on a worker thread and re-emits its events as Qt signals.

Qt signals emitted from a non-GUI thread are delivered by the event loop of the
thread that owns the receiver, so the models only ever mutate on the GUI
thread.  This class is therefore the entire threading story of the UI.
"""

from __future__ import annotations

import threading
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from dcs.core.config import AppConfig
from dcs.core.events import Finding, Stage, StageProgress, StageState
from dcs.core.log import get_logger
from dcs.core.pipeline import JobRequest, Pipeline
from dcs.core.result import JobResult

log = get_logger("gui.worker")


class SignalSink(QObject):
    """EventSink implementation that emits one Qt signal per event kind.

    It deliberately does not inherit from EventSink: the protocol is structural
    and inheriting it would drag ABCMeta into a QObject metaclass conflict.
    """

    stageChanged = pyqtSignal(str, str, str)
    progressChanged = pyqtSignal(object)
    findingFound = pyqtSignal(object)
    logMessage = pyqtSignal(str, str)
    summaryReady = pyqtSignal(object)

    def on_stage(self, stage: Stage, state: StageState, message: str = "") -> None:
        self.stageChanged.emit(str(stage), str(state), message or "")

    def on_progress(self, progress: StageProgress) -> None:
        self.progressChanged.emit(progress.to_dict())

    def on_finding(self, finding: Finding) -> None:
        self.findingFound.emit(finding.to_dict())

    def on_log(self, level: str, message: str) -> None:
        self.logMessage.emit(level, message)

    def on_summary(self, summary: dict[str, Any]) -> None:
        self.summaryReady.emit(summary)


class PipelineWorker(QObject):
    """Owns one job; lives inside a QThread created by the backend."""

    stageChanged = pyqtSignal(str, str, str)
    progressChanged = pyqtSignal(object)
    findingFound = pyqtSignal(object)
    logMessage = pyqtSignal(str, str)
    summaryReady = pyqtSignal(object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, config: AppConfig, request: JobRequest) -> None:
        super().__init__()
        self.config = config
        self.request = request
        self.cancel_event = threading.Event()
        self.sink = SignalSink()
        self.sink.stageChanged.connect(self.stageChanged)
        self.sink.progressChanged.connect(self.progressChanged)
        self.sink.findingFound.connect(self.findingFound)
        self.sink.logMessage.connect(self.logMessage)
        self.sink.summaryReady.connect(self.summaryReady)

    @pyqtSlot()
    def run(self) -> None:
        """Entry point executed on the worker thread."""
        try:
            pipeline = Pipeline(self.config)
            result: JobResult = pipeline.run(self.request, sink=self.sink, cancel=self.cancel_event)
            self.finished.emit(result.to_dict())
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("worker crashed")
            self.failed.emit(str(exc))

    @pyqtSlot()
    def request_cancel(self) -> None:
        self.cancel_event.set()
        self.logMessage.emit("warning", "cancellation requested; finishing the current file first")
