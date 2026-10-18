"""YARA rule scanning.

YARA is the second signature engine, and the only one whose rules are the
user's: it reads rule files (or directories of them), compiles them, and walks
the medium itself.  That shape is different from ClamAV, which is handed a list
of files 400 at a time, so the engine exposes scan_tree() and the scanner runs
it in its own thread beside the batch engines.

Neither YARA nor YARA-X report how many files they have looked at - they only
print the matches - so this engine deliberately does not tick the progress
counters.  The batch engines own those, and two engines counting the same file
would double the bar.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from dcs.core.config import YaraConfig, xdg_data_home
from dcs.core.events import Finding, Severity, severity_rank
from dcs.core.log import get_logger
from dcs.core.scanner.base import EngineStatus, ScanContext
from dcs.core.util import tool_version, which

log = get_logger("scan.yara")

#: the suffixes YARA itself recognises
RULE_SUFFIXES = (".yar", ".yara")

#: where distributions and rule packs usually keep their rules
SYSTEM_RULE_DIRS = ("/usr/share/yara", "/usr/local/share/yara", "/etc/yara")

#: "rule [tags] path", with an optional namespace in front ("ns:rule path")
_MATCH = re.compile(
    r"^(?:(?P<namespace>[A-Za-z_][\w.]*):)?"
    r"(?P<rule>[A-Za-z_]\w*)"
    r"(?:\s+\[(?P<tags>[^\]]*)\])?"
    r"\s+(?P<path>.+)$"
)

#: lines that are YARA talking about itself, not about a file
_DIAGNOSTIC_PREFIXES = ("error", "warning", "notice", "fatal")

#: a rule tag that says how bad the match is wins over the configured default
_TAG_SEVERITY: dict[str, Severity] = {
    "malware": Severity.CRITICAL,
    "trojan": Severity.CRITICAL,
    "backdoor": Severity.CRITICAL,
    "ransomware": Severity.CRITICAL,
    "apt": Severity.CRITICAL,
    "stealer": Severity.HIGH,
    "dropper": Severity.HIGH,
    "credential": Severity.HIGH,
    "botnet": Severity.HIGH,
    "miner": Severity.HIGH,
    "suspicious": Severity.MEDIUM,
    "pua": Severity.LOW,
    "adware": Severity.LOW,
    "riskware": Severity.LOW,
    "info": Severity.INFO,
    "informational": Severity.INFO,
    "metadata": Severity.INFO,
}


@dataclass(slots=True)
class YaraProbe:
    """Result of looking for a usable YARA installation."""

    binary: str = ""
    flavour: str = ""  # "yara" or "yara-x"
    version: str = ""
    rules: list[str] = field(default_factory=list)
    problem: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.binary and self.rules)


def rule_files(config: YaraConfig) -> list[str]:
    """Every rule file the configuration points at, in a stable order."""
    candidates = list(config.rules) or [
        str(xdg_data_home() / "dcs" / "yara"),
        *SYSTEM_RULE_DIRS,
    ]
    found: list[str] = []
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in RULE_SUFFIXES:
                    found.append(str(child))
        elif path.is_file():
            found.append(str(path))
        else:
            log.info("YARA rule location %s does not exist", candidate)
    unique: list[str] = []
    seen: set[str] = set()
    for item in found:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def probe(config: YaraConfig) -> YaraProbe:
    """Decide whether YARA can run right now, and with which rules."""
    result = YaraProbe()
    binary = which(config.binary) or which("yara") or which("yr")
    if binary is None:
        result.problem = (
            "YARA is not installed (install the 'yara' or 'yara-x' package, or point "
            "scan.yara.binary at the binary)"
        )
        return result
    result.binary = binary
    result.flavour = "yara-x" if os.path.basename(binary).startswith("yr") else "yara"
    result.version = (tool_version(binary) or "").strip()
    result.rules = rule_files(config)
    if not result.rules:
        locations = ", ".join(
            config.rules or [str(xdg_data_home() / "dcs" / "yara"), *SYSTEM_RULE_DIRS]
        )
        result.problem = f"no YARA rules found in {locations}"
    return result


class YaraEngine:
    """Runs one YARA pass over a root and reports every rule match."""

    engine_id = "yara"
    label = "YARA rules"

    def __init__(self, config: YaraConfig) -> None:
        self.config = config
        self.probe = probe(config)
        self.errors: list[str] = []
        #: things the report has to say out loud, e.g. that the rules did not compile
        self.notes: list[str] = []

    # -- status -----------------------------------------------------------
    def status(self) -> EngineStatus:
        warnings: list[str] = []
        if self.probe.usable and self.probe.flavour == "yara-x":
            warnings.append(
                "YARA-X is being used: rules that depend on modules it does not "
                "implement are reported as errors instead of matching"
            )
        if self.probe.usable and len(self.probe.rules) == 1:
            warnings.append(
                "only one rule file is loaded; a rule pack such as Neo23x0's "
                "signature-base covers far more than a single file can"
            )
        if self.probe.problem:
            detail = self.probe.problem
        else:
            names = ", ".join(sorted({Path(item).name for item in self.probe.rules})[:3])
            detail = f"{len(self.probe.rules)} rule file(s): {names}"
        return EngineStatus(
            engine_id=self.engine_id,
            label=self.label,
            available=self.probe.usable,
            version=self.probe.version,
            detail=detail,
            warnings=warnings,
        )

    # -- the pass ---------------------------------------------------------
    def build_command(self, root: str) -> list[str]:
        # Long option names on purpose: they are the ones both YARA and YARA-X
        # document, and a rule pack's wrapper script is easier to read with them.
        argv = [
            self.probe.binary,
            "--recursive",
            "--no-warnings",
            "--print-namespace",
            "--print-tags",
        ]
        if self.config.threads > 0:
            argv.append(f"--threads={self.config.threads}")
        argv.extend(self.config.extra_args)
        argv.extend(self.probe.rules)
        argv.append(root)
        return argv

    def scan_tree(self, root: str, ctx: ScanContext) -> Iterator[Finding]:
        if not self.probe.usable:
            return
        argv = self.build_command(root)
        ctx.log(
            f"YARA: {len(self.probe.rules)} rule file(s) with "
            f"{self.probe.version or 'an unknown version'}"
        )
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
            message = f"cannot start {argv[0]}: {exc}"
            self.errors.append(message)
            self.notes.append(message)
            ctx.log(message, "error")
            return

        assert proc.stdout is not None
        started = time.monotonic()
        timed_out = False
        try:
            for raw in proc.stdout:
                if ctx.cancelled:
                    proc.terminate()
                    break
                if self.config.timeout_s and time.monotonic() - started > self.config.timeout_s:
                    proc.kill()
                    timed_out = True
                    break
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                finding = self._parse_line(line)
                if finding is None:
                    self._maybe_diagnostic(line, ctx)
                    continue
                yield finding
        finally:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - the scan is hung
                proc.kill()
                proc.wait(timeout=10)

        if timed_out:
            message = f"YARA stopped after {self.config.timeout_s}s (scan.yara.timeout_s)"
            self.errors.append(message)
            self.notes.append(message)
            ctx.log(message, "warning")
        if proc.returncode not in (0,):
            message = f"the rule set did not compile (YARA exited with code {proc.returncode})"
            self.errors.append(message)
            self.notes.append(message)
            ctx.log(message, "warning")

    # -- parsing ----------------------------------------------------------
    def _parse_line(self, line: str) -> Finding | None:
        stripped = line.strip()
        if not stripped or stripped.lower().startswith(_DIAGNOSTIC_PREFIXES):
            return None
        match = _MATCH.match(stripped)
        if not match:
            return None
        path = match.group("path").strip()
        # YARA echoes the target it was given.  A "match" for a path that does
        # not exist is a diagnostic line that happened to look like one.
        if not os.path.exists(path):
            return None
        tags = [tag.strip() for tag in (match.group("tags") or "").split(",") if tag.strip()]
        rule = match.group("rule")
        namespace = match.group("namespace") or ""
        return Finding(
            path=path,
            threat=rule,
            engine=self.engine_id,
            severity=self._severity(tags),
            category="yara",
            size=self._size(path),
            detail=self._detail(tags, namespace),
        )

    def _maybe_diagnostic(self, line: str, ctx: ScanContext) -> None:
        lowered = line.strip().lower()
        if lowered.startswith(_DIAGNOSTIC_PREFIXES) or " error" in lowered:
            if "not found" in lowered and "include" in lowered:
                ctx.log(f"YARA: a rule includes a file that is missing: {line.strip()}", "warning")
                return
            ctx.log(f"YARA: {line.strip()}", "warning")

    def _severity(self, tags: list[str]) -> Severity:
        """The rule's own tags when it has them, the configured default otherwise."""
        tagged = [
            _TAG_SEVERITY[tag.lower()]
            for tag in tags
            if tag.lower() in _TAG_SEVERITY
        ]
        if tagged:
            return max(tagged, key=severity_rank)
        try:
            return Severity(self.config.severity)
        except ValueError:  # pragma: no cover - the config validator catches this
            return Severity.HIGH

    def _detail(self, tags: list[str], namespace: str) -> str:
        parts = ["YARA rule match"]
        if tags:
            parts.append("tags: " + ", ".join(tags))
        if namespace:
            parts.append(f"namespace: {namespace}")
        if self.probe.version:
            parts.append(self.probe.version)
        return " · ".join(parts)

    @staticmethod
    def _size(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
