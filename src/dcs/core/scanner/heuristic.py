"""Built-in, dependency-free heuristic engine.

The engine never executes anything from the scanned medium: it only reads
bytes and reasons about them.  It exists for three reasons:

1. DCS must be useful on a machine without ClamAV (a live USB, a locked-down
   workstation, a colleague's laptop).
2. ClamAV does not care *why* a file is dangerous.  The heuristics explain
   the actual behaviour (an HID payload script, a download cradle, a
   double-extension executable), which is what the report needs.
3. A drive that carries a physical-payload toolkit (Rubber Ducky, BadUSB) is
   usually clean from the antivirus point of view -- and that is exactly the
   attack this tool is named after.

Every check is conservative: a finding is only produced when the evidence is
concrete enough to be quoted in the report.
"""

from __future__ import annotations

import math
import os
import re
import struct
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from dcs.core.events import Finding, Severity
from dcs.core.scanner import rules
from dcs.core.scanner.base import EngineStatus, ScanContext
from dcs.core.util import FileEntry

# ---------------------------------------------------------------------------
# tiny helpers
# ---------------------------------------------------------------------------

_MAGIC_PE = b"MZ"
_MAGIC_ELF = b"\x7fELF"
_MAGIC_PDF = b"%PDF"
_MAGIC_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MAGIC_ZIP = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_MAGIC_RAR = (b"Rar!\x1a\x07",)
_MAGIC_7Z = (b"7z\xbc\xaf\x27\x1c",)
_MAGIC_RTF = b"{\\rtf"
_MAGIC_LNK = b"\x4c\x00\x00\x00"
_MAGIC_CAB = b"MSCF"

_BASE64_BLOB = re.compile(rb"[A-Za-z0-9+/]{200,}={0,2}")
_URL = re.compile(rb"https?://[\w./?=&%:@#~+-]{6,200}", re.I)
_PRINTABLE = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}


def _entropy(data: bytes) -> float:
    """Shannon entropy in bits per byte (0 .. 8)."""
    if not data:
        return 0.0
    counts = [0] * 256
    for byte in data:
        counts[byte] += 1
    total = len(data)
    entropy = 0.0
    for count in counts:
        if count:
            probability = count / total
            entropy -= probability * math.log2(probability)
    return entropy


def _looks_like_text(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    printable = sum(1 for byte in sample if byte in _PRINTABLE or byte >= 0x80)
    return printable / len(sample) >= 0.90


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _read_head(path: str, limit: int) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


@dataclass(slots=True)
class DuckyVerdict:
    """Outcome of the HID payload script analysis."""

    commands: int = 0
    command_ratio: float = 0.0
    keywords: list[str] = field(default_factory=list)

    @property
    def is_payload(self) -> bool:
        return self.commands >= 5 and self.command_ratio >= 0.30


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------


class HeuristicEngine:
    """Signature free analysis of file names, headers and embedded scripts."""

    engine_id = "heuristic"
    label = "DCS built-in heuristics"

    def __init__(self, hash_db: dict[str, str] | None = None) -> None:
        self.hash_db = hash_db if hash_db is not None else load_hash_db()

    # -- status -----------------------------------------------------------
    def status(self) -> EngineStatus:
        return EngineStatus(
            engine_id=self.engine_id,
            label=self.label,
            available=True,
            version="1.0",
            detail=(
                "Built-in rules: HID payload scripts, download cradles, "
                "double extensions, macro and shortcut abuse, packed binaries, "
                "archive bombs and known hashes."
            ),
            warnings=[] if self.hash_db else ["no hash database loaded (dcs/data/hashes.txt missing)"],
        )

    # -- batch entry point -------------------------------------------------
    def scan_batch(self, entries: Sequence[FileEntry], ctx: ScanContext) -> Iterator[Finding]:
        for entry in entries:
            if ctx.cancelled:
                return
            try:
                findings = list(self.analyze(entry, ctx))
            except OSError as exc:
                ctx.log(f"heuristics could not read {entry.path}: {exc}", "warning")
                findings = []
            finally:
                ctx.tick_file(entry.size, entry.path)
            yield from findings

    # -- one file ----------------------------------------------------------
    def analyze(self, entry: FileEntry, ctx: ScanContext) -> Iterator[Finding]:
        entry_findings: list[Finding] = []
        entry_findings.extend(self._check_name(entry))
        entry_findings.extend(self._check_known_hash(entry))

        if entry.is_special:
            entry_findings.append(
                self._finding(
                    entry,
                    "special file (device, fifo or socket)",
                    Severity.MEDIUM,
                    "filesystem",
                    "Special files on a removable medium are unusual and may be used to "
                    "trigger privileged actions when the stick is mounted.",
                )
            )
            yield from entry_findings
            return

        max_bytes = ctx.config.max_file_size_mb * 1024 * 1024
        if max_bytes and entry.size > max_bytes:
            yield from entry_findings
            return

        head = _read_head(entry.path, rules.HEAD_BYTES)
        if not head:
            yield from entry_findings
            return

        entry_findings.extend(self._check_eicar(head, entry))
        entry_findings.extend(self._check_binary(entry, head, ctx))
        entry_findings.extend(self._check_document(entry, head, ctx))
        entry_findings.extend(self._check_text(entry, head, ctx))
        yield from entry_findings

    # ------------------------------------------------------------------
    # name based checks
    # ------------------------------------------------------------------
    def _check_name(self, entry: FileEntry) -> list[Finding]:
        findings: list[Finding] = []
        name = entry.name
        lowered = name.lower()
        parts = lowered.rsplit(".", 2)

        for pattern, threat, severity in rules.SUSPICIOUS_NAME_PATTERNS:
            if pattern.search(name):
                findings.append(
                    self._finding(entry, threat, Severity(severity), "filename", f"Matched name rule: {pattern.pattern}")
                )

        for keyword, threat, severity in rules.MALWARE_NAME_KEYWORDS:
            if keyword in lowered:
                findings.append(
                    self._finding(
                        entry,
                        f"known offensive tool name: {keyword}",
                        Severity(severity),
                        "tooling",
                        f"File name contains {keyword!r} ({threat}).",
                    )
                )

        # double extension: report.pdf.exe / photo.jpg.scr
        if len(parts) >= 3:
            final = "." + parts[-1]
            previous = "." + parts[-2]
            if final in rules.EXECUTABLE_EXTENSIONS and previous in rules.DECOY_EXTENSIONS:
                findings.append(
                    self._finding(
                        entry,
                        "double extension executable",
                        Severity.HIGH,
                        "masquerading",
                        f"The file pretends to be {previous} but is executed as {final}.",
                    )
                )

        if lowered in rules.DUCKY_NAME_PATTERNS or any(
            token in lowered for token in ("ducky", "badusb", "hid_payload")
        ):
            findings.append(
                self._finding(
                    entry,
                    "possible physical payload file",
                    Severity.MEDIUM,
                    "physical-payload",
                    "The file name points at a HID injection payload (Rubber Ducky family).",
                )
            )

        if entry.relative == name and entry.suffix in rules.ROOT_SUSPICIOUS_EXTENSIONS:
            # an executable sitting in the drive root, where autorun looks first
            findings.append(
                self._finding(
                    entry,
                    "executable in the root of the drive",
                    Severity.MEDIUM,
                    "placement",
                    "Executables in the drive root are characteristic of USB worm and "
                    "autorun-based spreading.",
                )
            )
        return findings

    def _check_known_hash(self, entry: FileEntry) -> list[Finding]:
        if not self.hash_db:
            return []
        from dcs.core.util import sha256_file

        try:
            digest = sha256_file(entry.path)
        except OSError:
            return []
        threat = self.hash_db.get(digest)
        if not threat:
            return []
        return [
            self._finding(
                entry,
                f"known malware hash: {threat}",
                Severity.CRITICAL,
                "known-hash",
                f"SHA-256 {digest} is on the DCS block list.",
                sha256=digest,
            )
        ]

    # ------------------------------------------------------------------
    # generic content checks
    # ------------------------------------------------------------------
    def _check_eicar(self, head: bytes, entry: FileEntry) -> list[Finding]:
        if rules.EICAR_MARKER in head or rules.EICAR_SIGNATURE in head:
            return [
                self._finding(
                    entry,
                    "EICAR-AV-Test-File",
                    Severity.CRITICAL,
                    "test-file",
                    "The EICAR test pattern is present: this file exists purely to prove "
                    "that a scanner works.",
                )
            ]
        return []

    def _check_binary(self, entry: FileEntry, head: bytes, ctx: ScanContext) -> list[Finding]:
        findings: list[Finding] = []
        if head.startswith(_MAGIC_PE):
            findings.extend(self._check_pe(entry, head, ctx))
        elif head.startswith(_MAGIC_ELF):
            findings.extend(self._check_elf(entry, head))
        elif head.startswith(_MAGIC_OLE):
            findings.extend(self._check_ole(entry, ctx))
        elif any(head.startswith(magic) for magic in _MAGIC_ZIP) or head.startswith(b"PK\x03\x04"):
            findings.extend(self._check_zip(entry, ctx))
        elif head.startswith(_MAGIC_RAR) or head.startswith(_MAGIC_7Z) or head.startswith(_MAGIC_CAB):
            findings.extend(self._check_opaque_archive(entry))
        elif head.startswith(_MAGIC_LNK):
            findings.extend(self._check_lnk(entry, head))
        return findings

    def _check_document(self, entry: FileEntry, head: bytes, ctx: ScanContext) -> list[Finding]:
        if head.startswith(_MAGIC_PDF):
            return self._check_pdf(entry, ctx)
        if head.startswith(_MAGIC_RTF) and b"objupdate" in head.lower():
            return [
                self._finding(
                    entry,
                    "RTF object update",
                    Severity.HIGH,
                    "office",
                    "The RTF file contains an embedded object that is updated on open, a "
                    "known code execution path.",
                )
            ]
        return []

    # ------------------------------------------------------------------
    # text / script checks
    # ------------------------------------------------------------------
    def _check_text(self, entry: FileEntry, head: bytes, ctx: ScanContext) -> list[Finding]:
        suffix = entry.suffix
        interesting_name = any(token in entry.name.lower() for token in rules.DUCKY_NAME_PATTERNS)
        if suffix not in rules.TEXT_EXTENSIONS and not interesting_name and not _looks_like_text(head):
            return []

        if entry.size > rules.DEEP_READ_BYTES:
            data = head
        else:
            data = _read_head(entry.path, rules.DEEP_READ_BYTES)
        if not data:
            return []

        text = _decode_text(data)
        findings: list[Finding] = []
        findings.extend(self._check_ducky(entry, text, interesting_name))
        findings.extend(self._check_script_indicators(entry, text))
        findings.extend(self._check_autorun(entry, text))
        findings.extend(self._check_registry(entry, text))
        findings.extend(self._check_embedded_blob(entry, data))
        return findings

    def _check_ducky(self, entry: FileEntry, text: str, forced: bool) -> list[Finding]:
        verdict = analyze_ducky_script(text)
        if not verdict.is_payload:
            return []
        severity = Severity.HIGH
        detail = (
            f"{verdict.commands} HID command lines out of "
            f"{verdict.command_ratio:.0%} of the script lines."
        )
        if verdict.keywords:
            severity = Severity.CRITICAL
            detail += " Dangerous payload steps: " + ", ".join(sorted(set(verdict.keywords))[:6]) + "."
        elif not forced:
            severity = Severity.MEDIUM
        return [
            self._finding(
                entry,
                "Rubber Ducky / BadUSB payload script",
                severity,
                "physical-payload",
                detail
                + " Such a script types commands into the victim machine as soon as the "
                "stick is plugged in.",
            )
        ]

    def _check_script_indicators(self, entry: FileEntry, text: str) -> list[Finding]:
        findings: list[Finding] = []
        lowered = text.lower()
        for pattern, threat, severity, description in rules.SCRIPT_INDICATORS:
            match = pattern.search(text)
            if not match:
                continue
            snippet = match.group(0).strip().replace("\n", " ")[:120]
            findings.append(
                self._finding(
                    entry,
                    threat,
                    Severity(severity),
                    "script",
                    f"{description} Evidence: {snippet!r}",
                )
            )

        if "http" in lowered:
            for match in list(_URL.finditer(text.encode("utf-8", "ignore")))[:5]:
                url = match.group(0).decode("utf-8", "ignore")
                if rules.C2_URL_PATTERN.match(url):
                    findings.append(
                        self._finding(
                            entry,
                            "suspicious remote endpoint",
                            Severity.MEDIUM,
                            "network",
                            f"The file references {url}, an address shape typical of "
                            "throw-away command and control domains.",
                        )
                    )
                    break
        return findings

    def _check_autorun(self, entry: FileEntry, text: str) -> list[Finding]:
        if entry.name.lower() != "autorun.inf":
            return []
        findings: list[Finding] = []
        commands: list[str] = []
        payload_seen = False
        # Only the keys that actually start something make this file a launcher.
        # An icon= or label= entry is cosmetic, and flagging every autorun.inf
        # that merely sets an icon would drown the real finding in noise.
        launch_keys = ("open", "shellexecute", "run")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(";") or stripped.startswith("["):
                continue
            key, _, value = stripped.partition("=")
            key = key.strip().lower()
            value = value.strip().strip('"')
            if not value:
                continue
            if key not in launch_keys:
                if key in ("icon", "action", "label", "shell"):
                    commands.append(f"{key}={value} (not a launcher)")
                continue
            commands.append(f"{key}={value}")
            if os.path.splitext(value)[1].lower() in rules.EXECUTABLE_EXTENSIONS:
                payload_seen = True
            target = os.path.join(os.path.dirname(entry.path), value)
            if os.path.exists(target):
                payload_seen = True
        if not any(not command.endswith("(not a launcher)") for command in commands):
            return findings
        commands = [command for command in commands if not command.endswith("(not a launcher)")]
        severity = Severity.CRITICAL if payload_seen else Severity.HIGH
        findings.append(
            self._finding(
                entry,
                "autorun.inf launches a payload",
                severity,
                "autorun",
                "The file auto-executes on mount: " + "; ".join(commands[:5]),
            )
        )
        return findings

    def _check_registry(self, entry: FileEntry, text: str) -> list[Finding]:
        if entry.suffix not in (".reg", ".inf", ".vbs", ".ps1", ".bat", ".cmd") and not text.lstrip().startswith(
            ("Windows Registry Editor", "[Version]")
        ):
            return []
        findings: list[Finding] = []
        for pattern, threat, severity in rules.REGISTRY_INDICATORS:
            if pattern.search(text):
                findings.append(
                    self._finding(entry, threat, Severity(severity), "registry", f"Matched {pattern.pattern}")
                )
        return findings

    def _check_embedded_blob(self, entry: FileEntry, data: bytes) -> list[Finding]:
        """A long base64 blob inside a script-ish file is worth flagging."""
        if entry.suffix not in rules.SCRIPT_EXTENSIONS and entry.suffix not in (".txt", ".dat", ""):
            return []
        match = _BASE64_BLOB.search(data)
        if not match:
            return []
        blob = match.group(0)
        if len(blob) < 512:
            return []
        if blob[:4] in (b"UEsD", b"TVqQ", b"7Zqy"):  # base64 of PK../MZ../7z..
            detail = "The blob decodes to an archive or a Windows executable header."
            severity = Severity.CRITICAL
        else:
            detail = "Large base64 blob; decoded content cannot be inspected without running it."
            severity = Severity.MEDIUM
        return [
            self._finding(
                entry,
                "embedded base64 payload",
                severity,
                "obfuscation",
                f"{len(blob)} base64 characters starting with {blob[:24].decode('ascii', 'replace')!r}. {detail}",
            )
        ]

    # ------------------------------------------------------------------
    # binary format checks
    # ------------------------------------------------------------------
    def _check_pe(self, entry: FileEntry, head: bytes, ctx: ScanContext) -> list[Finding]:
        findings: list[Finding] = []
        try:
            offset = struct.unpack_from("<I", head, 0x3C)[0]
        except struct.error:
            return findings
        if offset + 24 > len(head) or head[offset : offset + 4] != b"PE\x00\x00":
            findings.append(
                self._finding(
                    entry,
                    "malformed PE header",
                    Severity.MEDIUM,
                    "executable",
                    "The file claims to be a Windows executable but the PE header is not "
                    "where it should be, which is typical for tampered or truncated samples.",
                )
            )
            return findings

        _machine, sections, timestamp = struct.unpack_from("<HHI", head, offset + 4)
        characteristics = struct.unpack_from("<H", head, offset + 22)[0]
        is_dll = bool(characteristics & 0x2000)
        optional_magic = struct.unpack_from("<H", head, offset + 24)[0] if offset + 26 <= len(head) else 0
        bitness = "PE32+" if optional_magic == 0x20B else "PE32"

        packed = [name for marker, name in rules.PACKER_MARKERS if marker in head]
        if packed:
            findings.append(
                self._finding(
                    entry,
                    f"packed executable ({packed[0]})",
                    Severity.HIGH,
                    "packer",
                    "The binary is compressed or encrypted by a packer, which hides its real "
                    "code from static analysis.",
                )
            )

        matched_api = [name for name, _, _ in rules.SUSPICIOUS_API_NAMES if name.encode() in head]
        injection = [n for n in matched_api if n in ("WriteProcessMemory", "CreateRemoteThread", "NtUnmapViewOfSection")]
        keylogging = [n for n in matched_api if n in ("GetAsyncKeyState", "GetKeyState", "SetWindowsHookEx")]
        if injection:
            findings.append(
                self._finding(
                    entry,
                    "process injection API usage",
                    Severity.CRITICAL,
                    "executable",
                    "Imports/strings typical of process injection: " + ", ".join(injection),
                )
            )
        if keylogging:
            findings.append(
                self._finding(
                    entry,
                    "keystroke capture API usage",
                    Severity.HIGH,
                    "executable",
                    "Imports/strings typical of keylogging: " + ", ".join(keylogging),
                )
            )

        entropy = _entropy(head[: min(len(head), 200_000)])
        if entropy > rules.HIGH_ENTROPY_THRESHOLD and entry.size > 200_000:
            findings.append(
                self._finding(
                    entry,
                    "high entropy executable body",
                    Severity.MEDIUM,
                    "obfuscation",
                    f"Entropy {entropy:.2f}/8 suggests an encrypted or compressed payload; "
                    "static analysis is unlikely to see the real behaviour.",
                )
            )

        if b"mscoree.dll" in head and not packed:
            findings.append(
                self._finding(
                    entry,
                    ".NET assembly",
                    Severity.INFO,
                    "executable",
                    "Managed .NET binary; decompilable with standard tools if needed.",
                )
            )

        if not any(severity_ok(f) for f in findings):
            findings.append(
                self._finding(
                    entry,
                    f"Windows executable ({bitness}{', DLL' if is_dll else ''})",
                    Severity.INFO,
                    "executable",
                    f"{sections} sections, timestamp {timestamp}. No obvious malicious "
                    "indicator, but executables on a removable drive deserve a second look.",
                )
            )
        return findings

    def _check_elf(self, entry: FileEntry, head: bytes) -> list[Finding]:
        findings: list[Finding] = []
        if b"UPX!" in head:
            findings.append(
                self._finding(
                    entry,
                    "UPX packed ELF binary",
                    Severity.MEDIUM,
                    "packer",
                    "The executable is packed, so its strings and imports are hidden.",
                )
            )
        entropy = _entropy(head[: min(len(head), 200_000)])
        if entropy > rules.HIGH_ENTROPY_THRESHOLD and entry.size > 200_000:
            findings.append(
                self._finding(
                    entry,
                    "high entropy ELF binary",
                    Severity.MEDIUM,
                    "obfuscation",
                    f"Entropy {entropy:.2f}/8: probably packed or encrypted.",
                )
            )
        return findings

    def _check_ole(self, entry: FileEntry, ctx: ScanContext) -> list[Finding]:
        """Legacy Word/Excel documents, and anything else in a CFB container."""
        data = _read_head(entry.path, rules.DEEP_READ_BYTES)
        lowered = data.lower()
        findings: list[Finding] = []
        if b"vba" in lowered or b"attribute vb_" in lowered:
            severity = Severity.HIGH
            detail = "The document embeds a Visual Basic for Applications macro project."
            for keyword in (b"autoopen", b"auto_open", b"document_open", b"workbook_open"):
                if keyword in lowered:
                    detail += " It contains an automatic entry point (" + keyword.decode() + "), so it runs on open."
                    severity = Severity.CRITICAL
                    break
            if b"createobject" in lowered or b"wscript.shell" in lowered or b"shellexecute" in lowered:
                detail += " The macro can start other programs."
                severity = Severity.CRITICAL
            findings.append(self._finding(entry, "Office macro with automatic execution", severity, "office", detail))
        elif b"msexcel" in lowered or b"msword" in lowered:
            findings.append(
                self._finding(
                    entry,
                    "legacy Office document",
                    Severity.INFO,
                    "office",
                    "Old binary Office format; no macro stream detected.",
                )
            )
        return findings

    def _check_zip(self, entry: FileEntry, ctx: ScanContext) -> list[Finding]:
        """OOXML documents and plain ZIP archives share the container format."""
        findings: list[Finding] = []
        try:
            with zipfile.ZipFile(entry.path) as archive:
                names = archive.namelist()
                lowered = [n.lower() for n in names]
                is_office = any(n.startswith("[content_types].xml") or "word/" in n or "xl/" in n or "ppt/" in n for n in lowered)
                if is_office:
                    findings.extend(self._check_ooxml(entry, archive, lowered))
                else:
                    findings.extend(self._check_zip_archive(entry, archive, names, lowered, ctx))
        except (zipfile.BadZipFile, OSError, NotImplementedError):
            return findings
        except RuntimeError as exc:  # encrypted central directory
            findings.append(
                self._finding(
                    entry,
                    "encrypted archive",
                    Severity.HIGH,
                    "archive",
                    f"The archive headers are encrypted ({exc}); its contents cannot be "
                    "scanned by any tool on this machine.",
                )
            )
        return findings

    def _check_ooxml(self, entry: FileEntry, archive: zipfile.ZipFile, lowered: list[str]) -> list[Finding]:
        findings: list[Finding] = []
        if any(name.endswith("vbaproject.bin") for name in lowered):
            findings.append(
                self._finding(
                    entry,
                    "Office document with VBA macro",
                    Severity.HIGH,
                    "office",
                    "vbaProject.bin is inside the document; it runs when the document is "
                    "opened with macros enabled.",
                )
            )
        if any("/embeddings/" in name for name in lowered):
            findings.append(
                self._finding(
                    entry,
                    "Office document with embedded object",
                    Severity.MEDIUM,
                    "office",
                    "The document carries an embedded OLE object, a classic dropper.",
                )
            )
        try:
            for name in archive.namelist():
                if not name.lower().endswith(".rels"):
                    continue
                payload = archive.read(name)[: 64 * 1024]
                if b'TargetMode="External"' in payload or b"TargetMode='External'" in payload:
                    findings.append(
                        self._finding(
                            entry,
                            "external content reference",
                            Severity.HIGH,
                            "office",
                            f"{name} pulls content from a remote location; remote template "
                            "injection abuses exactly this.",
                        )
                    )
                    break
        except (KeyError, OSError, zipfile.BadZipFile):
            pass
        return findings

    def _check_zip_archive(
        self,
        entry: FileEntry,
        archive: zipfile.ZipFile,
        names: list[str],
        lowered: list[str],
        ctx: ScanContext,
    ) -> list[Finding]:
        findings: list[Finding] = []
        try:
            infos = archive.infolist()
        except (zipfile.BadZipFile, OSError):
            return findings

        encrypted = [info for info in infos if info.flag_bits & 0x1]
        if encrypted:
            findings.append(
                self._finding(
                    entry,
                    "password protected archive",
                    Severity.HIGH,
                    "archive",
                    f"{len(encrypted)} member(s) are encrypted, so their content cannot be "
                    "scanned. Malware is frequently shipped inside password protected ZIPs.",
                )
            )

        uncompressed = sum(info.file_size for info in infos)
        compressed = sum(info.compress_size for info in infos) or 1
        ratio = uncompressed / compressed
        if ratio > rules.ZIP_BOMB_RATIO and uncompressed > rules.ZIP_BOMB_MIN_UNCOMPRESSED:
            findings.append(
                self._finding(
                    entry,
                    "suspected zip bomb",
                    Severity.HIGH,
                    "archive",
                    f"Expansion ratio {ratio:.0f}:1 ({uncompressed} bytes). Unpacking this "
                    "would fill the disk.",
                )
            )

        dangerous = [
            name
            for name, low in zip(names, lowered, strict=True)
            if os.path.splitext(low)[1] in rules.INTERESTING_ARCHIVE_MEMBERS and "/" not in name.strip("/")
        ]
        if dangerous:
            findings.append(
                self._finding(
                    entry,
                    "archive with executables at its root",
                    Severity.MEDIUM,
                    "archive",
                    "Extracting this archive drops executable content straight into the "
                    "target folder: " + ", ".join(dangerous[:5]),
                )
            )
        return findings

    def _check_opaque_archive(self, entry: FileEntry) -> list[Finding]:
        """RAR/7z/CAB: the built-in engine cannot look inside, so say so."""
        return [
            self._finding(
                entry,
                "archive not inspectable by the built-in engine",
                Severity.INFO,
                "archive",
                "RAR/7z/CAB contents are only covered when a ClamAV engine is enabled; "
                "consider enabling ClamAV for a complete scan.",
            )
        ]

    def _check_lnk(self, entry: FileEntry, head: bytes) -> list[Finding]:
        findings: list[Finding] = []
        # Windows shortcuts store their target both as ASCII and as UTF-16LE,
        # so both spellings have to be searched for.
        def present(needle: bytes) -> bool:
            return needle in lowered or needle.decode("ascii").encode("utf-16-le") in head

        lowered = head.lower()
        indicators = []
        if present(b"cmd.exe") or present(b"powershell") or present(b"pwsh"):
            indicators.append("starts a command interpreter")
        if present(b"mshta") or present(b"rundll32") or present(b"wscript") or present(b"certutil"):
            indicators.append("uses a living-off-the-land binary")
        if present(b"\\\\") or b"\\\\\\\\" in head:
            indicators.append("points at a UNC network path")
        if present(b"http://") or present(b"https://"):
            indicators.append("contains a URL")
        try:
            show_command = struct.unpack_from("<I", head, 0x3C)[0]
        except struct.error:
            show_command = 1
        if show_command == 7:
            indicators.append("is configured to run minimized")
        if indicators:
            findings.append(
                self._finding(
                    entry,
                    "suspicious shortcut",
                    Severity.HIGH,
                    "shortcut",
                    "The .lnk file " + " and ".join(indicators) + ".",
                )
            )
        return findings

    def _check_pdf(self, entry: FileEntry, ctx: ScanContext) -> list[Finding]:
        data = _read_head(entry.path, rules.PDF_READ_BYTES)
        lowered = data.lower()
        findings: list[Finding] = []
        javascript = b"/javascript" in lowered or b"/js" in lowered
        auto_action = b"/openaction" in lowered or b"/aa" in lowered
        launch = b"/launch" in lowered
        embedded = b"/embeddedfile" in lowered or b"/filespec" in lowered
        if javascript and (auto_action or launch):
            findings.append(
                self._finding(
                    entry,
                    "PDF with automatic JavaScript",
                    Severity.HIGH,
                    "document",
                    "The PDF runs JavaScript when it is opened, the standard vector for "
                    "PDF exploits.",
                )
            )
        elif javascript or launch or embedded:
            findings.append(
                self._finding(
                    entry,
                    "PDF with active content",
                    Severity.MEDIUM,
                    "document",
                    "The PDF contains JavaScript, a launch action or an embedded file.",
                )
            )
        return findings

    # -- helper ------------------------------------------------------------
    def _finding(
        self,
        entry: FileEntry,
        threat: str,
        severity: Severity,
        category: str,
        detail: str,
        *,
        sha256: str = "",
    ) -> Finding:
        return Finding(
            path=entry.path,
            threat=threat,
            engine=self.engine_id,
            severity=severity,
            category=category,
            size=entry.size,
            sha256=sha256,
            detail=detail,
        )


def severity_ok(finding: Finding) -> bool:
    return finding.severity in (Severity.HIGH, Severity.CRITICAL)


# ---------------------------------------------------------------------------
# stand-alone helpers (also used by the tests)
# ---------------------------------------------------------------------------


def analyze_ducky_script(text: str | None) -> DuckyVerdict:
    """Score a text file for DuckyScript style HID payload commands."""
    if not text:
        return DuckyVerdict()
    lines = [line.strip() for line in text.replace("\r", "\n").split("\n")]
    meaningful = [line for line in lines if line and not line.startswith(("REM", ";", "#", "//"))]
    commands = 0
    keywords: list[str] = []
    for line in meaningful:
        head = line.split(None, 1)[0].upper()
        if head in rules.DUCKY_COMMANDS:
            commands += 1
            lowered = line.lower()
            for keyword, description in rules.DUCKY_DANGEROUS_KEYWORDS:
                if keyword in lowered:
                    keywords.append(description)
    ratio = commands / len(meaningful) if meaningful else 0.0
    return DuckyVerdict(commands=commands, command_ratio=ratio, keywords=keywords)


def load_hash_db(path: str | None = None) -> dict[str, str]:
    """Load a sha256 -> threat name list shipped with the package."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "hashes.txt")
    database: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                digest, _, name = line.partition(" ")
                digest = digest.strip().lower()
                if len(digest) == 64:
                    database[digest] = name.strip() or "known malware"
    except OSError:
        return {}
    return database
