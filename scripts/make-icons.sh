#!/usr/bin/env bash
# Rasterise the application icons into the sizes a theme expects.
#
#   ./scripts/make-icons.sh            write packaging/icons/hicolor/<size>x<size>
#
# The scalable sources are the ones that matter and the only ones installed on a
# modern desktop; the PNGs are the fallback every distribution package still
# ships, for launchers and docks that do not look at scalable/ at all.  Render
# them with whatever SVG tool is installed, so this works on a bare machine.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

SOURCE="packaging/icons/hicolor/scalable/apps/org.kde.dcs.svg"
SIZES="16 22 24 32 48 64 128 256 512"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

if command -v rsvg-convert >/dev/null; then
    render() { rsvg-convert -w "$1" -h "$1" -o "$2" "$SOURCE"; }
elif command -v inkscape >/dev/null; then
    render() { inkscape --export-type=png --export-width="$1" --export-height="$1" --export-filename="$2" "$SOURCE" >/dev/null 2>&1; }
elif command -v magick >/dev/null; then
    render() { magick -background none -density 512 -resize "$1x$1" "$SOURCE" "$2"; }
elif command -v convert >/dev/null; then
    render() { convert -background none -density 512 -resize "$1x$1" "$SOURCE" "$2"; }
else
    printf 'no SVG renderer found: install librsvg2-bin, inkscape or imagemagick\n' >&2
    exit 1
fi

say "rendering $SOURCE"
for size in $SIZES; do
    target="packaging/icons/hicolor/${size}x${size}/apps"
    mkdir -p "$target"
    render "$size" "$target/org.kde.dcs.png"
    printf '  %s x %s\n' "$size" "$size"
done

say "checking the result"
python3 - "$SIZES" <<'PY'
import sys
from pathlib import Path
from PIL import Image

sizes = sys.argv[1].split()
for size in sizes:
    path = Path(f"packaging/icons/hicolor/{size}x{size}/apps/org.kde.dcs.png")
    with Image.open(path) as image:
        assert image.size == (int(size), int(size)), (path, image.size)
        assert image.mode == "RGBA", (path, image.mode)
        assert image.getchannel("A").getextrema()[0] == 0, f"{path} has no transparency"
print(f"{len(sizes)} sizes ok: each one has the right size and a transparent background")
PY
