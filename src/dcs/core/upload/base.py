"""Provider interface for the cloud upload stage.

Every adapter shares the same contract: hand it a file, a progress callback and
a cancellation event, and it returns a public link or an explanation of why it
could not.  Adding a host is therefore a small, self contained module.

Progress is the tricky part: requests streams a multipart body from a file
object, so DCS wraps the file in :class:ProgressReader.  It exposes __len__ (so
requests can send a Content-Length and the server accepts the upload) and
reports every read chunk back to the caller.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Protocol, runtime_checkable

from dcs.core.log import get_logger

log = get_logger("upload")

#: progress callback: (bytes_done, total_bytes)
ProgressFn = Callable[[int, int], None]


class UploadCancelled(Exception):  # noqa: N818 - reads better as "raise UploadCancelled"
    """Raised inside an adapter when the user cancels the upload."""


class UploadError(Exception):
    """Raised when a provider reports a failure."""


@dataclass
class UploadResult:
    """Result of one upload attempt (one part)."""

    ok: bool = False
    link: str = ""
    provider: str = ""
    path: str = ""
    size: int = 0
    duration: float = 0.0
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "link": self.link,
            "provider": self.provider,
            "path": self.path,
            "size": self.size,
            "duration": self.duration,
            "detail": self.detail,
            "error": self.error,
            "warnings": list(self.warnings),
        }


@runtime_checkable
class UploadProvider(Protocol):
    """What every cloud adapter must implement."""

    provider_id: str
    label: str
    requires_key: bool
    max_bytes: int

    def availability(self) -> tuple[bool, str]:
        """(usable, explanation)."""

    def upload(
        self,
        path: str,
        *,
        progress: ProgressFn | None = None,
        cancel: Any | None = None,
        part_label: str = "",
    ) -> UploadResult:
        """Upload one file and return its public link."""


class ProgressReader:
    """File wrapper that reports upload progress to requests.

    requests calls super_len() on the object, which uses __len__, so the
    request carries a real Content-Length and servers that reject chunked
    uploads still work.  Every read() then feeds the caller's progress bar.
    """

    def __init__(
        self,
        path: str,
        *,
        progress: ProgressFn | None = None,
        cancel: Any | None = None,
        chunk_size: int = 1 << 20,
    ) -> None:
        self.path = path
        self.size = os.path.getsize(path)
        self.progress = progress
        self.cancel = cancel
        self.chunk_size = chunk_size
        # the handle deliberately outlives the constructor: requests reads from
        # this object while streaming the body, and close()/__exit__ releases it.
        self._handle: BinaryIO = open(path, "rb")  # noqa: SIM115 - see the note above
        self._sent = 0

    # -- file protocol ----------------------------------------------------
    def __len__(self) -> int:
        return self.size

    def read(self, amount: int | None = None) -> bytes:
        if self.cancel is not None and self.cancel.is_set():
            raise UploadCancelled("upload cancelled by the user")
        data = self._handle.read(amount if amount and amount > 0 else self.chunk_size)
        self._sent += len(data)
        if self.progress is not None:
            # progress must never break an upload
            with contextlib.suppress(Exception):
                self.progress(self._sent, self.size)
        return data

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._handle.close()

    def __enter__(self) -> ProgressReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class BaseProvider:
    """Shared plumbing: retries, timing, result construction."""

    provider_id = "base"
    label = "Base"
    requires_key = False
    max_bytes = 0

    def __init__(self, config: Any = None, *, retries: int = 2, timeout: int = 1800) -> None:
        self.config = config
        self.retries = max(0, retries)
        self.timeout = timeout

    # -- helpers ----------------------------------------------------------
    def availability(self) -> tuple[bool, str]:
        return True, ""

    def _result(self, path: str, started: float, **kwargs: Any) -> UploadResult:
        size = os.path.getsize(path) if os.path.exists(path) else 0
        return UploadResult(
            provider=self.provider_id,
            path=path,
            size=size,
            duration=time.monotonic() - started,
            **kwargs,
        )

    @staticmethod
    def _check_size(path: str, maximum: int) -> str:
        if maximum <= 0:
            return ""
        size = os.path.getsize(path)
        if size > maximum:
            from dcs.core.util import human_bytes

            return f"{os.path.basename(path)} is {human_bytes(size)}, over the {human_bytes(maximum)} limit of this host"
        return ""
