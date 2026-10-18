"""The YARA rule pack: fetch, unpack, compile-check, install, report.

Nothing here reaches the network: `yara_pack.fetch` is replaced with a tarball
built in the test, and the compile check runs against a small stand-in binary.
"""

from __future__ import annotations

import io
import json
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from dcs.core import yara_pack as pack


def tarball(files: dict[str, str], prefix: str = "signature-base-master") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the pack inside the test directory."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(pack, "pack_dir", lambda: tmp_path / "dcs" / "yara" / pack.PACK_NAME)
    return tmp_path


def fake_yara(tmp_path: Path, *, broken: str = "", always_fail: bool = False) -> Path:
    """A yara stand-in: exit 1 for a named file, or for every compile."""
    binary = tmp_path / "yara"
    if always_fail:
        body = "#!/bin/sh\nexit 1\n"
    else:
        body = (
            "#!/bin/sh\n"
            "for arg in \"$@\"; do\n"
            f"    case \"$arg\" in *{broken or '__never__'}*) exit 1 ;; esac\n"
            "done\n"
            "exit 0\n"
        )
    binary.write_text(body)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


# -- unpacking -------------------------------------------------------------
def test_a_tarball_is_unpacked_without_its_top_directory() -> None:
    files = pack.extract_rules(tarball({"yara/one.yar": "rule One { condition: true }"}))

    assert list(files) == ["yara/one.yar"]


def test_a_zip_is_unpacked_too() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("signature-base-master/yara/two.yara", "rule Two { condition: true }")

    files = pack.extract_rules(buffer.getvalue())

    assert list(files) == ["yara/two.yara"]


def test_paths_that_escape_the_pack_are_refused() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("../../etc/evil.yar", "/abs/path/evil.yar"):
            data = b"rule Evil { condition: true }"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    files = pack.extract_rules(buffer.getvalue())

    assert "/abs/path/evil.yar" not in files
    assert "../../etc/evil.yar" not in files


def test_rule_names_are_read_from_the_files() -> None:
    names = pack.rule_names(
        {
            "a.yar": b"rule One { condition: true }\nprivate rule Two { condition: true }",
            "b.yar": b"global rule Three { condition: true }",
        }
    )

    assert names == {"One", "Two", "Three"}


# -- installing ------------------------------------------------------------
def test_an_update_installs_the_pack(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = fake_yara(home)
    monkeypatch.setattr(pack.shutil, "which", lambda name: str(binary) if name == "yara" else None)
    payload = tarball(
        {
            "yara/one.yar": "rule One { condition: true }",
            "yara/base/two.yar": "rule Two { condition: true }",
        }
    )
    monkeypatch.setattr(pack, "fetch", lambda url, timeout=0: payload)

    report = pack.update()

    assert report.ok is True
    assert report.verified is True
    assert report.total_rules == 2
    assert sorted(report.added_files) == ["yara/base/two.yar", "yara/one.yar"]
    assert (pack.pack_dir() / "yara" / "one.yar").is_file()
    manifest = json.loads((pack.pack_dir() / "manifest.json").read_text())
    assert manifest["rules"] == ["One", "Two"]
    assert manifest["sha256"] == report.sha256


def test_a_second_update_reports_nothing_changed(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = fake_yara(home)
    monkeypatch.setattr(pack.shutil, "which", lambda name: str(binary) if name == "yara" else None)
    payload = tarball({"yara/one.yar": "rule One { condition: true }"})
    monkeypatch.setattr(pack, "fetch", lambda url, timeout=0: payload)
    pack.update()

    report = pack.update()

    assert report.unchanged is True
    assert report.added_files == [] and report.removed_files == []


def test_a_new_rule_is_reported(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = fake_yara(home)
    monkeypatch.setattr(pack.shutil, "which", lambda name: str(binary) if name == "yara" else None)
    monkeypatch.setattr(
        pack, "fetch", lambda url, timeout=0: tarball({"yara/one.yar": "rule One { condition: true }"})
    )
    pack.update()

    monkeypatch.setattr(
        pack,
        "fetch",
        lambda url, timeout=0: tarball(
            {
                "yara/one.yar": "rule One { condition: true }",
                "yara/two.yar": "rule Two { condition: true }",
            }
        ),
    )
    report = pack.update()

    assert report.added_files == ["yara/two.yar"]
    assert report.added_rules == ["Two"]


def test_a_pack_that_does_not_compile_is_not_installed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_yara(home, always_fail=True)
    monkeypatch.setattr(pack.shutil, "which", lambda name: str(binary) if name == "yara" else None)
    monkeypatch.setattr(
        pack, "fetch", lambda url, timeout=0: tarball({"yara/one.yar": "rule One { condition: true }"})
    )

    report = pack.update()

    assert report.ok is False
    assert "does not compile" in report.error
    assert not pack.pack_dir().exists()


def test_files_that_cannot_compile_alone_are_dropped(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YARA aborts the whole run on one bad file, so the bad ones go."""
    binary = fake_yara(home, broken="broken")
    monkeypatch.setattr(pack.shutil, "which", lambda name: str(binary) if name == "yara" else None)
    monkeypatch.setattr(
        pack,
        "fetch",
        lambda url, timeout=0: tarball(
            {
                "yara/good.yar": "rule Good { condition: true }",
                "yara/broken.yar": "rule Broken { condition: true }",
            }
        ),
    )

    report = pack.update()

    assert report.ok is True
    assert report.dropped_files == ["yara/broken.yar"]
    assert not (pack.pack_dir() / "yara" / "broken.yar").exists()
    assert (pack.pack_dir() / "yara" / "good.yar").is_file()


def test_without_the_yara_binary_the_pack_is_installed_unverified(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pack.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        pack, "fetch", lambda url, timeout=0: tarball({"yara/one.yar": "rule One { condition: true }"})
    )

    report = pack.update()

    assert report.ok is True
    assert report.verified is None
    assert "not compile-checked" in report.verify_note


def test_a_download_error_is_reported(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, timeout: float = 0) -> bytes:
        raise pack.PackError("no route to host")

    monkeypatch.setattr(pack, "fetch", boom)

    report = pack.update()

    assert report.ok is False
    assert "no route" in report.error


def test_an_empty_pack_is_refused(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pack, "fetch", lambda url, timeout=0: tarball({"README.md": "nothing"}))

    report = pack.update()

    assert report.ok is False
    assert "no .yar" in report.error


def test_status_reports_what_is_installed(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = fake_yara(home)
    monkeypatch.setattr(pack.shutil, "which", lambda name: str(binary) if name == "yara" else None)
    monkeypatch.setattr(
        pack, "fetch", lambda url, timeout=0: tarball({"yara/one.yar": "rule One { condition: true }"})
    )
    assert pack.status()["installed"] is False
    pack.update()

    state = pack.status()

    assert state["installed"] is True
    assert state["rules"] == 1
    assert state["url"].startswith("https://")
