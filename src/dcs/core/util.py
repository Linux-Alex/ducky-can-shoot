"""Small, dependency-free helpers used across the core."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(value: float | int | None, decimals: int = 1) -> str:
    """Return a compact human readable size such as '3.4 GiB'."""
    if value is None:
        return "-"
    size = float(value)
    negative = size < 0
    size = abs(size)
    index = 0
    while size >= 1024.0 and index < len(_UNITS) - 1:
        size /= 1024.0
        index += 1
    text = (
        f"{int(size)} {_UNITS[index]}"
        if index == 0
        else f"{size:.{decimals}f} {_UNITS[index]}"
    )
    return "-" + text if negative else text


def human_duration(seconds: float | None) -> str:
    """Return 'HH:MM:SS' (or 'MM:SS') for a duration."""
    if seconds is None or seconds != seconds or seconds < 0:  # NaN safe
        return "--:--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def human_rate(bytes_per_second: float | None) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return "-"
    return f"{human_bytes(bytes_per_second)}/s"


def percent(done: float, total: float) -> float:
    """Fraction in the 0..1 range, tolerant of unknown totals."""
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, done / total))


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def timestamp_slug() -> str:
    """Filename-safe local timestamp, e.g. 20260113-191530."""
    return time.strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FileEntry:
    """One regular file discovered under a scan root."""

    path: str
    size: int
    relative: str
    is_symlink: bool = False
    is_special: bool = False  # device, fifo, socket
    mtime: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def suffix(self) -> str:
        return os.path.splitext(self.path)[1].lower()

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def matches_any(relative_path: str, patterns: Sequence[str]) -> bool:
    """fnmatch a relative path against a list of patterns.

    A pattern without a slash matches against the basename as well, so
    '*.log' excludes logs in every directory.
    """
    if not patterns:
        return False
    name = os.path.basename(relative_path)
    for pattern in patterns:
        if fnmatch.fnmatch(relative_path, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatch(name, pattern):
            return True
    return False


def walk_files(
    root: str | os.PathLike[str],
    *,
    follow_symlinks: bool = False,
    include_hidden: bool = True,
    exclude_globs: Sequence[str] = (),
    max_files: int = 0,
    on_error: Callable[[str, OSError], None] | None = None,
) -> Iterator[FileEntry]:
    """Yield every file under *root* with its size, never following links.

    The generator is intentionally tolerant: unreadable directories and
    vanished files are reported through *on_error* and skipped, because a
    damaged USB stick is exactly the case this tool has to survive.
    """
    root = os.path.abspath(os.fspath(root))
    count = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:  # permission denied, I/O error
            if on_error is not None:
                on_error(current, exc)
            continue
        for entry in entries:
            try:
                is_link = entry.is_symlink()
                if is_link and not follow_symlinks:
                    continue
                if entry.is_dir(follow_symlinks=follow_symlinks):
                    rel_dir = os.path.relpath(entry.path, root)
                    if not include_hidden and os.path.basename(entry.path).startswith("."):
                        continue
                    if matches_any(rel_dir, exclude_globs):
                        continue
                    stack.append(entry.path)
                    continue
                rel = os.path.relpath(entry.path, root)
                if not include_hidden and os.path.basename(entry.path).startswith("."):
                    continue
                if matches_any(rel, exclude_globs):
                    continue
                stat = entry.stat(follow_symlinks=False)
                special = not entry.is_file(follow_symlinks=follow_symlinks)
                yield FileEntry(
                    path=entry.path,
                    size=int(stat.st_size),
                    relative=rel,
                    is_symlink=is_link,
                    is_special=special,
                    mtime=float(stat.st_mtime),
                )
                count += 1
                if max_files and count >= max_files:
                    return
            except OSError as exc:
                if on_error is not None:
                    on_error(entry.path, exc)
                continue


def sha256_file(path: str, *, chunk_size: int = 1 << 20, limit: int | None = None) -> str:
    """Hash a file, optionally only its first *limit* bytes."""
    digest = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as handle:
        while True:
            size = chunk_size if remaining is None else min(chunk_size, remaining)
            if size <= 0:
                break
            block = handle.read(size)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


def free_bytes(path: str | os.PathLike[str]) -> int:
    try:
        return int(shutil.disk_usage(os.fspath(path)).free)
    except OSError:
        return 0


def sanitize_filename(name: str, *, fallback: str = "unnamed", max_length: int = 120) -> str:
    """Strip path separators and control characters from a file name."""
    cleaned = "".join(ch if ch.isprintable() and ch not in '/\\:*?"<>|' else "_" for ch in name)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length]


def unique_path(path: str | os.PathLike[str]) -> str:
    """Return *path* or a numbered variant that does not exist yet."""
    candidate = Path(path)
    if not candidate.exists():
        return str(candidate)
    stem, suffix, parent = candidate.stem, candidate.suffix, candidate.parent
    index = 1
    while True:
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return str(candidate)
        index += 1


def atomic_write_text(path: str | os.PathLike[str], text: str, *, mode: int = 0o644) -> str:
    """Write text to a temp file and rename it into place."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, target)
    return str(target)


def read_text_best_effort(path: str, *, limit: int = 65536, encoding: str = "utf-8") -> str:
    """Read at most *limit* bytes of a text file, never raising."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit)
    except OSError:
        return ""
    return raw.decode(encoding, errors="replace")


# ---------------------------------------------------------------------------
# subprocesses
# ---------------------------------------------------------------------------


def which(binary: str) -> str | None:
    return shutil.which(binary)


def tool_version(binary: str, args: Sequence[str] = ("--version",), timeout: float = 5.0) -> str | None:
    """Return the first line of a tool's version output, or None."""
    if which(binary) is None:
        return None
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    return output.splitlines()[0] if output else None


def popen_text(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    """Start a child process with a stable C locale and line buffering."""
    child_env = dict(os.environ)
    child_env.setdefault("LC_ALL", "C")
    child_env.setdefault("LANG", "C")
    if env:
        child_env.update(env)
    return subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def read_lines(stream: IO[str]) -> Iterator[str]:
    for line in stream:
        yield line.rstrip("\n")


def close_quietly(stream: IO[str] | None) -> None:
    if stream is None:
        return
    with contextlib.suppress(OSError):
        stream.close()
