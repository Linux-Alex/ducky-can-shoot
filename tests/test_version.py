"""One version, everywhere it is shown.

A release went out with the README badge saying 0.1.0, a desktop launcher
saying 0.2.0 because its virtualenv was installed a release earlier, and the
GitHub release saying 0.2.1 - three answers to a question that has exactly one.
`src/dcs/version.py` is the source of truth; the tests here hold the copies
that a reader sees to it, so the next drift is a failing test instead of a
confusing bug report.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from dcs.version import APP_NAME, __version__

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
PYPROJECT = REPO / "pyproject.toml"
SCREENSHOTS = REPO / "scripts" / "make-screenshots.py"

#: every version-like token, so a stale claim cannot hide inside a sentence
VERSION_TOKEN = re.compile(r"\b\d+\.\d+\.\d+\b")

#: a line that names this project or one of its artefacts, and so claims *our*
#: version (the transcript's "ClamAV 1.5.4" is somebody else's and may differ)
PROJECT_CLAIM = re.compile(r"ducky-can-shoot_|ducky_can_shoot-|Ducky Can Shoot \d|status-\d")


def test_the_package_metadata_carries_the_source_of_truth() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__


def test_the_readme_badge_shows_the_current_version() -> None:
    readme = README.read_text(encoding="utf-8")
    assert f"status-{__version__}%20alpha" in readme
    assert f"Status: {__version__} alpha" in readme


def test_the_readme_quick_start_names_the_current_deb() -> None:
    readme = README.read_text(encoding="utf-8")
    assert f"ducky-can-shoot_{__version__}-1_all.deb" in readme


def test_the_readme_transcript_shows_the_current_version() -> None:
    readme = README.read_text(encoding="utf-8")
    assert f"VERDICT: INFECTED    {APP_NAME} {__version__}" in readme


def test_the_readme_claims_no_other_version_of_this_project() -> None:
    """The stale badge is exactly how the three-version confusion started."""
    stale: set[str] = set()
    for line in README.read_text(encoding="utf-8").splitlines():
        if PROJECT_CLAIM.search(line):
            stale |= set(VERSION_TOKEN.findall(line)) - {__version__}
    assert not stale, f"the README also names {sorted(stale)}"


def test_the_screenshot_script_uses_the_real_versions() -> None:
    """A screenshot may not advertise an identity the package does not have."""
    source = SCREENSHOTS.read_text(encoding="utf-8")
    assert 'setContextProperty("appVersion", __version__)' in source
    assert 'setContextProperty("appName", APP_NAME)' in source
    assert not re.search(r'setContextProperty\("appVersion", "', source)
