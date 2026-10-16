#!/usr/bin/env python3
"""Turn the two illustrations into the icon set the desktop installs.

    ./scripts/make-icons.py

The artwork is the two 1254x1254 illustrations in docs/images: a duck holding a
pistol whose muzzle faces the viewer.  This script

* cuts the white background out (a flood fill from the borders, so highlights
  *inside* the drawing stay opaque) and trims to the drawing with a small
  margin,
* renders the nine sizes a theme asks for - the flatter of the two illustrations
  for the sizes that have to survive a launcher and a panel, the detailed one
  from 128 px up, where its detail is actually visible,
* keeps the in-app copy (512 px) that the About page and the window icon use,
* checks the result: every size must be square, have an alpha channel and a
  transparent border, and must not carry a white halo at its edge.

Only the raster sizes are shipped.  There is deliberately no symbolic variant:
a monochrome stencil of a photograph-like illustration reads as a blob at panel
sizes, and DCS has no tray icon that would need one; the interface's own icons
come from the user's icon theme and stay Breeze.

Needs Pillow (`python3-pil`), and nothing else.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
except ImportError as exc:  # pragma: no cover - the message is the point
    print(f"make-icons.py needs Pillow: sudo apt install python3-pil ({exc})", file=sys.stderr)
    raise SystemExit(1) from exc

REPO = Path(__file__).resolve().parent.parent

#: the illustrations, as drawn for this project
DETAILED = REPO / "docs" / "images" / "detail-icon.png"
FLATTER = REPO / "docs" / "images" / "less-detail-icon.png"

#: where the theme looks for them
HICOLOR = REPO / "packaging" / "icons" / "hicolor"

#: the copy that travels inside the package (window icon, About page)
IN_APP = REPO / "src" / "dcs" / "gui" / "qml" / "icons" / "dcs.png"

#: the sizes a theme may ask for, and which illustration to use
SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)

#: below this size the flatter drawing wins: at 16 px the detailed one is mush
DETAIL_FROM = 128

#: pixels this close to white, connected to the border, are background
BACKGROUND_THRESHOLD = 232

#: the margin left around the drawing, as a fraction of its size
MARGIN = 0.02

#: a little contrast helps the tiny sizes
SMALL_SIZE = 32


def cut_background(path: Path) -> Image.Image:
    """Return *path* with its white background made transparent."""
    image = Image.open(path).convert("RGBA")
    candidates = image.convert("L").point(
        lambda value: 0 if value >= BACKGROUND_THRESHOLD else 255, "L"
    )
    width, height = candidates.size
    for seed in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        if candidates.getpixel(seed) == 0:
            # 128 marks "reached from the border": white that is enclosed by the
            # drawing - an eye, a highlight - is never touched
            ImageDraw.floodfill(candidates, seed, 128, thresh=0)
    alpha = candidates.point(lambda value: 0 if value == 128 else 255, "L")
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.6))
    image.putalpha(alpha)
    return image


def squared(image: Image.Image) -> Image.Image:
    """Trim to the drawing and centre it on a square canvas with a margin."""
    box = image.getbbox()
    if box is None:
        raise SystemExit("the illustration is empty")
    image = image.crop(box)
    side = int(max(image.size) * (1 + MARGIN * 2))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2), image)
    return canvas


def icon(master: Image.Image, size: int) -> Image.Image:
    """One size, with a touch of contrast where the drawing is tiny."""
    scaled = master.resize((size, size), Image.Resampling.LANCZOS)
    if size <= SMALL_SIZE:
        scaled = ImageEnhance.Contrast(scaled).enhance(1.12)
        scaled = ImageEnhance.Color(scaled).enhance(1.15)
    return scaled


def edge_luminance(image: Image.Image) -> float:
    """Mean luminance of the opaque pixels that touch transparent ones.

    A cut that leaves white behind shows up here as a bright ring, which is the
    one artefact that looks broken on a dark panel.
    """
    alpha = image.getchannel("A")
    grey = image.convert("L")
    width, height = image.size
    values: list[int] = []
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            if alpha.getpixel((x, y)) > 200:
                continue
            neighbours = ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
            if any(alpha.getpixel(point) > 200 for point in neighbours):
                values.append(grey.getpixel((x, y)))
    return statistics.mean(values) if values else 0.0


def main() -> int:
    for source in (DETAILED, FLATTER):
        if not source.is_file():
            print(f"missing illustration: {source}", file=sys.stderr)
            return 1

    print(f"cutting the background out of {FLATTER.name} and {DETAILED.name}")
    flat_master = squared(cut_background(FLATTER))
    detail_master = squared(cut_background(DETAILED))
    print(f"  drawings: {flat_master.size[0]} px and {detail_master.size[0]} px after trimming")

    print("rendering the theme sizes")
    for size in SIZES:
        master = detail_master if size >= DETAIL_FROM else flat_master
        source = DETAILED if size >= DETAIL_FROM else FLATTER
        target = HICOLOR / f"{size}x{size}" / "apps" / "org.kde.dcs.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered = icon(master, size)
        rendered.save(target, "PNG", optimize=True)

        # checks: square, transparent at the edges, and no white ring
        assert rendered.size == (size, size), rendered.size
        assert rendered.getchannel("A").getextrema()[0] == 0, f"{target} has no transparency"
        for corner in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
            assert rendered.getchannel("A").getpixel(corner) < 8, f"{target} is not transparent in the corner"
        halo = edge_luminance(rendered)
        flag = "halo!" if halo > 190 else "ok"
        print(f"  {size:3} x {size:<3} from {source.name:22} {target.stat().st_size // 1024:4} KiB  edge {halo:5.1f} {flag}")

    print("writing the copy that ships inside the package")
    IN_APP.parent.mkdir(parents=True, exist_ok=True)
    big = icon(detail_master, 512)
    big.save(IN_APP, "PNG", optimize=True)
    print(f"  {IN_APP.relative_to(REPO)} ({IN_APP.stat().st_size // 1024} KiB)")

    print("done: the icon set is the two illustrations, nothing else")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
