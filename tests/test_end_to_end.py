"""One whole job, start to finish: the release gate for 0.3.

Everything else in the suite tests one stage or one module.  This file runs the
pipeline exactly as the CLI does - inspect, scan (through the fake ClamAV front
ends), archive, upload (to a stand-in host), report, email (written to disk) and
the history index - over a synthetic medium, and then checks the artefacts a
recipient would touch.

It is deliberately one test: its value is that the stages still compose, so a
failure here means "the product is broken", not "a unit moved".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dcs.core.config import AppConfig, load_config
from dcs.core.history import open_history
from dcs.core.pipeline import JobRequest, Pipeline
from dcs.core.upload.base import UploadResult

PASSPHRASE = "end to end passphrase"
FAKE_LINK = "https://example.test/f/e2e"


class RecordingProvider:
    """A cloud host that records what it was sent and returns a stable link."""

    provider_id = "recording"
    label = "Recording Host"
    requires_key = False
    max_bytes = 0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def availability(self) -> tuple[bool, str]:
        return True, ""

    def upload(
        self,
        path: str,
        *,
        progress: Any = None,
        cancel: Any = None,
        part_label: str = "",
    ) -> UploadResult:
        size = os.path.getsize(path)
        self.calls.append({"path": path, "size": size, "label": part_label})
        return UploadResult(
            ok=True, link=FAKE_LINK, provider=self.provider_id, path=path, size=size
        )

    def folder_link(self) -> str:
        return ""


def _decoded_message_text(path: Path) -> str:
    """Every text part of a stored MIME message, decoded.

    The dry-run file is the raw message; its HTML alternative is base64, so a
    plain read of the file would not contain the link the recipient sees.
    """
    from email import message_from_bytes

    message = message_from_bytes(path.read_bytes())
    chunks: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True) or b""
        chunks.append(payload.decode("utf-8", "replace"))
    return "\n".join(chunks)


def has_7z() -> bool:
    return shutil.which("7z") is not None or shutil.which("7za") is not None


@pytest.mark.skipif(not has_7z(), reason="encrypted archives need the 7z binary")
def test_a_whole_encrypted_job_produces_every_artefact(
    tmp_config: AppConfig,
    fake_clamav: Path,
    fake_provider: RecordingProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ---------------------------------------------------------------- medium
    medium = tmp_path / "usb"
    (medium / "docs").mkdir(parents=True)
    (medium / "readme.txt").write_text("Holiday photos and a few documents.\n")
    (medium / "docs" / "agenda.md").write_text("# Agenda\n\nNothing malicious here.\n")
    (medium / "docs" / "notes.pdf").write_bytes(
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
    )
    (medium / "song.mp3").write_bytes(b"ID3\x03\x00\x00\x00" + os.urandom(1024))

    # ------------------------------------------------------------- options
    config = tmp_config
    config.upload.enabled = True
    config.upload.upload_when = "clean"
    config.archive.encrypt = True
    config.archive.compression_level = 1
    config.scan.engines = ["clamav", "heuristic"]
    config.report.include_file_list = True

    # the fake ClamAV reports every file OK: this medium is clean

    request = JobRequest(
        source_path=str(medium),
        label="e2e-stick",
        email_dry_run=True,
        recipients=["ops@example.test"],
        passphrase=PASSPHRASE,
    )

    result = Pipeline(config).run(request)

    # ------------------------------------------------------------- verdict
    assert result.verdict.value == "clean", [t.message for t in result.timings]
    assert result.files_scanned == 4
    assert result.threat_count == 0
    assert not result.cancelled

    # ------------------------------------------------------------- archive
    assert result.archive is not None and result.archive.ok, result.upload_skipped_reason
    assert result.archive.encrypted is True
    assert result.archive.encryption == "AES-256"
    assert result.archive.headers_encrypted is True
    archive = Path(result.archive.primary_path)
    assert archive.suffix == ".7z" and archive.exists()
    # without the passphrase not even the file names can be listed
    listing = subprocess.run(
        ["7z", "l", str(archive)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    assert listing.returncode != 0

    # -------------------------------------------------------------- upload
    assert len(fake_provider.calls) == 1, fake_provider.calls
    uploaded = Path(fake_provider.calls[0]["path"])
    assert uploaded == archive
    assert result.share_link == FAKE_LINK
    assert result.upload_links == [FAKE_LINK]

    # ------------------------------------------------------------- reports
    reports = result.report_paths
    assert set(reports) == {"html", "text", "csv", "json"}
    for kind, path in reports.items():
        assert Path(path).is_file(), f"the {kind} report is missing"
        text = Path(path).read_text(encoding="utf-8")
        assert PASSPHRASE not in text, f"the passphrase leaked into the {kind} report"
    html = Path(reports["html"]).read_text(encoding="utf-8")
    assert "AES-256" in html
    assert "Encrypted" in html
    summary = json.loads(Path(reports["json"]).read_text(encoding="utf-8"))
    assert summary["archive"]["encrypted"] is True
    assert summary["shareLink"] == FAKE_LINK
    assert summary["verdict"] == "clean"

    # --------------------------------------------------------------- email
    assert result.email is not None and result.email.ok
    assert result.email.dry_run is True
    message = Path(result.email.message_path)
    assert message.is_file()
    body = _decoded_message_text(message)
    assert FAKE_LINK in body
    assert PASSPHRASE not in body
    assert "encrypted" in body.lower()

    # ------------------------------------------------------------- history
    entry = open_history(config).get(result.job_id)
    assert entry is not None
    assert entry.label == "e2e-stick"
    assert entry.verdict == "clean"
    assert entry.archive_encrypted is True
    assert entry.share_link == FAKE_LINK
    assert entry.files_scanned == 4
    assert entry.report_html == reports["html"]
    assert Path(entry.report_html).is_file()


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> RecordingProvider:
    provider = RecordingProvider()
    monkeypatch.setattr("dcs.core.pipeline.build_provider", lambda *args, **kwargs: provider)
    return provider


def test_the_cli_reports_the_same_job_as_the_pipeline(
    tmp_path: Path,
    tmp_config: AppConfig,
    fake_clamav: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The headless entry point produces the same verdict and exit code."""
    from dcs.cli import main

    medium = tmp_path / "usb-cli"
    medium.mkdir()
    (medium / "readme.txt").write_text("Nothing to see here.\n")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[general]",
                f'output_dir = "{tmp_path / "out"}"',
                "[report]",
                f'directory = "{tmp_path / "reports"}"',
                "[upload]",
                "enabled = false",
                "[email]",
                "enabled = false",
                "[scan]",
                'engines = ["clamav", "heuristic"]',
                "[history]",
                f'database = "{tmp_path / "history.db"}"',
            ]
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "--config",
            str(config_path),
            "run",
            "--path",
            str(medium),
            "--label",
            "cli-stick",
            "--no-upload",
            "--no-email",
            "--json",
            "--quiet",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0  # clean
    assert captured.out.strip().startswith("{"), captured.out[:200]
    payload = json.loads(captured.out)
    assert payload["verdict"] == "clean"
    assert payload["sourceLabel"] == "cli-stick"
    # the same job is in the index the CLI was told to use
    entry = open_history(load_config(config_path)).get(payload["jobId"])
    assert entry is not None
    assert entry.verdict == "clean"
    assert entry.label == "cli-stick"
