"""Archive creation: turn the triaged medium into one transferable image.

Design notes
------------
* The external zip/7z tools are preferred because they stream (constant memory)
  and report per-file progress, which is exactly what the live view needs.
* Volume splitting is delegated to those tools too: zip produces .z01/.z02/.zip
  sets, 7z produces .001/.002 files, and both are what the recipient expects.
* A pure Python zipfile fallback keeps the tool usable on a minimal system; it
  cannot split, and says so instead of silently producing the wrong thing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dcs.core.config import ArchiveConfig
from dcs.core.events import (
    EventSink,
    NullSink,
    ProgressMeter,
    Stage,
    StageProgress,
    StageState,
)
from dcs.core.log import get_logger
from dcs.core.util import free_bytes, human_bytes, sanitize_filename, timestamp_slug, walk_files

log = get_logger("archiver")

#: "  adding: path/to/file (deflated 42%)" (zip) and "+ path" (7z)
_PROGRESS_LINE = re.compile(
    r"^\s*(?:(?P<action>adding|updating|creating):\s(?P<zippath>.+?)(?:\s\((?P<how>.*)\))?"
    r"|(?P<sevenzaction>[+\-U])\s(?P<sevenzpath>.+))$"
)


def _progress_path(match: re.Match[str]) -> tuple[str, str]:
    """Return (action, relative path) for either tool's progress line."""
    if match.group("zippath") is not None:
        return (match.group("action") or "adding"), match.group("zippath").strip()
    return (match.group("sevenzaction") or "+"), match.group("sevenzpath").strip()

#: how many relpath -> size entries we keep for byte accurate progress
_SIZE_MAP_LIMIT = 200_000


@dataclass(slots=True)
class ArchivePart:
    path: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sizeText": human_bytes(self.size)}


@dataclass
class ArchiveResult:
    """Outcome of the archive stage."""

    ok: bool = False
    parts: list[ArchivePart] = field(default_factory=list)
    source_bytes: int = 0
    source_files: int = 0
    duration: float = 0.0
    tool: str = ""
    command: list[str] = field(default_factory=list)
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def archive_bytes(self) -> int:
        return sum(part.size for part in self.parts)

    @property
    def primary_path(self) -> str:
        return self.parts[0].path if self.parts else ""

    @property
    def split(self) -> bool:
        return len(self.parts) > 1

    @property
    def ratio(self) -> float:
        if not self.source_bytes:
            return 0.0
        return self.archive_bytes / self.source_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "parts": [part.to_dict() for part in self.parts],
            "partCount": len(self.parts),
            "primaryPath": self.primary_path,
            "sourceBytes": self.source_bytes,
            "sourceBytesText": human_bytes(self.source_bytes),
            "sourceFiles": self.source_files,
            "archiveBytes": self.archive_bytes,
            "archiveBytesText": human_bytes(self.archive_bytes),
            "duration": self.duration,
            "tool": self.tool,
            "command": list(self.command),
            "error": self.error,
            "warnings": list(self.warnings),
        }


def archive_name(label: str, *, fmt: str = "zip", when: float | None = None) -> str:
    stem = sanitize_filename(label or "usb", fallback="usb")
    suffix = "7z" if fmt == "7z" else "zip"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when)) if when else timestamp_slug()
    return f"DCS_{stem}_{stamp}.{suffix}"


def plan_source(root: str) -> tuple[int, int, dict[str, int]]:
    """Count files and bytes, and remember sizes for byte accurate progress."""
    total_files = 0
    total_bytes = 0
    sizes: dict[str, int] = {}
    keep_sizes = True
    for entry in walk_files(root, include_hidden=True):
        total_files += 1
        total_bytes += entry.size
        if keep_sizes:
            if len(sizes) < _SIZE_MAP_LIMIT:
                sizes[entry.relative] = entry.size
            else:
                keep_sizes = False
                sizes.clear()
    return total_files, total_bytes, sizes


class Archiver:
    """Creates the archive image, with progress events."""

    def __init__(self, config: ArchiveConfig) -> None:
        self.config = config

    # -- tooling ----------------------------------------------------------
    def tool_for(self, fmt: str) -> str:
        if fmt == "7z":
            return shutil.which("7z") or shutil.which("7za") or ""
        return shutil.which("zip") or ""

    def describe_tools(self) -> dict[str, str]:
        return {
            "zip": shutil.which("zip") or "",
            "7z": shutil.which("7z") or shutil.which("7za") or "",
            "python": "zipfile (fallback, no splitting)",
        }

    # -- capacity ---------------------------------------------------------
    def required_free_bytes(self, source_bytes: int) -> int:
        return int(source_bytes * max(1.0, self.config.min_free_space_ratio))

    def check_capacity(self, dest_dir: str, source_bytes: int) -> tuple[bool, str]:
        required = self.required_free_bytes(source_bytes)
        available = free_bytes(dest_dir)
        if available < required:
            return False, (
                f"not enough free space in {dest_dir}: the image needs about "
                f"{human_bytes(required)} but only {human_bytes(available)} is free"
            )
        return True, ""

    # -- the archive itself ----------------------------------------------
    def create(
        self,
        root: str,
        dest_dir: str,
        *,
        name: str,
        sink: EventSink | None = None,
        cancel: threading.Event | None = None,
        total_files: int = 0,
        total_bytes: int = 0,
        size_map: dict[str, int] | None = None,
    ) -> ArchiveResult:
        sink = sink or NullSink()
        cancel = cancel or threading.Event()
        result = ArchiveResult(tool="python-zipfile")
        fmt = self.config.format if self.config.format in ("zip", "7z") else "zip"

        os.makedirs(dest_dir, exist_ok=True)
        result.source_files = total_files
        result.source_bytes = total_bytes

        # Resolve the tool *before* deciding on the file name: when 7z is
        # configured but missing the job falls back to zip, and both the tool
        # and the extension have to follow, otherwise DCS would write a zip
        # archive called .7z.
        tool = self.tool_for(fmt)
        if not tool and fmt == "7z":
            result.warnings.append("7z is not installed: falling back to a zip archive")
            fmt = "zip"
            name = os.path.splitext(name)[0] + ".zip"
            tool = self.tool_for("zip")

        target = os.path.join(dest_dir, name)
        ok, problem = self.check_capacity(dest_dir, total_bytes)
        if not ok:
            result.error = problem
            sink.on_stage(Stage.ARCHIVE, StageState.FAILED, problem)
            return result
        if problem:
            result.warnings.append(problem)

        meter = ProgressMeter()
        sink.on_stage(
            Stage.ARCHIVE,
            StageState.RUNNING,
            f"creating {os.path.basename(target)} from {total_files} files "
            f"({human_bytes(total_bytes)})",
        )

        if tool:
            result.tool = "7z" if fmt == "7z" else "zip"
            return self._run_tool(
                tool, fmt, root, target, sink, cancel, meter, result, total_files, total_bytes, size_map
            )
        if self.config.split_size_mb:
            result.warnings.append(
                "no zip or 7z binary is installed, so the archive cannot be split; "
                "producing a single file instead"
            )
        return self._run_python_zip(
            root, target, sink, cancel, meter, result, total_files, total_bytes, size_map
        )

    # -- external tools ---------------------------------------------------
    def _build_command(self, tool: str, fmt: str, target: str) -> list[str]:
        """Build the argv; the tool always walks the current directory itself.

        Letting zip/7z discover the files instead of feeding them a list keeps
        the command line short, avoids a second enumeration and lets both tools
        stream: memory stays flat no matter how large the medium is.
        """
        level = max(0, min(9, self.config.compression_level))
        if fmt == "7z":
            argv = [tool, "a", "-t7z", f"-mx={level}", "-y", "-bb1", target, "."]
            if self.config.split_size_mb:
                argv.insert(3, f"-v{self.config.split_size_mb}m")
        else:
            argv = [tool, "-r", f"-{level}", "-y"]
            if self.config.split_size_mb:
                argv += ["-s", f"{self.config.split_size_mb}m"]
            argv += [target, "."]
        return argv

    def _run_tool(
        self,
        tool: str,
        fmt: str,
        root: str,
        target: str,
        sink: EventSink,
        cancel: threading.Event,
        meter: ProgressMeter,
        result: ArchiveResult,
        total_files: int,
        total_bytes: int,
        size_map: dict[str, int] | None,
    ) -> ArchiveResult:
        started = time.monotonic()
        sizes: dict[str, int] = size_map if size_map else {}
        try:
            argv = self._build_command(tool, fmt, target)
            result.command = argv
            env = dict(os.environ)
            env["LC_ALL"] = "C"
            env["LANG"] = "C"
            proc = subprocess.Popen(
                argv,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
                bufsize=1,
                env=env,
            )
            processed_files = 0
            processed_bytes = 0
            assert proc.stdout is not None
            for raw in proc.stdout:
                if cancel.is_set():
                    proc.terminate()
                    break
                line = raw.rstrip()
                match = _PROGRESS_LINE.match(line)
                if not match:
                    if line and ("error" in line.lower() or "warning" in line.lower()):
                        result.warnings.append(line.strip())
                    continue
                action, relpath = _progress_path(match)
                if action == "creating":
                    continue
                processed_files += 1
                processed_bytes += sizes.get(relpath, 0)
                snapshot = meter.snapshot(
                    Stage.ARCHIVE,
                    processed=processed_files,
                    total=total_files,
                    bytes_processed=processed_bytes,
                    bytes_total=total_bytes,
                    current=relpath,
                    message=(
                        f"{processed_files} / {total_files} files · "
                        f"{human_bytes(processed_bytes)} of {human_bytes(total_bytes)}"
                    ),
                )
                if snapshot is not None:
                    sink.on_progress(snapshot)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
                proc.wait(timeout=10)
        except OSError as exc:
            result.error = f"cannot run {tool}: {exc}"
            sink.on_stage(Stage.ARCHIVE, StageState.FAILED, result.error)
            return result

        result.parts = self._collect_parts(target, fmt)
        result.duration = time.monotonic() - started

        if cancel.is_set():
            result.error = "cancelled"
            sink.on_stage(Stage.ARCHIVE, StageState.CANCELLED, "archive cancelled")
            return result

        returncode = proc.returncode
        if not result.parts:
            result.error = f"{os.path.basename(tool)} failed with code {returncode}"
            sink.on_stage(Stage.ARCHIVE, StageState.FAILED, result.error)
            return result
        if returncode not in (0, 1, 18):  # 1 = warnings, 18 = some files unreadable
            result.warnings.append(f"{os.path.basename(tool)} exited with code {returncode}")

        result.ok = True
        sink.on_progress(
            StageProgress(
                stage=Stage.ARCHIVE,
                state=StageState.DONE,
                processed=processed_files,
                total=total_files,
                bytes_processed=processed_bytes or total_bytes,
                bytes_total=total_bytes,
                message=self._summary(result),
                elapsed=result.duration,
                percent=1.0,
            )
        )
        sink.on_stage(Stage.ARCHIVE, StageState.DONE, self._summary(result))
        sink.on_log("info", self._summary(result))
        return result

    # -- python fallback --------------------------------------------------
    def _run_python_zip(
        self,
        root: str,
        target: str,
        sink: EventSink,
        cancel: threading.Event,
        meter: ProgressMeter,
        result: ArchiveResult,
        total_files: int,
        total_bytes: int,
        size_map: dict[str, int] | None,
    ) -> ArchiveResult:
        started = time.monotonic()
        sizes = size_map or {}
        processed_files = 0
        processed_bytes = 0
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                for entry in walk_files(root, include_hidden=True):
                    if cancel.is_set():
                        break
                    relpath = os.path.relpath(entry.path, root)
                    try:
                        archive.write(entry.path, arcname=relpath)
                    except OSError as exc:
                        result.warnings.append(f"{relpath}: {exc}")
                    processed_files += 1
                    processed_bytes += sizes.get(relpath, entry.size)
                    snapshot = meter.snapshot(
                        Stage.ARCHIVE,
                        processed=processed_files,
                        total=total_files,
                        bytes_processed=processed_bytes,
                        bytes_total=total_bytes,
                        current=relpath,
                        message=f"{processed_files} / {total_files} files",
                    )
                    if snapshot is not None:
                        sink.on_progress(snapshot)
        except (OSError, zipfile.BadZipFile) as exc:
            result.error = str(exc)
            sink.on_stage(Stage.ARCHIVE, StageState.FAILED, result.error)
            return result

        result.duration = time.monotonic() - started
        result.parts = self._collect_parts(target, "zip")
        result.ok = bool(result.parts)
        if not result.ok:
            result.error = result.error or "the fallback archiver produced no file"
            sink.on_stage(Stage.ARCHIVE, StageState.FAILED, result.error)
            return result
        sink.on_progress(
            StageProgress(
                stage=Stage.ARCHIVE,
                state=StageState.DONE,
                processed=processed_files,
                total=total_files,
                bytes_processed=processed_bytes,
                bytes_total=total_bytes,
                message=self._summary(result),
                elapsed=result.duration,
                percent=1.0,
            )
        )
        sink.on_stage(Stage.ARCHIVE, StageState.DONE, self._summary(result))
        sink.on_log("info", self._summary(result))
        return result

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _collect_parts(target: str, fmt: str) -> list[ArchivePart]:
        """Find every volume the tool produced, in upload order."""
        directory = os.path.dirname(target) or "."
        base = os.path.basename(target)
        stem = base.rsplit(".", 1)[0]
        parts: list[ArchivePart] = []
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return parts
        for name in names:
            full = os.path.join(directory, name)
            if not os.path.isfile(full):
                continue
            if fmt == "7z":
                # 7z volumes are named <archive>.7z.001, .002, ...
                tail = name[len(base) + 1 :]
                is_part = name == base or (name.startswith(base + ".") and tail.isdigit())
            else:
                tail = name[len(stem) + 2 :]
                is_part = name == base or (
                    name.startswith(stem + ".z") and len(tail) == 2 and tail.isdigit()
                )
            if not is_part:
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size <= 0:
                continue
            parts.append(ArchivePart(path=full, size=size))

        # zip volumes: .z01, .z02, ..., .zip (the .zip is the last volume and
        # carries the central directory); 7z volumes: .001, .002, ...
        def sort_key(part: ArchivePart) -> tuple[int, str]:
            name = os.path.basename(part.path)
            if name == base:
                return (1, name)
            if part.path.endswith(base):
                return (1, name)
            return (0, name)

        if fmt == "7z":
            parts.sort(key=lambda part: part.path)
        else:
            parts.sort(key=sort_key)
        return parts

    @staticmethod
    def _summary(result: ArchiveResult) -> str:
        if result.split:
            return (
                f"{len(result.parts)} volumes, {human_bytes(result.archive_bytes)} total "
                f"({result.ratio:.0%} of {human_bytes(result.source_bytes)})"
            )
        return (
            f"{os.path.basename(result.primary_path)}: {human_bytes(result.archive_bytes)} "
            f"from {human_bytes(result.source_bytes)} "
            f"({result.ratio:.0%})"
        )


def human_archive_summary(result: ArchiveResult) -> str:
    """One line summary used by the CLI."""
    if not result.ok:
        return f"archive failed: {result.error}"
    names = ", ".join(os.path.basename(part.path) for part in result.parts[:3])
    if len(result.parts) > 3:
        names += f", ... (+{len(result.parts) - 3})"
    return f"{names} - {human_bytes(result.archive_bytes)} from {human_bytes(result.source_bytes)}"


#: type of the callback the pipeline uses to surface archive progress
ProgressCallback = Callable[[StageProgress], None]
