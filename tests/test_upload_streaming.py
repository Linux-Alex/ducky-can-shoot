"""Regression tests for the truncated upload bug.

The bug: ProgressReader.read() with no argument returned a single chunk instead
of the rest of the file.  requests calls exactly that (PreparedRequest._encode_files
does fp.read() and buffers the whole part), so every upload larger than the chunk
size silently sent only its first megabyte, and the link that came back pointed
at a truncated archive.

These tests exercise the real code paths: the file protocol, the multipart body
framing, and a complete upload against a local HTTP server that records what it
received - no network, no mocking of the transport.
"""

from __future__ import annotations

import hashlib
import http.server
import os
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dcs.core.upload.base import MultipartStream, ProgressReader, UploadCancelled, file_md5

MiB = 1024 * 1024


@pytest.fixture
def big_file(tmp_path: Path) -> Path:
    """A file large enough that a chunk-sized read would be visible."""
    path = tmp_path / "usb-image.bin"
    path.write_bytes(os.urandom(3 * MiB + 1234))
    return path


# ---------------------------------------------------------------------------
# the file protocol
# ---------------------------------------------------------------------------


def test_read_without_an_argument_returns_the_whole_file(big_file: Path) -> None:
    """This is the exact call requests makes, and the one that broke."""
    reader = ProgressReader(str(big_file))
    data = reader.read()
    assert len(data) == big_file.stat().st_size
    assert data == big_file.read_bytes()


def test_read_with_a_size_returns_that_many_bytes(big_file: Path) -> None:
    reader = ProgressReader(str(big_file))
    first = reader.read(1024)
    second = reader.read(2048)
    assert len(first) == 1024
    assert len(second) == 2048
    assert first + second == big_file.read_bytes()[:3072]


def test_read_zero_returns_nothing_and_does_not_advance(big_file: Path) -> None:
    reader = ProgressReader(str(big_file))
    assert reader.read(0) == b""
    assert reader.read(16) == big_file.read_bytes()[:16]


def test_negative_size_means_read_to_the_end(big_file: Path) -> None:
    reader = ProgressReader(str(big_file))
    assert len(reader.read(-1)) == big_file.stat().st_size


def test_iterating_reads_every_chunk_and_reports_progress(big_file: Path) -> None:
    reports: list[tuple[int, int]] = []
    reader = ProgressReader(str(big_file), progress=lambda done, total: reports.append((done, total)), chunk_size=MiB)
    total = sum(len(chunk) for chunk in reader)
    assert total == big_file.stat().st_size
    assert len(reports) == 4  # three full chunks and the remainder
    assert reports[-1][0] == big_file.stat().st_size
    assert [done for done, _ in reports] == sorted(done for done, _ in reports)


def test_len_is_the_file_size(big_file: Path) -> None:
    assert len(ProgressReader(str(big_file))) == big_file.stat().st_size


def test_cancellation_is_raised_from_read(big_file: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    reader = ProgressReader(str(big_file), cancel=cancel)
    with pytest.raises(UploadCancelled):
        reader.read(16)


# ---------------------------------------------------------------------------
# the multipart body
# ---------------------------------------------------------------------------


def test_multipart_body_contains_the_whole_file(big_file: Path) -> None:
    stream = MultipartStream(str(big_file), filename="usb-image.bin", fields={"folderId": "abc"})
    body = b"".join(stream)
    payload = big_file.read_bytes()
    assert payload in body
    assert len(body) == stream.content_length
    assert body.startswith(b"--" + stream.boundary.encode())
    assert body.endswith(b"--" + stream.boundary.encode() + b"--" + b"\r\n")


def test_multipart_content_length_is_exact(big_file: Path) -> None:
    stream = MultipartStream(str(big_file), fields={"folderId": "x", "name": "y"})
    assert len(stream) == stream.content_length
    assert stream.headers["Content-Length"] == str(stream.content_length)
    assert len(b"".join(stream)) == int(stream.headers["Content-Length"])


def test_multipart_reports_progress_reaching_the_file_size(big_file: Path) -> None:
    reports: list[int] = []
    stream = MultipartStream(str(big_file), progress=lambda done, total: reports.append(done), chunk_size=MiB)
    b"".join(stream)
    assert reports
    assert reports[-1] == big_file.stat().st_size


def test_multipart_field_and_file_names_are_escaped(tmp_path: Path) -> None:
    path = tmp_path / "plain.bin"
    path.write_bytes(b"data")
    stream = MultipartStream(str(path), filename='we"ird\r\nname.bin', fields={"a\"b": "c"})
    body = b"".join(stream)
    # a CRLF inside a quoted name would allow header injection
    assert b"\r\n\r\n" in body  # the legitimate separator between parts
    head = body.split(b"\r\n\r\n")[0]
    assert b"we\"ird name.bin" in body or b'we\\"ird' in body
    assert b"name=\"a" in body
    assert head.count(b"\r\n") <= 3


# ---------------------------------------------------------------------------
# a complete upload against a local server
# ---------------------------------------------------------------------------


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Accepts one multipart POST and remembers exactly what arrived."""

    #: every request the test server saw, so tests can inspect the bytes
    received: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).received.append(
            {
                "path": self.path,
                "body": body,
                "content_type": self.headers.get("Content-Type", ""),
                "content_length": length,
                "transfer_encoding": self.headers.get("Transfer-Encoding", ""),
            }
        )
        payload = b"https://example.test/abc123"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # keep the test output quiet
        return


@pytest.fixture
def local_host() -> Any:
    server = http.server.HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    _RecordingHandler.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_large_file_arrives_complete_at_the_server(local_host: Any, big_file: Path) -> None:
    """The end to end check: every byte of a >1 MiB file reaches the host.

    Before the fix this test failed with exactly one megabyte received.
    """
    from dcs.core.upload.simple import SIMPLE_HOSTS, SimpleHostProvider

    _server, base_url = local_host
    provider = SimpleHostProvider(SIMPLE_HOSTS["tempsh"], api_key="", base_url=base_url)
    reports: list[int] = []
    result = provider.upload(str(big_file), progress=lambda done, total: reports.append(done))

    assert result.ok is True, result.error
    assert len(_RecordingHandler.received) == 1
    request = _RecordingHandler.received[0]
    payload = big_file.read_bytes()
    # the whole file is in the body, exactly once, with no chunked framing
    assert payload in request["body"]
    assert request["body"].count(payload[:4096]) == 1
    assert request["content_length"] == len(request["body"])
    assert request["transfer_encoding"] == ""
    assert request["content_type"].startswith("multipart/form-data; boundary=")
    # progress covered the file, not just its first chunk
    assert reports and reports[-1] == big_file.stat().st_size
    # and the link came back intact
    assert result.link == "https://example.test/abc123"


def test_a_large_file_can_be_cancelled_midway(local_host: Any, big_file: Path) -> None:
    from dcs.core.upload.simple import SIMPLE_HOSTS, SimpleHostProvider

    _server, base_url = local_host
    cancel = threading.Event()
    provider = SimpleHostProvider(SIMPLE_HOSTS["tempsh"], api_key="", base_url=base_url, retries=0)

    def progress(done: int, total: int) -> None:
        if done >= MiB:
            cancel.set()

    result = provider.upload(str(big_file), progress=progress, cancel=cancel)
    assert result.ok is False
    # either the reader raised UploadCancelled or the request was cut short;
    # both are reported as a failure rather than as a successful upload
    assert result.error


def test_file_md5_matches_hashlib(big_file: Path) -> None:
    assert file_md5(str(big_file)) == hashlib.md5(big_file.read_bytes()).hexdigest()
