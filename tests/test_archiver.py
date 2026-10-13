"""Tests for dcs.core.archiver.

The real zip binary (and 7z, when it is installed) is exercised here, because
the volume naming, the exact command line and the split behaviour are part of
the contract with whoever receives the image.  The python zipfile fallback is
covered by hiding both binaries from the module, and the capacity check by
replacing free_bytes.

A 7z format without 7z installed must downgrade to the zip binary *and* to a
.zip file name; test_zip_binary_is_used_after_downgrading_from_7z pins that.
"""

from __future__ import annotations

import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from dcs.core.archiver import (
    ArchivePart,
    Archiver,
    ArchiveResult,
    archive_name,
    human_archive_summary,
    plan_source,
)
from dcs.core.config import ArchiveConfig
from dcs.core.events import CollectingSink, Stage, StageState

#: 2026-02-03 04:05:06 local time, so time.localtime(WHEN) is stable
WHEN = time.mktime((2026, 2, 3, 4, 5, 6, 0, 0, -1))
STAMP = "20260203-040506"

HAS_ZIP = shutil.which("zip") is not None
HAS_7Z = shutil.which("7z") is not None

requires_zip = pytest.mark.skipif(not HAS_ZIP, reason="the zip binary is not installed")
requires_7z = pytest.mark.skipif(not HAS_7Z, reason="the 7z binary is not installed")


def make_tree(root: Path, files: dict[str, bytes | int]) -> Path:
    """Write *files* under *root*; an int value means that many random bytes."""
    for name, data in files.items():
        payload = os.urandom(data) if isinstance(data, int) else data
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


def create_archive(
    config: ArchiveConfig,
    root: Path,
    dest: Path,
    name: str,
    sink: CollectingSink | None = None,
) -> tuple[ArchiveResult, CollectingSink]:
    """Run Archiver.create with the file count and size map plan_source returns."""
    collector = sink if sink is not None else CollectingSink()
    total_files, total_bytes, size_map = plan_source(str(root))
    result = Archiver(config).create(
        str(root),
        str(dest),
        name=name,
        sink=collector,
        total_files=total_files,
        total_bytes=total_bytes,
        size_map=size_map,
    )
    return result, collector


@pytest.fixture
def archive_config() -> ArchiveConfig:
    """ArchiveConfig that compresses as fast as possible, to keep tests quick."""
    config = ArchiveConfig()
    config.compression_level = 1
    return config


# ---------------------------------------------------------------------------
# archive_name
# ---------------------------------------------------------------------------


def test_archive_name_uses_the_injected_timestamp() -> None:
    assert archive_name("holiday", when=WHEN) == f"DCS_holiday_{STAMP}.zip"


def test_archive_name_keeps_printable_spaces_but_strips_separators() -> None:
    # Spaces are printable and survive; path separators, drive letters and
    # control characters are replaced by an underscore.
    assert archive_name("My USB stick", when=WHEN) == f"DCS_My USB stick_{STAMP}.zip"
    assert archive_name("evil/../../etc", when=WHEN) == f"DCS_evil_.._.._etc_{STAMP}.zip"
    assert archive_name("usb\\name:1", when=WHEN) == f"DCS_usb_name_1_{STAMP}.zip"
    assert archive_name("usb\x07\x00stick", when=WHEN) == f"DCS_usb__stick_{STAMP}.zip"


def test_archive_name_falls_back_to_usb_for_an_empty_label() -> None:
    assert archive_name("", when=WHEN) == f"DCS_usb_{STAMP}.zip"
    assert archive_name("   ", when=WHEN) == f"DCS_usb_{STAMP}.zip"
    assert archive_name("..", when=WHEN) == f"DCS_usb_{STAMP}.zip"


def test_archive_name_extension_follows_the_format() -> None:
    assert archive_name("holiday", fmt="zip", when=WHEN) == f"DCS_holiday_{STAMP}.zip"
    assert archive_name("holiday", fmt="7z", when=WHEN) == f"DCS_holiday_{STAMP}.7z"
    # an unknown format is treated as zip
    assert archive_name("holiday", fmt="rar", when=WHEN) == f"DCS_holiday_{STAMP}.zip"


def test_archive_name_uses_the_current_clock_without_an_injected_time() -> None:
    assert re.fullmatch(r"DCS_holiday_\d{8}-\d{6}\.zip", archive_name("holiday"))


# ---------------------------------------------------------------------------
# plan_source
# ---------------------------------------------------------------------------


def test_plan_source_counts_hidden_files_too(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path / "usb",
        {
            "a.txt": b"12345",
            "sub/b.bin": b"x" * 10,
            ".hidden": b"ab",
            "sub/.dotfile": b"z",
        },
    )
    total_files, total_bytes, sizes = plan_source(str(root))
    assert total_files == 4
    assert total_bytes == 5 + 10 + 2 + 1
    assert sizes == {
        "a.txt": 5,
        "sub/b.bin": 10,
        ".hidden": 2,
        "sub/.dotfile": 1,
    }


def test_plan_source_of_an_empty_directory_is_all_zero(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert plan_source(str(empty)) == (0, 0, {})


# ---------------------------------------------------------------------------
# create with the real zip tool
# ---------------------------------------------------------------------------


@requires_zip
def test_create_with_the_real_zip_tool(tmp_path: Path, archive_config: ArchiveConfig) -> None:
    payloads: dict[str, Any] = {
        "readme.txt": b"holiday photos\n",
        "docs/notes.md": b"# notes\n",
        "photos/holiday.jpg": b"\xff\xd8\xff\xe0" + os.urandom(4092),
    }
    root = make_tree(tmp_path / "usb", payloads)
    dest = tmp_path / "images"
    name = f"DCS_holiday_{STAMP}.zip"

    result, sink = create_archive(archive_config, root, dest, name)

    assert result.ok is True
    assert result.error == ""
    assert result.tool == "zip"
    assert result.source_files == 3
    assert result.source_bytes == sum(len(payload) for payload in payloads.values())
    assert result.duration >= 0.0

    # the documented command line
    assert result.command == [shutil.which("zip") or "", "-r", "-1", "-y", str(dest / name), "."]
    assert "-r" in result.command
    assert "-1" in result.command

    # exactly one part, on disk, with the reported size
    assert result.split is False
    assert len(result.parts) == 1
    part = result.parts[0]
    assert part.path == str(dest / name)
    assert part.size == os.path.getsize(part.path) > 0
    assert result.primary_path == part.path
    assert result.archive_bytes == part.size

    with zipfile.ZipFile(part.path) as archive:
        assert archive.testzip() is None
        members = set(archive.namelist())
    assert set(payloads) <= members

    # progress events: archive RUNNING then DONE, and 100 percent reported
    assert [(stage, state) for stage, state, _ in sink.stages if stage is Stage.ARCHIVE] == [
        (Stage.ARCHIVE, StageState.RUNNING),
        (Stage.ARCHIVE, StageState.DONE),
    ]
    assert "3 files" in sink.stages[0][2]
    assert sink.max_percent[Stage.ARCHIVE] == 1.0
    done = [progress for progress in sink.progress if progress.state is StageState.DONE]
    assert done and done[-1].percent == 1.0
    assert done[-1].total == 3
    assert sink.logs and sink.logs[-1][0] == "info"


@requires_zip
def test_create_splits_zip_into_volumes(tmp_path: Path, archive_config: ArchiveConfig) -> None:
    archive_config.split_size_mb = 1
    payload = os.urandom(3 * 1024 * 1024)  # incompressible, so the split really happens
    root = make_tree(tmp_path / "usb", {"random.bin": payload})
    dest = tmp_path / "images"
    name = "DCS_holiday_split.zip"

    result, _sink = create_archive(archive_config, root, dest, name)

    assert result.ok is True
    assert result.error == ""
    assert result.tool == "zip"
    assert "-s" in result.command and "1m" in result.command
    assert result.split is True
    assert len(result.parts) > 1
    assert result.archive_bytes >= len(payload)

    names = [os.path.basename(part.path) for part in result.parts]
    # the zip tool writes .z01, .z02, ... and keeps the central directory in .zip
    assert names[:-1] == [f"DCS_holiday_split.z{index:02d}" for index in range(1, len(names))]
    assert names[-1] == name
    for part in result.parts:
        assert os.path.exists(part.path)
        assert part.size == os.path.getsize(part.path) > 0
    assert result.archive_bytes == sum(part.size for part in result.parts)

    # the last volume carries the end of central directory record, so it can be
    # listed on its own; note that the python zipfile module refuses to extract
    # a multi volume set, so the structure is what gets checked here
    last_volume = Path(result.parts[-1].path)
    assert b"PK\x05\x06" in last_volume.read_bytes()[-1024:]
    with zipfile.ZipFile(last_volume) as archive:
        assert archive.namelist() == ["random.bin"]

    # concatenating the volumes in the documented order keeps the same stream
    combined = tmp_path / "combined.zip"
    with combined.open("wb") as handle:
        for part in result.parts:
            handle.write(Path(part.path).read_bytes())
    assert combined.stat().st_size == result.archive_bytes
    with zipfile.ZipFile(combined) as archive:
        assert archive.namelist() == ["random.bin"]


@requires_7z
def test_create_splits_7z_into_numbered_volumes(
    tmp_path: Path, archive_config: ArchiveConfig
) -> None:
    archive_config.format = "7z"
    archive_config.split_size_mb = 1
    payload = os.urandom(3 * 1024 * 1024)
    root = make_tree(tmp_path / "usb", {"random.bin": payload})
    dest = tmp_path / "images"
    name = "DCS_holiday.7z"

    result, _sink = create_archive(archive_config, root, dest, name)

    assert result.ok is True, result.error
    assert result.tool == "7z"
    assert result.command[:4] == [shutil.which("7z") or "", "a", "-t7z", "-v1m"]
    assert "-mx=1" in result.command
    assert result.split is True

    names = [os.path.basename(part.path) for part in result.parts]
    assert len(names) > 1
    assert names == [f"DCS_holiday.7z.{index:03d}" for index in range(1, len(names) + 1)]
    assert sorted(names) == names
    assert not (dest / name).exists()  # 7z split means there is no plain .7z file
    for part in result.parts:
        assert os.path.exists(part.path)
        assert part.size == os.path.getsize(part.path) > 0


# ---------------------------------------------------------------------------
# the python zipfile fallback
# ---------------------------------------------------------------------------


def test_python_zipfile_fallback_when_the_zip_binary_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_config: ArchiveConfig
) -> None:
    monkeypatch.setattr("dcs.core.archiver.shutil.which", lambda name: None)
    payloads = {"readme.txt": b"hello\n", "docs/notes.md": b"# notes\n", "photos/pic.jpg": 2048}
    root = make_tree(tmp_path / "usb", payloads)
    dest = tmp_path / "images"

    result, sink = create_archive(archive_config, root, dest, "DCS_holiday.zip")

    assert result.ok is True
    assert result.tool == "python-zipfile"
    assert result.command == []
    assert len(result.parts) == 1
    assert result.parts[0].path == str(dest / "DCS_holiday.zip")
    with zipfile.ZipFile(result.parts[0].path) as archive:
        assert archive.testzip() is None
        assert set(payloads) <= set(archive.namelist())
    assert sink.max_percent[Stage.ARCHIVE] == 1.0


def test_fallback_warns_that_it_cannot_split_and_writes_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_config: ArchiveConfig
) -> None:
    monkeypatch.setattr("dcs.core.archiver.shutil.which", lambda name: None)
    archive_config.split_size_mb = 1
    root = make_tree(tmp_path / "usb", {"random.bin": 2 * 1024 * 1024})
    dest = tmp_path / "images"

    result, sink = create_archive(archive_config, root, dest, "DCS_holiday.zip")

    assert result.ok is True
    assert result.tool == "python-zipfile"
    assert any("cannot be split" in warning for warning in result.warnings)
    assert len(result.parts) == 1
    assert result.parts[0].path == str(dest / "DCS_holiday.zip")
    assert len(list(dest.iterdir())) == 1
    assert sink.max_percent[Stage.ARCHIVE] == 1.0


@requires_zip
def test_zip_binary_is_used_after_downgrading_from_7z(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_config: ArchiveConfig
) -> None:
    real_which = shutil.which
    monkeypatch.setattr(
        "dcs.core.archiver.shutil.which",
        lambda name: None if name in ("7z", "7za") else real_which(name),
    )
    archive_config.format = "7z"
    archive_config.split_size_mb = 1
    root = make_tree(tmp_path / "usb", {"random.bin": 2 * 1024 * 1024})
    dest = tmp_path / "images"

    result, _sink = create_archive(archive_config, root, dest, "DCS_holiday.7z")

    # the zip binary is installed, so the downgraded archive can be built and
    # split with it, and the warning must not claim that zip is missing
    assert result.tool == "zip"
    assert not any("zip tool is not installed" in warning for warning in result.warnings)
    assert len(result.parts) > 1


# ---------------------------------------------------------------------------
# capacity
# ---------------------------------------------------------------------------


def test_required_free_bytes_applies_and_clamps_the_ratio(
    archive_config: ArchiveConfig,
) -> None:
    archiver = Archiver(archive_config)  # default ratio is 1.1
    assert archive_config.min_free_space_ratio == 1.1
    assert archiver.required_free_bytes(1234) == int(1234 * 1.1) == 1357
    assert archiver.required_free_bytes(0) == 0

    archive_config.min_free_space_ratio = 2.0
    assert archiver.required_free_bytes(1234) == 2468

    # a ratio below 1.0 would let the image fill the disk, so it is clamped up
    archive_config.min_free_space_ratio = 0.25
    assert archiver.required_free_bytes(1234) == 1234

    archive_config.min_free_space_ratio = 1.0
    assert archiver.required_free_bytes(1234) == 1234


def test_check_capacity_compares_free_space_with_the_required_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_config: ArchiveConfig
) -> None:
    monkeypatch.setattr("dcs.core.archiver.free_bytes", lambda path: 5000)
    archiver = Archiver(archive_config)

    ok, problem = archiver.check_capacity(str(tmp_path), 5000)  # 5000 * 1.1 = 5500 needed
    assert ok is False
    assert problem.startswith(f"not enough free space in {tmp_path}:")
    assert "5.4 KiB" in problem  # the required size, human readable
    assert "4.9 KiB" in problem  # the available size, human readable

    ok, problem = archiver.check_capacity(str(tmp_path), 1000)
    assert (ok, problem) == (True, "")


def test_create_refuses_to_start_when_the_target_has_no_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_config: ArchiveConfig
) -> None:
    monkeypatch.setattr("dcs.core.archiver.free_bytes", lambda path: 10)
    root = make_tree(tmp_path / "usb", {"readme.txt": b"hello"})
    dest = tmp_path / "images"
    sink = CollectingSink()

    result = Archiver(archive_config).create(
        str(root),
        str(dest),
        name="DCS_holiday.zip",
        sink=sink,
        total_files=1,
        total_bytes=4096,
    )

    assert result.ok is False
    assert result.parts == []
    assert result.error.startswith(f"not enough free space in {dest}:")
    assert "4.4 KiB" in result.error  # 4096 * 1.1 = 4505 bytes needed
    assert result.error.endswith("but only 10 B is free")
    assert result.tool == "python-zipfile"  # no tool was run
    assert list(dest.iterdir()) == []  # and nothing was written
    assert sink.stages == [(Stage.ARCHIVE, StageState.FAILED, result.error)]
    assert sink.progress == []


# ---------------------------------------------------------------------------
# _collect_parts
# ---------------------------------------------------------------------------


def test_collect_parts_returns_zip_volumes_in_upload_order(tmp_path: Path) -> None:
    directory = tmp_path / "images"
    directory.mkdir()
    for name, size in (
        ("DCS_holiday.z02", 20),
        ("DCS_holiday.zip", 30),
        ("DCS_holiday.z01", 10),
        ("unrelated.txt", 40),
        ("DCS_holiday.z03", 0),  # empty files are not volumes
    ):
        (directory / name).write_bytes(b"x" * size)
    (directory / "DCS_holiday.z04").mkdir()  # neither are directories

    parts = Archiver._collect_parts(str(directory / "DCS_holiday.zip"), "zip")

    assert [os.path.basename(part.path) for part in parts] == [
        "DCS_holiday.z01",
        "DCS_holiday.z02",
        "DCS_holiday.zip",
    ]
    assert [part.size for part in parts] == [10, 20, 30]


def test_collect_parts_returns_7z_volumes_in_ascending_order(tmp_path: Path) -> None:
    directory = tmp_path / "images"
    directory.mkdir()
    for name, size in (
        ("archive.7z.002", 20),
        ("archive.7z.010", 100),
        ("archive.7z.001", 10),
        ("archive.zip", 5),  # a different format is not a volume of this archive
    ):
        (directory / name).write_bytes(b"x" * size)

    parts = Archiver._collect_parts(str(directory / "archive.7z"), "7z")

    assert [os.path.basename(part.path) for part in parts] == [
        "archive.7z.001",
        "archive.7z.002",
        "archive.7z.010",
    ]


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------


def test_archive_result_derived_values() -> None:
    result = ArchiveResult(
        ok=True,
        tool="zip",
        source_files=3,
        source_bytes=2048,
        parts=[ArchivePart(path="/tmp/out/DCS_holiday.zip", size=512)],
    )
    assert result.archive_bytes == 512
    assert result.primary_path == "/tmp/out/DCS_holiday.zip"
    assert result.split is False
    assert result.ratio == 0.25
    assert result.to_dict()["partCount"] == 1
    assert result.to_dict()["archiveBytesText"] == "512 B"


def test_human_archive_summary_failure_form() -> None:
    assert human_archive_summary(ArchiveResult(ok=False, error="disk full")) == (
        "archive failed: disk full"
    )


def test_human_archive_summary_lists_volumes_and_sizes() -> None:
    result = ArchiveResult(
        ok=True,
        source_bytes=10_000,
        parts=[
            ArchivePart(path="/tmp/out/DCS_holiday.z01", size=100),
            ArchivePart(path="/tmp/out/DCS_holiday.z02", size=100),
            ArchivePart(path="/tmp/out/DCS_holiday.zip", size=200),
        ],
    )
    assert human_archive_summary(result) == (
        "DCS_holiday.z01, DCS_holiday.z02, DCS_holiday.zip - 400 B from 9.8 KiB"
    )


def test_human_archive_summary_shortens_more_than_three_volumes() -> None:
    result = ArchiveResult(
        ok=True,
        source_bytes=2048,
        parts=[ArchivePart(path=f"/tmp/out/DCS_holiday.z{index:02d}", size=100) for index in range(1, 6)],
    )
    assert human_archive_summary(result) == (
        "DCS_holiday.z01, DCS_holiday.z02, DCS_holiday.z03, ... (+2) - 500 B from 2.0 KiB"
    )
