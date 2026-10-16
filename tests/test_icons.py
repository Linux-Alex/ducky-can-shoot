"""The application artwork: the illustrations, the rendered sizes, the wiring.

The icon is an illustration rather than a vector, so the checks here are about
the *chain*: the two source drawings exist, every size a theme asks for is
rendered from them with transparency and without a white halo edge, the copy that
ships inside the package is the same drawing as the one in the theme, and the
installer, the Debian package and the interface all point at it.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HICOLOR = REPO / "packaging" / "icons" / "hicolor"
IN_APP = REPO / "src" / "dcs" / "gui" / "qml" / "icons" / "dcs.png"
GENERATOR = REPO / "scripts" / "make-icons.py"
INSTALL_SCRIPT = REPO / "scripts" / "install.sh"
UNINSTALL_SCRIPT = REPO / "scripts" / "uninstall.sh"
PACKAGE_SCRIPT = REPO / "scripts" / "build-deb.sh"
DESKTOP_FILE = REPO / "packaging" / "org.kde.dcs.desktop"
ABOUT_VIEW = REPO / "src" / "dcs" / "gui" / "qml" / "views" / "AboutView.qml"
APP_PY = REPO / "src" / "dcs" / "gui" / "app.py"

#: the illustrations drawn for this project, and the only artwork that is used
SOURCES = (
    REPO / "docs" / "images" / "detail-icon.png",
    REPO / "docs" / "images" / "less-detail-icon.png",
)

#: every size a theme may ask a raster icon for
SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)

SIZE_VARIABLE = "$" + "{size}"


def png_header(path: Path) -> tuple[int, int, int, int]:
    """width, height, bit depth and colour type, straight out of the IHDR."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    length, chunk = struct.unpack(">I4s", data[8:16])
    assert length == 13 and chunk == b"IHDR", f"{path} has no IHDR chunk"
    width, height, depth, colour_type = struct.unpack(">IIBB", data[16:26])
    return width, height, depth, colour_type


def icon_path(size: int) -> Path:
    return HICOLOR / (str(size) + "x" + str(size)) / "apps" / "org.kde.dcs.png"


def size_loop_of(script: Path) -> tuple[int, ...]:
    """The sizes a 'for size in ...' loop in *script* walks over."""
    line = next(
        line
        for line in script.read_text(encoding="utf-8").splitlines()
        if line.startswith("for size in")
    )
    listed = line.split("in", 1)[1].split(";")[0].strip()
    return tuple(int(part) for part in listed.split())


# ---------------------------------------------------------------------------
# the illustrations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES, ids=lambda path: path.name)
def test_the_illustrations_are_in_the_repository(source: Path) -> None:
    """They are the artwork, so they are part of the build, not a local leftover."""
    assert source.is_file(), f"{source} is missing: it is the artwork"

    width, height, depth, _colour = png_header(source)
    assert width == height, "the artwork is square"
    assert width >= 512, "the artwork has to be big enough to render 512 px from"
    assert depth == 8


def test_the_icon_is_made_from_those_two_illustrations_and_nothing_else() -> None:
    text = GENERATOR.read_text(encoding="utf-8")

    for source in SOURCES:
        assert source.name in text, f"{source.name} is not used"
    assert "floodfill" in text, "the white background is cut out, not painted over"
    assert "scalable" not in text, "the artwork is raster: no vector icon is invented"
    assert os.access(GENERATOR, os.X_OK)


def test_the_generator_covers_every_shipped_size() -> None:
    text = GENERATOR.read_text(encoding="utf-8")
    line = next(entry for entry in text.splitlines() if entry.startswith("SIZES = "))
    declared = tuple(int(part) for part in line.split("=", 1)[1].strip().strip("()").split(","))

    assert declared == SIZES, "the generator writes exactly the sizes that are shipped"


# ---------------------------------------------------------------------------
# the rendered sizes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES)
def test_every_size_is_square_transparent_and_free_of_a_white_edge(size: int) -> None:
    path = icon_path(size)
    assert path.is_file(), f"{path} is missing"

    width, height, depth, colour_type = png_header(path)
    assert (width, height) == (size, size)
    assert depth == 8
    # 6 is truecolour with alpha; anything else would sit on a white square
    assert colour_type == 6, f"{path} has no alpha channel"

    pytest.importorskip("PIL.Image", reason="Pillow is needed to inspect the pixels")
    from PIL import Image

    with Image.open(path) as image:
        alpha = image.convert("RGBA").getchannel("A")
        assert alpha.getextrema()[0] == 0, "the background has to be transparent"
        for corner in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
            assert alpha.getpixel(corner) < 8, f"{path} is opaque in a corner"


def test_the_artwork_is_not_a_flat_placeholder_any_more() -> None:
    """The drawing has real colours in it: a duck, not a two colour glyph."""
    pytest.importorskip("PIL.Image", reason="Pillow is needed to inspect the pixels")
    from PIL import Image

    with Image.open(icon_path(512)) as image:
        colours = {colour for _count, colour in image.convert("RGB").getcolors(maxcolors=1 << 24)}

    assert len(colours) > 5000, "an illustration has thousands of colours"
    # the duck is yellow and the accents are blue: those hues have to be there
    yellowish = [c for c in colours if c[0] > 180 and c[1] > 140 and c[2] < 120]
    bluish = [c for c in colours if c[2] > 140 and c[2] > c[0] + 40]
    assert len(yellowish) > 100, "no yellow in the artwork"
    assert len(bluish) > 100, "no blue in the artwork"


def test_the_sizes_stay_small_enough_to_ship() -> None:
    assert icon_path(512).stat().st_size < 400 * 1024
    total = sum(icon_path(size).stat().st_size for size in SIZES)
    assert total < 600 * 1024, "the whole set should stay well under a megabyte"


def test_no_vector_icon_is_left_over_from_the_previous_artwork() -> None:
    """The old drawing is gone: the repository ships one artwork, this one."""
    assert not (HICOLOR / "scalable").exists()
    assert not (REPO / "src" / "dcs" / "gui" / "qml" / "icons" / "dcs.svg").exists()


# ---------------------------------------------------------------------------
# the copies that travel with the program
# ---------------------------------------------------------------------------


def test_the_icon_inside_the_package_is_the_same_drawing() -> None:
    """The About page and the window icon use the copy inside the package; it
    must not drift away from the one the theme installs."""
    assert IN_APP.read_bytes() == icon_path(512).read_bytes()


def test_the_window_icon_and_the_about_page_use_that_copy() -> None:
    assert '"icons", "dcs.png"' in APP_PY.read_text(encoding="utf-8")
    assert 'source: "../icons/dcs.png"' in ABOUT_VIEW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# what actually puts them on a desktop
# ---------------------------------------------------------------------------


def test_the_desktop_entry_names_an_icon_that_exists() -> None:
    icon_line = [line for line in DESKTOP_FILE.read_text().splitlines() if line.startswith("Icon=")]

    assert icon_line == ["Icon=org.kde.dcs"]
    assert icon_path(256).name == "org.kde.dcs.png"


def test_the_installer_installs_every_size() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "ICONROOT" in text
    assert "apps/org.kde.dcs.png" in text
    assert SIZE_VARIABLE + "x" + SIZE_VARIABLE in text
    # no vector version is installed; an installation that still has the old one
    # is cleaned up instead
    installed = [line for line in text.splitlines() if line.strip().startswith("install ")]
    assert not any("scalable" in line for line in installed)
    assert "rm -f" in text and "scalable/apps/org.kde.dcs.svg" in text


def test_the_installer_keeps_the_size_list_in_step_with_the_files() -> None:
    assert size_loop_of(INSTALL_SCRIPT) == SIZES
    assert size_loop_of(PACKAGE_SCRIPT) == SIZES


def test_the_uninstaller_removes_what_the_installer_wrote() -> None:
    text = UNINSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "org.kde.dcs.png" in text
    assert "ICONROOT" in text


def test_qt_resolves_the_icon_in_every_size(tmp_path: Path) -> None:
    """Install the icons the way install.sh does and ask Qt to load them.

    Raster sizes, theme layout and file names are only worth anything if the
    toolkit finds them, and Qt caches its icon theme for the lifetime of a
    process, so the lookup runs in a subprocess of its own.
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
        if sizes != [16, 22, 24, 32, 48, 64, 128, 256, 512]:
            sys.exit(f"unexpected sizes: {sizes}")
        for size in (16, 22, 24, 32, 48, 64, 128, 256, 512):
            pixmap = icon.pixmap(size, size)
            if pixmap.isNull():
                sys.exit(f"no {size}px pixmap")
            # Qt hands back the closest raster it has and scales it, so the size
            # may differ by a pixel or two (it answers 24 with the 22 entry)
            if abs(pixmap.width() - size) > 2 or pixmap.width() != pixmap.height():
                sys.exit(f"{size}px became {pixmap.width()}x{pixmap.height()}")
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
