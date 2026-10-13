"""ClamAV integration.

Two binaries are supported, chosen automatically:

* clamdscan -- talks to a running clamd.  Much faster on a USB stick with
  thousands of small files, and --fdpass lets the daemon read files the
  unprivileged user could not open itself.
* clamscan  -- the stand-alone scanner.  Slower because it reloads the
  signature database for every invocation, so DCS hands it one large batch.

Progress works because both tools accept a *file list* (--file-list) and print
one line per file in verbose mode, which is exactly the granularity the GUI
wants to show.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dcs.core.config import ClamAVConfig
from dcs.core.events import Finding, Severity
from dcs.core.log import get_logger
from dcs.core.scanner.base import EngineStatus, ScanContext
from dcs.core.util import FileEntry, human_bytes, tool_version, which

log = get_logger("scan.clamav")

#: "path: OK" / "path: Eicar-Test-Signature FOUND" / "path: Access denied ERROR"
_RESULT_LINE = re.compile(r"^(?P<path>.+?):\s(?P<rest>.+?)(?:\s(?P<status>FOUND|ERROR))?$")

#: ClamAV signature database directories, newest file wins
DB_DIRS = ("/var/lib/clamav", "/usr/local/share/clamav", "/usr/share/clamav", "/var/db/clamav")

#: threat name prefixes that are not treated as malware
_NON_MALWARE_PREFIXES = (
    "pua.",
    "puas.",
    "heuristics.pua",
    "adware",
    "riskware",
    "spyware.agents",
    "unwanted",
    "tracker",
    "proxy",
    "joke",
    "test",
    "doc.dropper.ole2.template.aggressivedoctype",  # noisy, often a false positive
)


@dataclass(slots=True)
class DatabaseInfo:
    path: str = ""
    modified: float = 0.0

    @property
    def age_days(self) -> float:
        if not self.modified:
            return -1.0
        return max(0.0, (time.time() - self.modified) / 86400.0)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "ageDays": round(self.age_days, 1) if self.modified else None,
            "newestSignature": self.path,
        }


@dataclass
class ClamAVProbe:
    """Result of looking for a usable ClamAV installation."""

    binary: str = ""          # absolute path of the binary to use
    mode: str = ""            # "clamdscan" or "clamscan"
    version: str = ""
    daemon_ok: bool = False
    database: DatabaseInfo = field(default_factory=DatabaseInfo)
    problem: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.binary)


def _newest_database() -> DatabaseInfo:
    newest = DatabaseInfo()
    for directory in DB_DIRS:
        if not os.path.isdir(directory):
            continue
        try:
            for entry in os.scandir(directory):
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not entry.name.endswith((".cvd", ".cld", ".cud", ".hdb", ".ndb", ".ldb", ".sdb", ".mdb")):
                    continue
                mtime = entry.stat().st_mtime
                if mtime > newest.modified:
                    newest = DatabaseInfo(path=entry.path, modified=mtime)
        except OSError:
            continue
    return newest


def probe(config: ClamAVConfig) -> ClamAVProbe:
    """Decide which ClamAV front end (if any) is usable right now."""
    result = ClamAVProbe(database=_newest_database())

    clamdscan = which(config.clamdscan_binary)
    clamscan = which(config.clamscan_binary)

    if config.prefer_daemon and clamdscan:
        version = tool_version(clamdscan) or ""
        ping_ok = False
        try:
            proc = subprocess.run(
                [clamdscan, "--ping", "3"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            ping_ok = proc.returncode == 0
            if not ping_ok and proc.stderr:
                result.problem = proc.stderr.strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError) as exc:
            result.problem = str(exc)
        if ping_ok:
            result.binary = clamdscan
            result.mode = "clamdscan"
            result.version = version
            result.daemon_ok = True
            return result

    if clamscan:
        version = tool_version(clamscan) or ""
        result.binary = clamscan
        result.mode = "clamscan"
        result.version = version
        if not result.problem:
            result.problem = "clamd is not reachable; using the slower stand-alone clamscan"
        return result

    if clamdscan and not result.problem:
        result.problem = "clamdscan is installed but no clamd daemon is listening"
    if not result.problem:
        result.problem = (
            "ClamAV is not installed (install the clamav package, then run freshclam)"
        )
    return result


def database_warning(config: ClamAVConfig, database: DatabaseInfo) -> str:
    if not database.modified:
        return "ClamAV signature database not found; install it with freshclam"
    age = database.age_days
    if age > config.max_database_age_days:
        return (
            f"ClamAV signatures are {age:.0f} days old "
            f"(threshold {config.max_database_age_days}); run freshclam to update them"
        )
    return ""


class ClamAVEngine:
    """ClamAV front end that reports one Finding per detected threat."""

    engine_id = "clamav"
    label = "ClamAV"

    def __init__(self, config: ClamAVConfig) -> None:
        self.config = config
        self.probe = probe(config)
        self.errors: list[str] = []

    # -- status -----------------------------------------------------------
    def status(self) -> EngineStatus:
        warnings: list[str] = []
        warning = database_warning(self.config, self.probe.database)
        if warning:
            warnings.append(warning)
        if self.probe.usable and self.probe.mode == "clamscan":
            warnings.append(
                "clamd is not running: the stand-alone clamscan reloads its database for "
                "every batch and is several times slower"
            )
        detail = self.probe.problem or (
            f"{self.probe.mode} with {self.probe.database.path or 'unknown database'}"
        )
        return EngineStatus(
            engine_id=self.engine_id,
            label=self.label,
            available=self.probe.usable,
            version=self.probe.version,
            detail=detail,
            warnings=warnings,
        )

    # -- scanning ---------------------------------------------------------
    def build_command(self, list_path: str) -> list[str]:
        if self.probe.mode == "clamdscan":
            argv = [
                self.probe.binary,
                "--fdpass",
                "--verbose",
                "--no-summary",
                "--file-list",
                list_path,
            ]
        else:
            argv = [
                self.probe.binary,
                "--verbose",
                "--no-summary",
                "--file-list",
                list_path,
            ]
        argv.extend(self.config.extra_args)
        return argv

    def scan_batch(self, entries: Sequence[FileEntry], ctx: ScanContext) -> Iterator[Finding]:
        if not self.probe.usable or not entries:
            return

        # A file list keeps the command line short even for 100k files.  Paths
        # containing a newline cannot be expressed this way and are reported
        # instead of silently skipped.
        skip_newlines = [e for e in entries if "\n" in e.path or "\r" in e.path]
        for entry in skip_newlines:
            ctx.tick_file(entry.size, entry.path)
            yield Finding(
                path=entry.path,
                threat="file name contains a newline: not scannable by ClamAV",
                engine=self.engine_id,
                severity=Severity.MEDIUM,
                category="engine-limitation",
                size=entry.size,
            )
        scannable = [e for e in entries if "\n" not in e.path and "\r" not in e.path]
        if not scannable:
            return

        by_path = {entry.path: entry for entry in scannable}
        by_name: dict[str, FileEntry] = {}
        for entry in scannable:
            by_name.setdefault(entry.name, entry)

        handle, list_path = tempfile.mkstemp(prefix="dcs-clam-", suffix=".lst")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", errors="surrogateescape") as stream:
                stream.write("\n".join(entry.path for entry in scannable))
                stream.write("\n")
            yield from self._run(self.build_command(list_path), by_path, by_name, ctx)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(list_path)

    def _run(
        self,
        argv: list[str],
        by_path: dict[str, FileEntry],
        by_name: dict[str, FileEntry],
        ctx: ScanContext,
    ) -> Iterator[Finding]:
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            self.errors.append(f"cannot start {argv[0]}: {exc}")
            ctx.log(f"cannot start {argv[0]}: {exc}", "error")
            return

        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                if ctx.cancelled:
                    proc.terminate()
                    break
                line = raw.rstrip("\r\n")
                if not line or line.startswith(("-----------", "SCAN SUMMARY")):
                    continue
                finding, entry, status = self._parse_line(line, by_path, by_name)
                if entry is not None and status != "ignored":
                    ctx.tick_file(entry.size, entry.path)
                if finding is not None:
                    yield finding
        finally:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - engine hung
                proc.kill()
                proc.wait(timeout=10)

        if proc.returncode not in (0, 1):
            message = f"ClamAV exited with code {proc.returncode}"
            self.errors.append(message)
            ctx.log(message, "warning")

    def _parse_line(
        self,
        line: str,
        by_path: dict[str, FileEntry],
        by_name: dict[str, FileEntry],
    ) -> tuple[Finding | None, FileEntry | None, str]:
        match = _RESULT_LINE.match(line)
        if not match:
            return None, None, "ignored"
        path = match.group("path")
        rest = match.group("rest").strip()
        status = (match.group("status") or "").strip()

        entry = by_path.get(path) or by_name.get(os.path.basename(path))
        if entry is None:
            # clamd may sanitise exotic characters in the path it echoes back
            for candidate in by_path:
                if candidate.endswith(path) or path.endswith(os.path.basename(candidate)):
                    entry = by_path[candidate]
                    break

        if status == "FOUND":
            severity = Severity.CRITICAL
            lowered = rest.lower()
            if lowered.startswith(_NON_MALWARE_PREFIXES):
                severity = Severity.MEDIUM
            finding = Finding(
                path=entry.path if entry else path,
                threat=rest,
                engine=self.engine_id,
                severity=severity,
                category="antivirus",
                size=entry.size if entry else 0,
                detail=(
                    "ClamAV signature match"
                    + (f" via {self.probe.mode}" if self.probe.mode else "")
                    + f" ({self.probe.version})"
                ),
            )
            return finding, entry, "found"

        if status == "ERROR" or rest.endswith("ERROR"):
            self.errors.append(f"{path}: {rest}")
            return None, entry, "error"

        # "OK", "Empty file", "Symbolic link", "Excluded", ...
        return None, entry, "ok"


def clamav_status_line(probe_result: ClamAVProbe) -> str:
    """One line summary used by the CLI doctor command."""
    if not probe_result.usable:
        return f"ClamAV: not available ({probe_result.problem})"
    parts = [f"ClamAV: {probe_result.mode} {probe_result.version}".strip()]
    if probe_result.database.path:
        parts.append(
            f"database {Path(probe_result.database.path).name} "
            f"({probe_result.database.age_days:.0f} days old, "
            f"{human_bytes(os.path.getsize(probe_result.database.path)) if os.path.exists(probe_result.database.path) else '?'})"
        )
    return " | ".join(parts)
