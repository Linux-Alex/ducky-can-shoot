"""Logging setup with an in-memory ring buffer the GUI can display."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

LOGGER_NAME = "dcs"


@dataclass(frozen=True, slots=True)
class LogRecordView:
    """Plain snapshot of a log record, safe to hand to the GUI thread."""

    time: str
    level: str
    logger: str
    message: str


class RingBufferHandler(logging.Handler):
    """Keep the last N records in memory for the live log pane."""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self.capacity = capacity
        self._records: deque[LogRecordView] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.listeners: list = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            view = LogRecordView(
                time=self.formatter.formatTime(record, "%H:%M:%S") if self.formatter else "",
                level=record.levelname,
                logger=record.name,
                message=record.getMessage(),
            )
        except Exception:  # pragma: no cover - logging must never explode
            return
        with self._lock:
            self._records.append(view)
            listeners = list(self.listeners)
        for listener in listeners:
            # a bad listener is not fatal
            with contextlib.suppress(Exception):
                listener(view)

    def snapshot(self) -> list[LogRecordView]:
        with self._lock:
            return list(self._records)

    def add_listener(self, callback) -> None:
        with self._lock:
            self.listeners.append(callback)

    def remove_listener(self, callback) -> None:
        with self._lock:
            if callback in self.listeners:
                self.listeners.remove(callback)


_ring: RingBufferHandler | None = None
_configured = False


def configure_logging(
    level: int | str = logging.INFO,
    *,
    logfile: str | None = None,
    capacity: int = 2000,
    quiet: bool = False,
) -> RingBufferHandler:
    """Configure the 'dcs' logger once and return the ring buffer handler."""
    global _ring, _configured
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if _configured:
        logger.setLevel(level)
        assert _ring is not None
        return _ring

    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    ring = RingBufferHandler(capacity)
    ring.setFormatter(formatter)
    logger.addHandler(ring)

    if not quiet:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    if logfile:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(logfile)), exist_ok=True)
            file_handler = logging.FileHandler(logfile, encoding="utf-8")
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
            )
            logger.addHandler(file_handler)
        except OSError:
            logger.warning("could not open log file %s", logfile)

    _ring = ring
    _configured = True
    return ring


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def log_ring() -> RingBufferHandler:
    global _ring
    if _ring is None:
        _ring = configure_logging()
    return _ring


def iter_records() -> Iterable[LogRecordView]:
    return log_ring().snapshot()
