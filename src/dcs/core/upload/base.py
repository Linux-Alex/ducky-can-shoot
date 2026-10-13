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
import hashlib
import os
import time
import uuid
from collections.abc import Callable, Iterator
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
    """File wrapper that reports upload progress while the body is streamed.

    Implements the file protocol correctly, including the part that matters
    most: read() with no argument returns everything up to EOF.  requests
    calls exactly that when it is handed a file object (it buffers the file
    into memory), and returning a single chunk there silently truncated every
    upload larger than the chunk size.
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
        # the handle deliberately outlives the constructor: the multipart body
        # reads from this object while streaming, and close()/__exit__ releases
        # it, which is why the context manager form does not apply here.
        self._handle: BinaryIO = open(path, "rb")  # noqa: SIM115 - see the note above
        self._sent = 0

    # -- file protocol ----------------------------------------------------
    def __len__(self) -> int:
        return self.size

    def __iter__(self) -> Iterator[bytes]:
        """Iterate the file in chunks, reporting progress as it goes."""
        while True:
            chunk = self.read(self.chunk_size)
            if not chunk:
                return
            yield chunk

    def read(self, amount: int | None = None) -> bytes:
        if self.cancel is not None and self.cancel.is_set():
            raise UploadCancelled("upload cancelled by the user")
        if amount is None or amount < 0:
            # the file protocol says: no argument means read to EOF
            data = self._handle.read()
        elif amount == 0:
            return b""
        else:
            data = self._handle.read(amount)
        self._sent += len(data)
        if data and self.progress is not None:
            # progress must never break an upload
            with contextlib.suppress(Exception):
                self.progress(self._sent, self.size)
        return data

    def tell(self) -> int:
        return self._sent

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._handle.close()

    def __enter__(self) -> ProgressReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class MultipartStream:
    """A multipart/form-data body that streams a file instead of buffering it.

    requests cannot stream a file part: PreparedRequest._encode_files calls
    fp.read() once and keeps the whole thing in memory.  For a USB image that
    is both a memory hazard and the reason progress used to jump straight from
    0 to 100 percent.  Building the body here gives the caller a generator to
    hand to requests: urllib3 iterates it, so the file is read one chunk at a
    time, the progress callback reports real byte positions, and the
    Content-Length is exact because every part of the framing is known before
    the first byte is sent.
    """

    def __init__(
        self,
        path: str,
        *,
        file_field: str = "file",
        filename: str | None = None,
        fields: dict[str, str] | None = None,
        boundary: str | None = None,
        progress: ProgressFn | None = None,
        cancel: Any | None = None,
        chunk_size: int = 1 << 20,
    ) -> None:
        self.path = path
        self.size = os.path.getsize(path)
        self.file_field = file_field
        self.filename = filename or os.path.basename(path)
        self.fields = dict(fields or {})
        self.boundary = boundary or f"----DCSBoundary{uuid.uuid4().hex}"
        self.progress = progress
        self.cancel = cancel
        self.chunk_size = chunk_size

    # -- framing ----------------------------------------------------------
    @property
    def prologue(self) -> bytes:
        """The part before the file bytes: the fields and the file header."""
        parts: list[str] = []
        for name, value in self.fields.items():
            parts.append(f"--{self.boundary}{MID}")
            parts.append(f'Content-Disposition: form-data; name="{_quote(name)}"{MID}{MID}')
            parts.append(f"{value}{MID}")
        parts.append(f"--{self.boundary}{MID}")
        parts.append(
            f'Content-Disposition: form-data; name="{_quote(self.file_field)}"; '
            f'filename="{_quote(self.filename)}"{MID}'
        )
        parts.append(f"Content-Type: application/octet-stream{MID}{MID}")
        return "".join(parts).replace(MID, CRLF).encode("utf-8")

    @property
    def epilogue(self) -> bytes:
        return f"{CRLF}--{self.boundary}--{CRLF}".encode()

    @property
    def content_length(self) -> int:
        return len(self.prologue) + self.size + len(self.epilogue)

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    @property
    def headers(self) -> dict[str, str]:
        """Exact Content-Length plus the matching Content-Type."""
        return {"Content-Type": self.content_type, "Content-Length": str(self.content_length)}

    def __len__(self) -> int:
        """The exact body length, which requests has to be able to see.

        Handing requests a bare generator makes it add
        Transfer-Encoding: chunked even when a Content-Length header is already
        present; a server that receives both answers 400 Bad Request.  Exposing
        __len__ lets requests compute the length itself and frame the body the
        classic way.
        """
        return self.content_length

    def __iter__(self) -> Iterator[bytes]:
        yield self.prologue
        with ProgressReader(
            self.path,
            progress=self.progress,
            cancel=self.cancel,
            chunk_size=self.chunk_size,
        ) as reader:
            yield from reader
        yield self.epilogue


#: carriage return plus line feed, spelled out so no reader has to guess
CRLF = chr(13) + chr(10)
#: placeholder swapped for CRLF while the multipart prologue is built
MID = chr(0)


def file_md5(path: str, chunk_size: int = 1 << 20) -> str:
    """MD5 of a file, which is the checksum the free hosts report back."""
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _quote(value: str) -> str:
    """Quote a multipart field name or file name (no header injection)."""
    cleaned = value.replace(chr(13), " ").replace(chr(10), " ")
    return cleaned.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))

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
