"""Fetch a YARA rule pack, install it, and say what changed.

The YARA engine ships no rules of its own: a rule pack is somebody else's
project, and asking the user to download a tarball and guess where it goes is
why the engine stays switched off.  `dcs yara update` fetches a pack (Neo23x0's
signature-base by default), keeps it in `$XDG_DATA_HOME/dcs/yara/signature-base`
- a directory the engine already scans - compiles it with the local `yara`
binary before it replaces anything, and reports which rule files and rule names
appeared, changed or went away.

The pack is replaced as a whole (staged, verified, then swapped), so a download
that is broken in the middle can never leave the engine with half a rule set.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dcs.core.config import xdg_data_home
from dcs.core.log import get_logger

log = get_logger("core.yara_pack")

#: the rule pack most triage tools start from
DEFAULT_URL = "https://github.com/Neo23x0/signature-base/archive/refs/heads/master.tar.gz"

#: the directory name inside the data directory
PACK_NAME = "signature-base"

#: what counts as a rule file
RULE_SUFFIXES = (".yar", ".yara")

MANIFEST_NAME = "manifest.json"

#: "meta:private rule Name" and the plain form, in any indentation
_RULE = re.compile(r"^\s*(?:(?:private|global)\s+)*rule\s+([A-Za-z_]\w*)", re.M)

#: how long one download may take
TIMEOUT_S = 180.0


class PackError(Exception):
    """The pack could not be fetched, unpacked or compiled."""


@dataclass
class UpdateReport:
    """What one `dcs yara update` did."""

    ok: bool = False
    unchanged: bool = False
    error: str = ""
    directory: str = ""
    url: str = ""
    sha256: str = ""
    added_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)
    dropped_files: list[str] = field(default_factory=list)
    added_rules: list[str] = field(default_factory=list)
    removed_rules: list[str] = field(default_factory=list)
    total_rules: int = 0
    verified: bool | None = None
    verify_note: str = ""

    def summary(self) -> str:
        if self.error:
            return self.error
        if self.unchanged:
            return f"the pack is unchanged ({self.total_rules} rules)"
        parts = [f"{self.total_rules} rules in {len(self.added_files) + len(self.changed_files)} file(s)"]
        if self.added_files:
            parts.append(f"+{len(self.added_files)} new file(s)")
        if self.changed_files:
            parts.append(f"{len(self.changed_files)} changed")
        if self.removed_files:
            parts.append(f"-{len(self.removed_files)} file(s)")
        if self.added_rules:
            parts.append(f"+{len(self.added_rules)} rule(s)")
        if self.removed_rules:
            parts.append(f"-{len(self.removed_rules)} rule(s)")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "unchanged": self.unchanged,
            "error": self.error,
            "directory": self.directory,
            "url": self.url,
            "sha256": self.sha256,
            "addedFiles": list(self.added_files),
            "changedFiles": list(self.changed_files),
            "removedFiles": list(self.removed_files),
            "droppedFiles": list(self.dropped_files),
            "addedRules": list(self.added_rules),
            "removedRules": list(self.removed_rules),
            "totalRules": self.total_rules,
            "verified": self.verified,
            "verifyNote": self.verify_note,
            "summary": self.summary(),
        }


# ---------------------------------------------------------------------------
# where the pack lives
# ---------------------------------------------------------------------------


def pack_dir() -> Path:
    """The directory the pack is installed into (inside the engine's rule path)."""
    return xdg_data_home() / "dcs" / "yara" / PACK_NAME


def rule_files(directory: Path) -> list[Path]:
    return sorted(
        path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in RULE_SUFFIXES
    )


# ---------------------------------------------------------------------------
# fetching and unpacking
# ---------------------------------------------------------------------------


def fetch(url: str, *, timeout: float = TIMEOUT_S) -> bytes:
    """Download the pack.  Tests replace this."""
    request = urllib.request.Request(url, headers={"user-agent": "ducky-can-shoot"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PackError(f"could not download {url}: {exc}") from exc


def _safe_member(name: str) -> str | None:
    """A relative path inside the archive, or None when it tries to escape."""
    clean = name.replace("\\", "/").lstrip("/")
    parts = [part for part in clean.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def extract_rules(blob: bytes) -> dict[str, bytes]:
    """Every rule file in a tarball or zip, keyed by its path without the top directory."""
    files: dict[str, bytes] = {}
    if zipfile.is_zipfile(io.BytesIO(blob)):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for info in archive.infolist():
                if info.is_dir() or not info.filename.lower().endswith(RULE_SUFFIXES):
                    continue
                name = _safe_member(info.filename)
                if name:
                    files[_strip_top(name)] = archive.read(info)
        return files
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not member.name.lower().endswith(RULE_SUFFIXES):
                    continue
                name = _safe_member(member.name)
                if not name:
                    continue
                handle = archive.extractfile(member)
                if handle is not None:
                    files[_strip_top(name)] = handle.read()
    except tarfile.TarError as exc:
        raise PackError(f"the download is not a tarball or a zip: {exc}") from exc
    return files


def _strip_top(name: str) -> str:
    """Drop the "signature-base-master/" prefix a GitHub archive puts on everything."""
    return name.split("/", 1)[1] if "/" in name else name


def rule_names(files: dict[str, bytes]) -> set[str]:
    names: set[str] = set()
    for data in files.values():
        text = data.decode("utf-8", errors="replace")
        names.update(_RULE.findall(text))
    return names


# ---------------------------------------------------------------------------
# the manifest, so the next update can say what changed
# ---------------------------------------------------------------------------


def read_manifest(directory: Path) -> dict[str, Any]:
    try:
        payload = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _digest(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}


# ---------------------------------------------------------------------------
# compile check
# ---------------------------------------------------------------------------


def _compile(paths: list[str], executable: str, timeout: float) -> tuple[bool | None, str]:
    """Compile *paths* together; (None, note) when the binary cannot run at all."""
    try:
        proc = subprocess.run(
            [executable, *paths, os.devnull],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"the compile check could not run: {exc}"
    if proc.returncode == 0:
        return True, f"{len(paths)} rule file(s) compile"
    message = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, message[0] if message else f"yara exited with code {proc.returncode}"


def verify_rules(directory: Path, *, binary: str | None = None, timeout: float = 120.0) -> tuple[bool | None, str]:
    """Compile the staged rules with the local yara binary.

    Returns (ok, note); ok is None when no yara binary is available, because
    then there is nothing to check with.
    """
    executable = binary or shutil.which("yara")
    if not executable:
        return None, "the yara binary is not installed, so the pack was not compile-checked"
    files = [str(path) for path in rule_files(directory)]
    if not files:
        return False, "the pack contains no rule files"
    return _compile(files, executable, timeout)


def verify_and_prune(directory: Path, *, timeout: float = 120.0) -> tuple[bool | None, str, list[str]]:
    """Compile the pack, and drop the files that cannot compile on their own.

    YARA aborts the whole run when one rule file does not compile, so a pack
    that is installed unchecked can silently switch the engine off.  When the
    set as a whole fails, the files that fail alone are moved out; if the rest
    then compiles, that rest is what gets installed.
    """
    executable = shutil.which("yara")
    if not executable:
        return None, "the yara binary is not installed, so the pack was not compile-checked", []
    files = rule_files(directory)
    if not files:
        return False, "the pack contains no rule files", []
    ok, note = _compile([str(path) for path in files], executable, timeout)
    if ok is not False:
        return ok, note, []

    dropped: list[str] = []
    for path in files:
        single_ok, _single_note = _compile([str(path)], executable, timeout)
        if single_ok is False:
            dropped.append(str(path.relative_to(directory)))
    if not dropped:
        return False, note, []
    for name in dropped:
        (directory / name).unlink(missing_ok=True)
    kept = rule_files(directory)
    ok, note = _compile([str(path) for path in kept], executable, timeout)
    if ok is True:
        return True, f"{len(kept)} rule file(s) compile; dropped {len(dropped)} that do not", dropped
    return False, note, dropped


# ---------------------------------------------------------------------------
# the update itself
# ---------------------------------------------------------------------------


def update(
    *,
    url: str = DEFAULT_URL,
    force: bool = False,
    verify: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> UpdateReport:
    """Fetch the pack, verify it and install it.  Never raises."""
    emit = on_progress or (lambda line: None)
    target = pack_dir()
    report = UpdateReport(directory=str(target), url=url)

    try:
        emit(f"fetching {url}")
        blob = fetch(url)
    except PackError as exc:
        report.error = str(exc)
        return report
    report.sha256 = hashlib.sha256(blob).hexdigest()

    try:
        files = extract_rules(blob)
    except PackError as exc:
        report.error = str(exc)
        return report
    if not files:
        report.error = "the download contains no .yar or .yara files"
        return report

    if target.is_dir():
        installed = {
            str(path.relative_to(target)): path.read_bytes() for path in rule_files(target)
        }
    else:
        installed = {}

    emit(f"unpacking {len(files)} rule file(s)")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".dcs-yara-", dir=str(target.parent)))
    try:
        for name, data in files.items():
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        if verify:
            ok, note, dropped = verify_and_prune(staging)
            report.verified = ok
            report.verify_note = note
            report.dropped_files = sorted(dropped)
            emit(note)
            if ok is False:
                report.error = f"the pack does not compile and was not installed: {note}"
                return report
        # The pack that would be installed, after pruning: everything the
        # diff and the manifest describe comes from here, so a second run with
        # the same download really is "unchanged".
        staged = {
            str(path.relative_to(staging)): path.read_bytes() for path in rule_files(staging)
        }
        fresh = rule_names(staged)
        known = rule_names(installed)
        old_digests = _digest(installed)
        new_digests = _digest(staged)
        report.total_rules = len(fresh)
        report.added_files = sorted(set(new_digests) - set(old_digests))
        report.removed_files = sorted(set(old_digests) - set(new_digests))
        report.changed_files = sorted(
            name
            for name in set(old_digests) & set(new_digests)
            if old_digests[name] != new_digests[name]
        )
        report.added_rules = sorted(fresh - known)
        report.removed_rules = sorted(known - fresh)

        if target.is_dir() and not force and old_digests == new_digests:
            report.ok = True
            report.unchanged = True
            report.verify_note = note if verify else "already up to date"
            emit(report.summary())
            return report

        backup = target.with_suffix(".old")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        if target.exists():
            target.rename(backup)
        staging.rename(target)
        staging = None  # type: ignore[assignment]
        shutil.rmtree(backup, ignore_errors=True)
    except OSError as exc:
        report.error = f"could not install the pack: {exc}"
        return report
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    manifest = {
        "url": url,
        "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sha256": report.sha256,
        "files": new_digests,
        "rules": sorted(fresh),
    }
    try:
        (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    except OSError as exc:  # the pack is there; only the manifest is missing
        log.warning("cannot write the rule pack manifest: %s", exc)

    report.ok = True
    emit(report.summary())
    return report
    try:
        (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    except OSError as exc:  # the pack is there; only the manifest is missing
        log.warning("cannot write the rule pack manifest: %s", exc)

    report.ok = True
    emit(report.summary())
    return report


def status() -> dict[str, Any]:
    """What is installed right now, without touching the network."""
    directory = pack_dir()
    manifest = read_manifest(directory)
    files = rule_files(directory)
    return {
        "directory": str(directory),
        "installed": bool(files),
        "files": len(files),
        "rules": len(manifest.get("rules") or []) or _count_rules(files),
        "fetched": manifest.get("fetched", ""),
        "url": manifest.get("url", ""),
        "sha256": manifest.get("sha256", ""),
    }


def _count_rules(files: list[Path]) -> int:
    total = 0
    for path in files:
        try:
            total += len(_RULE.findall(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return total
