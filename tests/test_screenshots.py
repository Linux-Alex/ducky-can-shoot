"""The documentation screenshots may not contradict their own captions.

``docs/images/result-view.png`` is captioned "a clean verdict: no findings,
the archive and the share link", so the run behind it has to be clean: a
screenshot that shows EICAR under a clean caption tells the reader something
about the tool that is not true.  These tests hold the two halves of that
promise: the sample medium, and the guards that refuse to save an image whose
run came out differently.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "make-screenshots.py"

#: what the infected demo medium has to contain on top of the documents
SAMPLES = (
    "eicar.com",
    "payload.txt",
    "autorun.inf",
    "setup.exe",
    "invoice.pdf.exe",
    "update.ps1",
    "cleanup.bat",
)


@pytest.fixture(scope="module")
def shots() -> Any:
    """The screenshot script itself, imported without rendering anything."""
    pytest.importorskip("PyQt6.QtQml", reason="the screenshot script drives the real interface")
    spec = importlib.util.spec_from_file_location("dcs_make_screenshots", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fake_backend(verdict: str, findings: int, link: str = "") -> Any:
    """The little of Backend the guards look at."""
    return types.SimpleNamespace(
        summary={"verdict": verdict, "findingCount": findings, "shareLink": link},
        findings=types.SimpleNamespace(count=findings),
    )


# -- the demo media -------------------------------------------------------
def test_the_clean_medium_contains_no_samples(shots: Any, tmp_path: Path) -> None:
    shots.make_medium(tmp_path, nested_files=8)

    files = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())
    assert len(files) == 8, files
    assert set(files) & set(SAMPLES) == set(), f"the clean medium carries samples: {files}"
    assert not any("EICAR" in p.read_text(errors="ignore") for p in tmp_path.rglob("*.txt"))


def test_the_infected_medium_contains_every_sample(shots: Any, tmp_path: Path) -> None:
    shots.make_medium(tmp_path, nested_files=8, samples=True)

    files = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    missing = set(SAMPLES) - files
    assert not missing, f"the infected medium is missing {sorted(missing)}"
    assert (tmp_path / "eicar.com").read_bytes().startswith(b"X5O!P%@AP[4")


def test_the_samples_are_opt_in(shots: Any, tmp_path: Path) -> None:
    """The default has to stay clean: the clean shot is built with no argument."""
    clean = tmp_path / "clean"
    clean.mkdir()
    shots.make_medium(clean)
    assert not (clean / "eicar.com").exists()


# -- the guards -----------------------------------------------------------
def test_a_clean_shot_is_refused_when_the_run_found_something(shots: Any) -> None:
    backend = fake_backend("infected", 22, "")
    with pytest.raises(AssertionError, match="clean medium came out 'infected'"):
        shots.Shot.check_clean(backend)


def test_a_clean_shot_is_refused_when_a_finding_is_listed(shots: Any) -> None:
    backend = fake_backend("clean", 3, shots.DEMO_SHARE_LINK)
    with pytest.raises(AssertionError, match="3 finding"):
        shots.Shot.check_clean(backend)


def test_a_clean_shot_is_refused_without_the_share_link(shots: Any) -> None:
    backend = fake_backend("clean", 0, "")
    with pytest.raises(AssertionError, match="no demo link"):
        shots.Shot.check_clean(backend)


def test_a_clean_run_passes_the_guard(shots: Any) -> None:
    shots.Shot.check_clean(fake_backend("clean", 0, shots.DEMO_SHARE_LINK))


def test_an_infected_shot_is_refused_when_nothing_was_found(shots: Any) -> None:
    backend = fake_backend("clean", 0, "")
    with pytest.raises(AssertionError, match="came out 'clean'"):
        shots.Shot.check_infected(backend)


def test_an_infected_shot_is_refused_when_a_link_was_published(shots: Any) -> None:
    backend = fake_backend("infected", 22, shots.DEMO_SHARE_LINK)
    with pytest.raises(AssertionError, match="must not publish a link"):
        shots.Shot.check_infected(backend)


def test_an_infected_run_passes_the_guard(shots: Any) -> None:
    shots.Shot.check_infected(fake_backend("infected", 22, ""))


# -- the stand-in upload host --------------------------------------------
def test_the_stand_in_provider_answers_like_a_host(shots: Any, tmp_path: Path) -> None:
    """The pipeline is untouched, so the stand-in has to fit its calls."""
    part = tmp_path / "DCS_SANDISK16.zip"
    part.write_bytes(b"PK\x03\x04" + b"0" * 512)
    seen: list[tuple[int, int]] = []

    result = shots.DemoProvider().upload(
        str(part),
        progress=lambda sent, total: seen.append((sent, total)),
        cancel=None,
        part_label="",
    )

    assert result.ok, result.error
    assert result.link == shots.DEMO_SHARE_LINK
    assert result.size == part.stat().st_size
    assert seen and seen[-1][0] == result.size


def test_the_script_replaces_the_upload_provider(shots: Any) -> None:
    """Refreshing the docs must not publish a file to a real free host."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "pipeline_module.build_provider = " in source


def test_the_script_builds_the_infected_medium_with_samples(shots: Any) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "samples=True" in source
    # the clean medium is the one built without a keyword argument
    assert 'make_medium(medium / "usb")' in source
