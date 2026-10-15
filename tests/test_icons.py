"""The application artwork: the SVGs, the bitmap sizes and what installs them.

The icon is the only artwork this project ships, and it is used from three places
that can drift apart: the package (the wheel carries it), the desktop
(`scripts/install.sh` copies it into hicolor) and the interface (the About page
shows it).  These tests keep the three in step, and they check the two things an
icon must have to work at all: an SVG that carries its own geometry, and bitmaps
with a transparent background.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HICOLOR = ROOT / "packaging" / "icons" / "hicolor"
SCALABLE = HICOLOR / "scalable" / "apps"
APP_ICON = SCALABLE / "org.kde.dcs.svg"
SYMBOLIC_ICON = SCALABLE / "org.kde.dcs-symbolic.svg"
IN_APP_ICON = ROOT / "src" / "dcs" / "gui" / "qml" / "icons" / "dcs.svg"
DESKTOP_FILE = ROOT / "packaging" / "org.kde.dcs.desktop"
INSTALL_SCRIPT = ROOT / "scripts" / "install.sh"
UNINSTALL_SCRIPT = ROOT / "scripts" / "uninstall.sh"
ICON_SCRIPT = ROOT / "scripts" / "make-icons.sh"

#: every size a theme may ask a raster icon for
SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)

SVG_NS = "{http://www.w3.org/2000/svg}"

#: the shell loop variable, spelled so that this file contains no template
#: looking sequence of its own
SIZE_VARIABLE = "$" + "{size}"


def png_header(path: Path) -> tuple[int, int, int, int]:
    """width, height, bit depth and colour type, straight out of the IHDR."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    length, chunk = struct.unpack(">I4s", data[8:16])
    assert length == 13 and chunk == b"IHDR", f"{path} has no IHDR chunk"
    width, height, depth, colour_type = struct.unpack(">IIBB", data[16:26])
    return width, height, depth, colour_type


def size_loop_of(script: Path) -> tuple[int, ...]:
    """The sizes a 'for size in ...' loop in *script* walks over."""
    line = next(
        line
        for line in script.read_text(encoding="utf-8").splitlines()
        if line.startswith("for size in")
    )
    listed = line.split("in", 1)[1].split(";")[0].strip()
    if not listed:  # a loop over a variable, e.g. 'for size in $SIZES'
        variable = line.split("in", 1)[1].split(";")[0].strip().strip('"')
        source = next(
            entry
            for entry in script.read_text(encoding="utf-8").splitlines()
            if entry.startswith(variable.lstrip("$") + "=") or entry.startswith("SIZES=")
        )
        listed = source.split("=", 1)[1].strip().strip('"')
    return tuple(int(part) for part in listed.split())


# ---------------------------------------------------------------------------
# the scalable icons
# ---------------------------------------------------------------------------


def test_the_application_icon_is_a_self_contained_svg() -> None:
    text = APP_ICON.read_text(encoding="utf-8")
    root = ET.parse(APP_ICON).getroot()

    assert root.tag == SVG_NS + "svg"
    assert root.get("viewBox"), "an icon has to scale, so it needs a viewBox"
    # nothing may be pulled in from outside: an icon is one file
    assert "<image" not in text and "<script" not in text and "href" not in text


def test_the_application_icon_has_the_shapes_it_is_about() -> None:
    """A duck, a bill, a gun and the badge: the drawing is not empty."""
    root = ET.parse(APP_ICON).getroot()
    circles = list(root.iter(SVG_NS + "circle"))
    ellipses = list(root.iter(SVG_NS + "ellipse"))
    paths = list(root.iter(SVG_NS + "path"))

    assert len(circles) >= 4, "the badge, the muzzle and the bore are circles"
    assert len(ellipses) >= 4, "two eyes and two wings"
    assert len(paths) >= 3, "the duck, the slide and the bill"
    colours = {element.get("fill") for element in root.iter() if element.get("fill")}
    assert "#FED329" in colours, "the duck is yellow"
    assert "#F59E0B" in colours, "the bill is orange"
    assert "#0A66C8" in colours, "the wings are blue"


def test_the_symbolic_icon_follows_the_breeze_convention() -> None:
    text = SYMBOLIC_ICON.read_text(encoding="utf-8")
    root = ET.parse(SYMBOLIC_ICON).getroot()

    assert "currentColor" in text, "a symbolic icon takes the colour of the theme"
    assert "ColorScheme-Text" in text, "Breeze names the colour class that way"
    assert 'fill-rule="evenodd"' in text, "the eyes and the barrel are holes"
    assert root.get("viewBox") == "0 0 22 22", "symbolic icons use the small grid"
    assert "<image" not in text and "href" not in text


def test_the_icon_inside_the_package_is_the_same_drawing() -> None:
    """The About page shows qml/icons/dcs.svg while the launcher shows the one
    from packaging/: two copies of one drawing must not drift apart."""
    assert IN_APP_ICON.read_bytes() == APP_ICON.read_bytes()


# ---------------------------------------------------------------------------
# the bitmap sizes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES)
def test_every_bitmap_size_exists_with_its_own_size_and_transparency(size: int) -> None:
    path = HICOLOR / (str(size) + "x" + str(size)) / "apps" / "org.kde.dcs.png"
    assert path.is_file(), f"{path} is missing"

    width, height, depth, colour_type = png_header(path)
    assert (width, height) == (size, size)
    assert depth == 8
    # 6 is truecolour with alpha; anything else would sit on a white square
    assert colour_type == 6, f"{path} has no alpha channel"


def test_the_bitmaps_are_small_enough_to_ship() -> None:
    biggest = HICOLOR / "512x512" / "apps" / "org.kde.dcs.png"
    assert biggest.stat().st_size < 200 * 1024


# ---------------------------------------------------------------------------
# what actually puts them on a desktop
# ---------------------------------------------------------------------------


def test_the_desktop_entry_names_an_icon_that_exists() -> None:
    icon_line = [line for line in DESKTOP_FILE.read_text().splitlines() if line.startswith("Icon=")]

    assert icon_line == ["Icon=org.kde.dcs"]
    assert APP_ICON.name == "org.kde.dcs.svg"


def test_the_installer_installs_the_scalable_icons_and_every_size() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "scalable/apps/org.kde.dcs.svg" in text
    assert "scalable/apps/org.kde.dcs-symbolic.svg" in text
    # the bitmaps are installed by one loop over the sizes, into hicolor
    assert "ICONROOT" in text
    assert "apps/org.kde.dcs.png" in text
    assert SIZE_VARIABLE + "x" + SIZE_VARIABLE in text


def test_the_installer_keeps_the_size_list_in_step_with_the_files() -> None:
    """The loop in install.sh must cover exactly the sizes that are shipped."""
    assert size_loop_of(INSTALL_SCRIPT) == SIZES


def test_the_uninstaller_removes_what_the_installer_wrote() -> None:
    text = UNINSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "org.kde.dcs.svg" in text
    assert "org.kde.dcs-symbolic.svg" in text
    assert "org.kde.dcs.png" in text


def test_the_about_page_shows_the_icon_that_ships_with_the_package() -> None:
    about = (ROOT / "src" / "dcs" / "gui" / "qml" / "views" / "AboutView.qml").read_text()

    assert 'source: "../icons/dcs.svg"' in about
    assert IN_APP_ICON.is_file()


# ---------------------------------------------------------------------------
# and what Qt does with them
# ---------------------------------------------------------------------------


def test_qt_resolves_the_icon_in_every_size(tmp_path: Path) -> None:
    """Install the icons the way install.sh does and ask Qt to load them.

    File names, sizes and theme layout are only worth anything if the toolkit
    finds them, and an SVG is not decoded without the Qt SVG plugin.  The check
    runs in a subprocess: Qt caches its icon theme for the lifetime of a
    process, so a lookup started after the interpreter is up would prove nothing.
    """
    pytest.importorskip("PyQt6.QtGui", reason="PyQt6 is not installed")
    data = tmp_path / "data"
    shutil.copytree(HICOLOR, data / "icons" / "hicolor")

    script = textwrap.dedent(
        """
        import sys

        from PyQt6.QtGui import QGuiApplication, QIcon

        app = QGuiApplication([])
        QIcon.setThemeSearchPaths([sys.argv[1], "/usr/share/icons"])
        QIcon.setThemeName("hicolor")

        icon = QIcon.fromTheme("org.kde.dcs")
        if icon.isNull():
            sys.exit("Qt did not find org.kde.dcs in the installed icons")

        sizes = sorted({size.width() for size in icon.availableSizes()})
        print("sizes:", ",".join(str(size) for size in sizes))
        for size in (16, 22, 24, 32, 48, 64, 128, 256, 512):
            pixmap = icon.pixmap(size, size)
            if pixmap.isNull() or pixmap.width() != size:
                sys.exit(f"no {size}px pixmap: {pixmap.width()}x{pixmap.height()}")
        print("pixmaps: ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(data / "icons")],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "XDG_DATA_HOME": str(data)},
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:] or result.stdout[-2000:]
    assert "sizes:" in result.stdout and "512" in result.stdout
    assert "pixmaps: ok" in result.stdout


def test_the_icon_rendering_script_covers_every_size() -> None:
    text = ICON_SCRIPT.read_text(encoding="utf-8")
    line = next(entry for entry in text.splitlines() if entry.startswith("SIZES="))
    declared = tuple(int(part) for part in line.split("=", 1)[1].strip().strip('"').split())

    assert declared == SIZES, "the script writes exactly the sizes that are shipped"
    assert "org.kde.dcs.svg" in text, "it renders the scalable source"
