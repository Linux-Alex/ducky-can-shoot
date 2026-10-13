"""Tests for the small helpers in dcs.core.util.

Everything here is filesystem local: trees are built under tmp_path, no
network or device is touched, and the only subprocesses are the current
interpreter itself.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Callable

import pytest

from dcs.core.util import (
    atomic_write_text,
    human_bytes,
    human_duration,
    human_rate,
    matches_any,
    percent,
    read_text_best_effort,
    sanitize_filename,
    sha256_file,
    tool_version,
    unique_path,
    walk_files,
    which,
)

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def rel_paths(root: Path | str, **kwargs: object) -> list[str]:
    """Sorted relative paths yielded by walk_files for *root*."""
    return sorted(entry.relative for entry in walk_files(root, **kwargs))


def error_collector() -> tuple[list[tuple[str, OSError]], Callable[[str, OSError], None]]:
    """Return an (errors, callback) pair for walk_files(on_error=...)."""
    errors: list[tuple[str, OSError]] = []

    def collect(path: str, exc: OSError) -> None:
        errors.append((path, exc))

    return errors, collect


# ---------------------------------------------------------------------------
# human_bytes
# ---------------------------------------------------------------------------


def test_human_bytes_exact_values() -> None:
    assert human_bytes(0) == "0 B"
    assert human_bytes(512) == "512 B"
    assert human_bytes(1023) == "1023 B"
    assert human_bytes(1024) == "1.0 KiB"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(1048576) == "1.0 MiB"
    assert human_bytes(1024**3) == "1.0 GiB"
    assert human_bytes(3 * 1024**3 + 512 * 1024**2) == "3.5 GiB"


def test_human_bytes_negative_values_keep_a_minus_prefix() -> None:
    assert human_bytes(-512) == "-512 B"
    assert human_bytes(-1536) == "-1.5 KiB"
    assert human_bytes(-1024**3) == "-1.0 GiB"


def test_human_bytes_none_is_a_dash() -> None:
    assert human_bytes(None) == "-"


def test_human_bytes_decimals_parameter() -> None:
    assert human_bytes(1536, decimals=3) == "1.500 KiB"
    assert human_bytes(1536, decimals=0) == "2 KiB"
    assert human_bytes(1280, decimals=0) == "1 KiB"
    assert human_bytes(512, decimals=2) == "512 B"  # bytes are never decimal


# ---------------------------------------------------------------------------
# human_duration
# ---------------------------------------------------------------------------


def test_human_duration_exact_values() -> None:
    assert human_duration(0) == "00:00"
    assert human_duration(59) == "00:59"
    assert human_duration(60) == "01:00"
    assert human_duration(3599) == "59:59"
    assert human_duration(3600) == "1:00:00"
    assert human_duration(3661) == "1:01:01"


def test_human_duration_large_value() -> None:
    assert human_duration(5 * 3600 + 61) == "5:01:01"
    assert human_duration(100 * 3600) == "100:00:00"


def test_human_duration_unknown_or_invalid() -> None:
    assert human_duration(None) == "--:--"
    assert human_duration(-1) == "--:--"
    assert human_duration(-0.5) == "--:--"
    assert human_duration(float("nan")) == "--:--"


def test_human_duration_truncates_fractional_seconds() -> None:
    assert human_duration(59.9) == "00:59"


# ---------------------------------------------------------------------------
# human_rate
# ---------------------------------------------------------------------------


def test_human_rate_unknown_values() -> None:
    assert human_rate(0) == "-"
    assert human_rate(0.0) == "-"
    assert human_rate(None) == "-"
    assert human_rate(-1024) == "-"


def test_human_rate_appends_per_second() -> None:
    assert human_rate(1) == "1 B/s"
    assert human_rate(512) == "512 B/s"
    assert human_rate(1536) == "1.5 KiB/s"
    assert human_rate(2 * 1024 * 1024) == "2.0 MiB/s"


# ---------------------------------------------------------------------------
# percent
# ---------------------------------------------------------------------------


def test_percent_unknown_total_is_zero() -> None:
    assert percent(10, 0) == 0.0
    assert percent(10, -5) == 0.0
    assert percent(0, 0) == 0.0


def test_percent_exact_fraction() -> None:
    assert percent(1, 4) == 0.25
    assert percent(1, 2) == 0.5
    assert percent(3, 4) == 0.75
    assert percent(0, 4) == 0.0


def test_percent_clamps_out_of_range_values() -> None:
    assert percent(5, 4) == 1.0
    assert percent(1000, 4) == 1.0
    assert percent(-1, 4) == 0.0


# ---------------------------------------------------------------------------
# matches_any
# ---------------------------------------------------------------------------


def test_matches_any_empty_pattern_list_never_matches() -> None:
    assert matches_any("a.txt", []) is False
    assert matches_any("a.txt", ()) is False


def test_matches_any_pattern_with_a_slash_matches_the_relative_path() -> None:
    assert matches_any("System Volume Information/IndexerVolumeGuid", ["System Volume Information/*"])
    assert matches_any("sub/dir/a.txt", ["sub/*"])
    # a slash pattern is anchored to the full relative path, not to the name
    assert matches_any("deep/sub/a.txt", ["sub/*"]) is False
    assert matches_any("System Volume Information", ["System Volume Information/*"]) is False


def test_matches_any_pattern_without_a_slash_matches_the_basename_anywhere() -> None:
    assert matches_any("a.log", ["*.log"])
    assert matches_any("sub/a.log", ["*.log"])
    assert matches_any("deep/sub/dir/a.log", ["*.log"])
    assert matches_any("notes.TMP", ["*.tmp"]) is False  # fnmatch is case sensitive


def test_matches_any_non_match() -> None:
    assert matches_any("sub/a.txt", ["*.log", "*.tmp", "other/*"]) is False


# ---------------------------------------------------------------------------
# walk_files
# ---------------------------------------------------------------------------


def test_walk_files_reports_relative_paths_and_sizes(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "deep").mkdir()
    (root / "readme.txt").write_bytes(b"hello")
    (root / "docs" / "notes.md").write_bytes(b"1234567")
    (root / "docs" / "deep" / "empty.bin").write_bytes(b"")

    entries = {entry.relative: entry.size for entry in walk_files(root)}

    assert entries == {"readme.txt": 5, "docs/notes.md": 7, "docs/deep/empty.bin": 0}
    for entry in walk_files(root):
        assert Path(entry.path) == root / entry.relative
        assert entry.path == str(root / entry.relative)
        assert entry.is_symlink is False
        assert entry.is_special is False
        assert Path(entry.path).stat().st_size == entry.size


def test_walk_files_includes_hidden_entries_by_default(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    (root / ".hidden_dir").mkdir(parents=True)
    (root / ".hidden_dir" / "inside.txt").write_bytes(b"x")
    (root / ".hidden_file").write_bytes(b"y")
    (root / "visible.txt").write_bytes(b"z")

    assert rel_paths(root) == [".hidden_dir/inside.txt", ".hidden_file", "visible.txt"]
    assert rel_paths(root, include_hidden=True) == [
        ".hidden_dir/inside.txt",
        ".hidden_file",
        "visible.txt",
    ]


def test_walk_files_excludes_hidden_files_and_hidden_directories(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    (root / ".hidden_dir").mkdir(parents=True)
    (root / ".hidden_dir" / "inside.txt").write_bytes(b"x")
    (root / ".hidden_file").write_bytes(b"y")
    (root / "visible.txt").write_bytes(b"z")

    assert rel_paths(root, include_hidden=False) == ["visible.txt"]


def test_walk_files_exclude_globs_match_any_depth_and_full_paths(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    (root / "System Volume Information").mkdir(parents=True)
    (root / "System Volume Information" / "IndexerVolumeGuid").write_bytes(b"guid")
    (root / "sub").mkdir()
    (root / "a.tmp").write_bytes(b"a")
    (root / "sub" / "b.tmp").write_bytes(b"b")
    (root / "sub" / "keep.txt").write_bytes(b"c")

    assert rel_paths(root, exclude_globs=["*.tmp"]) == [
        "System Volume Information/IndexerVolumeGuid",
        "sub/keep.txt",
    ]
    assert rel_paths(root, exclude_globs=["System Volume Information/*"]) == [
        "a.tmp",
        "sub/b.tmp",
        "sub/keep.txt",
    ]
    assert rel_paths(root, exclude_globs=["*.tmp", "System Volume Information/*"]) == ["sub/keep.txt"]
    assert rel_paths(root, exclude_globs=[]) == [
        "System Volume Information/IndexerVolumeGuid",
        "a.tmp",
        "sub/b.tmp",
        "sub/keep.txt",
    ]


def test_walk_files_never_yields_symlinks_by_default(tmp_path: Path) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    target = outside_dir / "real.txt"
    target.write_bytes(b"outside the root")
    root = tmp_path / "usb"
    root.mkdir()
    (root / "inside.txt").write_bytes(b"inside")
    (root / "link.txt").symlink_to(target)
    (dirname_link := root / "dirlink").symlink_to(outside_dir)

    assert dirname_link.is_symlink()
    assert rel_paths(root) == ["inside.txt"]


def test_walk_files_follow_symlinks_yields_the_link_but_not_the_target_path(tmp_path: Path) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    target = outside_dir / "real.txt"
    target.write_bytes(b"outside the root")
    root = tmp_path / "usb"
    root.mkdir()
    (root / "link.txt").symlink_to(target)

    entries = {entry.relative: entry for entry in walk_files(root, follow_symlinks=True)}

    assert list(entries) == ["link.txt"]
    entry = entries["link.txt"]
    assert entry.is_symlink is True
    # the link itself is stat()ed, so the size is the length of the target path
    assert entry.size == len(os.fsencode(str(target)))
    assert Path(entry.path) == root / "link.txt"
    # the real file outside the root is never reported under its own path
    assert all(entry.relative != "outside/real.txt" for entry in walk_files(root, follow_symlinks=True))


def test_walk_files_does_not_check_the_root_entry_for_being_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.txt").write_bytes(b"a")
    link = tmp_path / "link"
    link.symlink_to(real)

    # only the entries inside the root are tested for being links
    assert rel_paths(link) == ["a.txt"]


def test_walk_files_max_files_caps_the_yielded_entries(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    root.mkdir()
    for index in range(5):
        (root / f"file{index}.dat").write_bytes(b"x")

    assert len(list(walk_files(root, max_files=2))) == 2
    assert len(list(walk_files(root, max_files=1))) == 1
    assert len(list(walk_files(root, max_files=0))) == 5
    assert len(list(walk_files(root, max_files=99))) == 5


@pytest.mark.skipif(IS_ROOT, reason="root ignores directory permissions")
def test_walk_files_reports_an_unreadable_directory(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    (root / "locked").mkdir(parents=True)
    (root / "locked" / "inside.txt").write_bytes(b"hidden")
    (root / "open.txt").write_bytes(b"open")
    errors, on_error = error_collector()

    (root / "locked").chmod(0o000)
    try:
        found = list(walk_files(root, on_error=on_error))
    finally:
        (root / "locked").chmod(0o755)

    assert [entry.relative for entry in found] == ["open.txt"]
    assert [path for path, _ in errors] == [str(root / "locked")]
    assert isinstance(errors[0][1], PermissionError)


def test_walk_files_missing_root_reports_an_error_and_yields_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "not-here"
    errors, on_error = error_collector()

    assert list(walk_files(missing, on_error=on_error)) == []
    assert len(errors) == 1
    assert errors[0][0] == str(missing)
    assert isinstance(errors[0][1], FileNotFoundError)


def test_walk_files_without_on_error_swallows_errors(tmp_path: Path) -> None:
    assert list(walk_files(tmp_path / "not-here")) == []


# ---------------------------------------------------------------------------
# sha256_file
# ---------------------------------------------------------------------------


def test_sha256_file_hashes_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "sample.bin"
    data = b"ducky can shoot" * 1000
    path.write_bytes(data)

    assert sha256_file(str(path)) == hashlib.sha256(data).hexdigest()
    assert sha256_file(str(path), chunk_size=7) == hashlib.sha256(data).hexdigest()
    assert sha256_file(str(path), chunk_size=1) == hashlib.sha256(data).hexdigest()


def test_sha256_file_limit_hashes_only_the_first_bytes(tmp_path: Path) -> None:
    path = tmp_path / "sample.bin"
    data = bytes(range(256)) * 4
    path.write_bytes(data)

    for limit in (0, 1, 16, 255):
        assert sha256_file(str(path), limit=limit) == hashlib.sha256(data[:limit]).hexdigest()
    # a limit larger than the file still hashes the whole file
    assert sha256_file(str(path), limit=len(data) + 10) == hashlib.sha256(data).hexdigest()


def test_sha256_file_chunk_size_does_not_change_the_digest_with_limit(tmp_path: Path) -> None:
    path = tmp_path / "sample.bin"
    data = b"abcdefghij" * 100
    path.write_bytes(data)

    expected = hashlib.sha256(data[:250]).hexdigest()
    assert sha256_file(str(path), limit=250, chunk_size=3) == expected
    assert sha256_file(str(path), limit=250, chunk_size=4096) == expected


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------


def test_sanitize_filename_replaces_path_separators_and_control_characters() -> None:
    assert sanitize_filename("evil/name.txt") == "evil_name.txt"
    assert sanitize_filename("evil\\name.txt") == "evil_name.txt"
    # separators become underscores first, then the leading dots are stripped
    assert sanitize_filename("..\\..\\etc\\passwd") == "_.._etc_passwd"
    assert sanitize_filename("tab\there") == "tab_here"
    assert sanitize_filename("line\nbreak") == "line_break"
    assert sanitize_filename("nul\x00byte") == "nul_byte"


def test_sanitize_filename_replaces_windows_and_quoting_characters() -> None:
    assert sanitize_filename('say "hi".txt') == "say _hi_.txt"
    assert sanitize_filename("a:b*c?d<e>f|g") == "a_b_c_d_e_f_g"
    assert sanitize_filename("colon:name") == "colon_name"


def test_sanitize_filename_strips_leading_and_trailing_dots_and_spaces() -> None:
    assert sanitize_filename("  ..trimmed..  ") == "trimmed"
    assert sanitize_filename(".hidden") == "hidden"
    assert sanitize_filename("name. ") == "name"
    assert sanitize_filename(" . . name . . ") == "name"


def test_sanitize_filename_falls_back_when_nothing_is_left() -> None:
    assert sanitize_filename("") == "unnamed"
    assert sanitize_filename("   ") == "unnamed"
    assert sanitize_filename("..") == "unnamed"
    assert sanitize_filename("", fallback="report") == "report"
    assert sanitize_filename("/", fallback="report") == "_"


def test_sanitize_filename_truncates_to_max_length() -> None:
    assert sanitize_filename("abcdefghij", max_length=4) == "abcd"
    assert sanitize_filename("a" * 200) == "a" * 120
    assert sanitize_filename("a" * 200, max_length=10) == "a" * 10


def test_sanitize_filename_keeps_a_realistic_name_unchanged() -> None:
    assert sanitize_filename("holiday photo 2026-01.jpg") == "holiday photo 2026-01.jpg"
    assert sanitize_filename("invoice_2026.doc") == "invoice_2026.doc"


# ---------------------------------------------------------------------------
# unique_path
# ---------------------------------------------------------------------------


def test_unique_path_returns_the_same_path_when_free(tmp_path: Path) -> None:
    path = tmp_path / "report.zip"

    assert unique_path(path) == str(path)
    assert unique_path(str(path)) == str(path)


def test_unique_path_numbers_the_name_when_it_exists(tmp_path: Path) -> None:
    path = tmp_path / "report.zip"
    path.write_bytes(b"first")

    assert unique_path(path) == str(tmp_path / "report-1.zip")
    (tmp_path / "report-1.zip").write_bytes(b"second")
    assert unique_path(path) == str(tmp_path / "report-2.zip")
    (tmp_path / "report-2.zip").write_bytes(b"third")
    assert unique_path(path) == str(tmp_path / "report-3.zip")


def test_unique_path_handles_names_without_a_suffix(tmp_path: Path) -> None:
    path = tmp_path / "archive"
    path.write_bytes(b"x")

    assert unique_path(path) == str(tmp_path / "archive-1")


# ---------------------------------------------------------------------------
# atomic_write_text
# ---------------------------------------------------------------------------


def test_atomic_write_text_writes_the_content(tmp_path: Path) -> None:
    path = tmp_path / "out" / "result.txt"

    assert atomic_write_text(path, "hello ducky") == str(path)
    assert path.read_text(encoding="utf-8") == "hello ducky"
    assert path.read_bytes() == b"hello ducky"


def test_atomic_write_text_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "c" / "deep.txt"

    atomic_write_text(path, "deep")

    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "deep"


def test_atomic_write_text_honours_the_mode(tmp_path: Path) -> None:
    private = tmp_path / "secret.toml"
    public = tmp_path / "report.html"

    atomic_write_text(private, "pw", mode=0o600)
    atomic_write_text(public, "html")

    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(public.stat().st_mode) == 0o644


def test_atomic_write_text_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "result.txt"
    path.write_text("old", encoding="utf-8")

    atomic_write_text(path, "new")

    assert path.read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "result.txt.tmp").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["result.txt"]


def test_atomic_write_text_overwrites_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "result.txt"
    path.write_text("a" * 100, encoding="utf-8")

    atomic_write_text(path, "short")

    assert path.read_text(encoding="utf-8") == "short"


# ---------------------------------------------------------------------------
# read_text_best_effort
# ---------------------------------------------------------------------------


def test_read_text_best_effort_reads_utf8(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("Gruezi, ducky!\n", encoding="utf-8")

    assert read_text_best_effort(str(path)) == "Gruezi, ducky!\n"


def test_read_text_best_effort_replaces_invalid_bytes(tmp_path: Path) -> None:
    path = tmp_path / "latin1.txt"
    path.write_bytes(b"caf\xe9 latte")

    assert read_text_best_effort(str(path)) == "caf\ufffd latte"


def test_read_text_best_effort_returns_empty_for_missing_files_and_directories(tmp_path: Path) -> None:
    directory = tmp_path / "adir"
    directory.mkdir()

    assert read_text_best_effort(str(tmp_path / "missing.txt")) == ""
    assert read_text_best_effort(str(directory)) == ""


def test_read_text_best_effort_honours_limit(tmp_path: Path) -> None:
    path = tmp_path / "long.txt"
    path.write_bytes(b"0123456789")

    assert read_text_best_effort(str(path), limit=4) == "0123"
    assert read_text_best_effort(str(path), limit=0) == ""
    assert read_text_best_effort(str(path), limit=100) == "0123456789"


# ---------------------------------------------------------------------------
# which / tool_version
# ---------------------------------------------------------------------------


def test_which_finds_the_running_interpreter() -> None:
    found = which(sys.executable)

    assert found is not None
    assert Path(found) == Path(sys.executable)


def test_which_returns_none_for_a_bogus_binary() -> None:
    assert which("dcs-no-such-binary-4f3a19") is None


def test_which_finds_the_fake_clamav_on_the_patched_path(fake_clamav: Path) -> None:
    assert which("clamscan") == str(fake_clamav / "clamscan")


def test_tool_version_reads_the_first_line_of_the_output() -> None:
    version = tool_version(sys.executable, ("--version",))

    assert version is not None
    assert version.startswith("Python")
    assert "\n" not in version


def test_tool_version_returns_none_for_a_missing_binary() -> None:
    assert tool_version("dcs-no-such-binary-4f3a19", ("--version",)) is None


def test_tool_version_returns_none_when_there_is_no_output() -> None:
    assert tool_version(sys.executable, ("-c", "pass")) is None


def test_tool_version_returns_none_when_the_command_fails_without_output(tmp_path: Path) -> None:
    assert tool_version(sys.executable, ("-c", "raise SystemExit(3)")) is None


def test_tool_version_reads_stderr_when_stdout_is_empty() -> None:
    script = "import sys; sys.stderr.write('fake 1.2.3\\n')"

    assert tool_version(sys.executable, ("-c", script)) == "fake 1.2.3"


def test_tool_version_uses_the_first_line_only() -> None:
    script = "print('first line'); print('second line')"

    assert tool_version(sys.executable, ("-c", script)) == "first line"
