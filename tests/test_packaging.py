"""The Debian package and the release helpers.

The package is built by scripts/build-deb.sh rather than by debhelper, so a few
things debhelper would take care of - the dependencies, the layout a desktop
expects, the maintainer scripts - are checked here instead.  Nothing builds a
package: that needs `python -m build` and a network for the build backend, which
belongs in CI, not in the unit tests.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEBIAN = REPO / "packaging" / "debian"
BUILD_SCRIPT = REPO / "scripts" / "build-deb.sh"
NOTES_SCRIPT = REPO / "scripts" / "changelog-for-tag.py"
BUMP_SCRIPT = REPO / "scripts" / "bump-version.py"
CONTROL = DEBIAN / "control.in"
METAINFO = REPO / "packaging" / "org.kde.dcs.metainfo.xml"
DESKTOP = REPO / "packaging" / "org.kde.dcs.desktop"
MANPAGE = REPO / "packaging" / "dcs.1"
AUTOSTART = DEBIAN / "org.kde.dcs.watch.desktop"

sys.path.insert(0, str(REPO / "src"))
from dcs.version import APP_ID, APP_NAME, __version__  # noqa: E402


def fields_of(text: str) -> dict[str, str]:
    """The control file as a dict, with continuation lines joined."""
    fields: dict[str, str] = {}
    key = ""
    for line in text.splitlines():
        if line.startswith(" ") or line.startswith("\t"):
            fields[key] += " " + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


# ---------------------------------------------------------------------------
# the build
# ---------------------------------------------------------------------------


def test_the_build_script_is_a_runnable_shell_script() -> None:
    assert BUILD_SCRIPT.is_file()
    assert os.access(BUILD_SCRIPT, os.X_OK), "the build script must be executable"

    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "--root-owner-group" in text, "the package must not carry the builder's uid"
    assert "md5sums" in text, "without debhelper, the md5sums file is ours to write"
    assert "set -euo pipefail" in text


def test_the_package_is_architecture_independent() -> None:
    """Pure Python: one .deb for every architecture, no bundled Qt."""
    assert "ARCH=\"all\"" in BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "dist-packages" in BUILD_SCRIPT.read_text(encoding="utf-8")


def test_the_control_file_depends_on_what_the_interface_needs() -> None:
    fields = fields_of(CONTROL.read_text(encoding="utf-8"))

    assert fields["Package"] == "ducky-can-shoot"
    assert fields["Architecture"] == "all"
    assert fields["Version"] == "@VERSION@-@REVISION@"
    assert fields["Section"] == "utils"

    depends = fields["Depends"]
    for package in (
        "python3 (>= 3.11)",
        "python3-pyqt6",
        "python3-pyqt6.qtqml",
        "python3-pyqt6.qtquick",
        "python3-requests",
        "qml6-module-org-kde-kirigami",
        "qml6-module-qtquick-controls",
        "qml6-module-qtquick-layouts",
        "qml6-module-qtquick-dialogs",
        "qml6-module-qtquick-templates",
        "qml6-module-qtqml-workerscript",
        "libqt6svg6",
        "qt6-svg-plugins",
        "libglib2.0-bin",
        "zip",
        "udisks2",
        "util-linux",
    ):
        assert package in depends, f"{package} is not a dependency"
    assert "clamav" in fields["Recommends"], "the scanners are optional, not required"


def test_the_package_installs_the_paths_a_desktop_looks_at() -> None:
    text = BUILD_SCRIPT.read_text(encoding="utf-8")

    for path in (
        "/usr/share/applications/org.kde.dcs.desktop",
        "/usr/share/metainfo/org.kde.dcs.metainfo.xml",
        "/usr/share/man/man1/dcs.1.gz",
        "/usr/bin/dcs",
        "/usr/bin/dcs-gui",
        "/etc/xdg/autostart/org.kde.dcs.watch.desktop",
    ):
        assert path in text, f"{path} is not installed"
    assert "usr/share/icons/hicolor" in text
    assert "usr/lib/python3/dist-packages" in text


def test_the_wrapper_runs_the_distribution_interpreter() -> None:
    """No virtualenv in the package: the distribution's python3 finds it."""
    wrapper = (DEBIAN / "dcs.sh").read_text(encoding="utf-8")

    assert wrapper.startswith("#!/bin/sh")
    assert "exec python3 -m dcs" in wrapper
    assert (DEBIAN / "dcs-gui.sh").read_text(encoding="utf-8").strip().endswith('gui "$@"')


def test_the_maintainer_scripts_are_careful() -> None:
    for name in ("postinst", "postrm"):
        path = DEBIAN / name
        assert path.is_file(), f"{name} is missing"
        assert os.access(path, os.X_OK), f"{name} must be executable"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh")
        assert "set -e" in text
        assert text.rstrip().endswith("exit 0"), "a maintainer script must exit 0"
        # a cache tool that is missing must not fail the installation
        assert "|| true" in text

    # Python bytecode is compiled and cleaned the Debian way, so that running the
    # program does not leave a __pycache__ that dpkg cannot remove
    postinst = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert "py3compile -p ducky-can-shoot" in postinst
    # a Plasma session caches desktop entries and icon lookups: without this the
    # new icon can stay invisible until the next login
    assert "kbuildsycoca6" in postinst and "--noincremental" in postinst

    postrm = (DEBIAN / "postrm").read_text(encoding="utf-8")
    assert "py3clean -p ducky-can-shoot" in postrm
    # and the tree is cleaned directly as well: py3clean -p leaves the bytecode
    # that py3compile wrote, and an empty directory keeps dpkg from finishing
    assert "rmdir" in postrm and "__pycache__" in postrm


def test_the_copyright_file_follows_the_machine_readable_format() -> None:
    text = (DEBIAN / "copyright").read_text(encoding="utf-8")

    assert text.startswith("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/")
    assert "License: GPL-3.0-or-later" in text
    assert "/usr/share/common-licenses/GPL-3" in text


# ---------------------------------------------------------------------------
# the metadata the package carries
# ---------------------------------------------------------------------------


def test_the_appstream_metadata_describes_this_application() -> None:
    root = ET.parse(METAINFO).getroot()
    desktop_id = root.findtext("launchable")

    assert root.get("type") == "desktop-application"
    assert root.findtext("id") == APP_ID
    assert root.findtext("name") == APP_NAME
    assert desktop_id == DESKTOP.name, "the launchable has to be the desktop entry"
    assert root.findtext("metadata_license")
    assert root.findtext("project_license") == "GPL-3.0-or-later"
    assert root.find("developer") is not None, "AppStream asks for a developer block"
    assert [category.text for category in root.findall("categories/category")]
    assert "dcs" in [binary.text for binary in root.findall("provides/binary")]


def test_the_appstream_metadata_shows_the_current_release() -> None:
    root = ET.parse(METAINFO).getroot()
    releases = root.findall("releases/release")

    assert releases, "software centres show the newest release from here"
    assert releases[0].get("version") == __version__, "the newest entry is the current version"

    for release in releases:
        assert re.fullmatch(r"\d+\.\d+\.\d+", release.get("version", ""))
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", release.get("date", ""))


def test_the_appstream_screenshots_are_the_ones_in_the_repository() -> None:
    root = ET.parse(METAINFO).getroot()
    images = [image.text or "" for image in root.findall(".//screenshot/image")]

    assert images, "a software centre shows at least one screenshot"
    for url in images:
        assert url.startswith("https://raw.githubusercontent.com/"), url
        name = url.rsplit("/", 1)[-1]
        assert (REPO / "docs" / "images" / name).is_file(), f"{name} does not exist"


def test_the_desktop_entry_and_the_package_agree_on_the_command() -> None:
    entry = DESKTOP.read_text(encoding="utf-8")

    assert "Exec=dcs" in entry
    assert "Icon=org.kde.dcs" in entry


def test_the_autostart_entry_starts_the_watcher_quietly() -> None:
    entry = AUTOSTART.read_text(encoding="utf-8")

    assert "Exec=dcs watch" in entry
    assert "Type=Application" in entry
    assert "NoDisplay=true" in entry, "it is a background service, not a menu entry"
    assert "Terminal=false" in entry


def test_the_man_page_documents_the_command() -> None:
    text = MANPAGE.read_text(encoding="utf-8")

    assert ".TH DCS 1" in text
    assert ".SH NAME" in text and ".SH EXIT STATUS" in text
    assert text.count("\\-") > 20, "roff wants escaped hyphens in options"
    for command in (r"dcs scan", r"dcs run", r"dcs watch", r"dcs doctor", r"dcs install\-clamav"):
        assert command in text, f"{command} is not documented"
    assert "~/.config/dcs/config.toml" in text


# ---------------------------------------------------------------------------
# starting a release
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# the release notes
# ---------------------------------------------------------------------------


def release_notes(*args: str) -> str:
    """Run the notes script the way the release workflow does."""
    result = subprocess.run(
        [sys.executable, str(NOTES_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_release_notes_carry_the_changelog_and_the_install_steps() -> None:
    notes = release_notes("v9.9.9", "--changelog", str(REPO / "CHANGELOG.md"))

    assert "## Installation" in notes, "the install steps are part of the body"
    for route in ("apt install ./ducky-can-shoot_9.9.9-1_all.deb",
                  "pip install ducky_can_shoot-9.9.9-py3-none-any.whl",
                  "git clone"):
        assert route in notes, f"{route} is missing from the notes"
    assert notes.count("```bash") == 3
    assert notes.count("```") == 6, "every code block is opened and closed"
    assert "CHANGELOG.md" in notes


def test_the_release_notes_quote_the_changelog_for_that_version() -> None:
    notes = release_notes("0.2.0")

    assert "### Changed" in notes, "the 0.2.0 section is quoted"
    assert "The interface follows the KDE Human Interface Guidelines" in notes
    assert "## [0.2.0]" not in notes, "the heading itself is left out"


def test_the_notes_are_not_run_through_a_shell() -> None:
    """The first two releases lost their code fences this way.

    A shell heredoc performs command substitution, so a fenced block written
    into one turned into the *output* of apt, pip and the installer - complete
    with terminal escape codes.
    """
    text = NOTES_SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "Popen", "check_output"):
        assert forbidden not in text, f"the notes script must not shell out: {forbidden}"

    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "cat >> release-notes.md" not in workflow, "notes are written by the script"
    assert "<<EOF" not in workflow, "an unquoted heredoc would run what is inside it"


def test_the_notes_can_be_the_changelog_alone() -> None:
    notes = release_notes("0.2.0", "--changelog-only")

    assert "## Installation" not in notes
    assert "### Changed" in notes


def test_the_version_bump_moves_every_place_that_carries_a_version(tmp_path: Path) -> None:
    """One command has to do all four, or a release goes out half bumped."""
    for name in ("pyproject.toml", "CHANGELOG.md", "README.md"):
        shutil.copy(REPO / name, tmp_path / name)
    (tmp_path / "src" / "dcs").mkdir(parents=True)
    shutil.copy(REPO / "src" / "dcs" / "version.py", tmp_path / "src" / "dcs" / "version.py")
    (tmp_path / "packaging").mkdir()
    shutil.copy(METAINFO, tmp_path / "packaging" / METAINFO.name)

    result = subprocess.run(
        [sys.executable, str(BUMP_SCRIPT), "9.9.9", "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    assert '__version__ = "9.9.9"' in (tmp_path / "src" / "dcs" / "version.py").read_text()
    assert 'version = "9.9.9"' in (tmp_path / "pyproject.toml").read_text()

    metainfo = ET.parse(tmp_path / "packaging" / METAINFO.name).getroot()
    assert metainfo.findall("releases/release")[0].get("version") == "9.9.9"

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "status-9.9.9%20alpha" in readme, "the README badge has to move with the version"
    assert "ducky-can-shoot_9.9.9-1_all.deb" in readme

    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]\n\nNothing yet.\n\n## [9.9.9] - " in changelog
    assert "[9.9.9]: https://github.com/Linux-Alex/ducky-can-shoot/releases/tag/v9.9.9" in changelog
    assert "/compare/v9.9.9...HEAD" in changelog


def test_the_version_bump_refuses_a_version_that_is_not_one(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(BUMP_SCRIPT), "next", "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "is not a version" in result.stderr


def test_the_changelog_has_the_link_targets_the_bump_script_rewrites() -> None:
    """The script edits those two lines; losing them would break the release."""
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")

    assert re.search(r"^\[Unreleased\]: https://github\.com/\S+/compare/v\S+\.\.\.HEAD$", changelog, re.M)
    assert re.search(r"^\[\d+\.\d+\.\d+\]: https://github\.com/\S+/releases/tag/v\S+$", changelog, re.M)
