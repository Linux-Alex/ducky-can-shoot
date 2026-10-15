#!/usr/bin/env bash
# Build a Debian package for Ducky Can Shoot.
#
#   ./scripts/build-deb.sh                package the current checkout
#   ./scripts/build-deb.sh --wheel FILE   package an already built wheel
#
# The result is dist/ducky-can-shoot_<version>-1_all.deb: one architecture
# independent package that uses the distribution's Python, PyQt6 and Kirigami,
# exactly like the README asks a user to install them.  Nothing is bundled - a
# second copy of Qt in a .deb would be a bug, not a feature.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

WHEEL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --wheel) WHEEL="${2:-}"; shift ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

VERSION="$(python3 -c 'import re, pathlib; print(re.search(r"__version__ = \"([^\"]+)\"", pathlib.Path("src/dcs/version.py").read_text()).group(1))')"
REVISION="${DEB_REVISION:-1}"
PACKAGE="ducky-can-shoot"
ARCH="all"
OUT="dist"
TREE="$(mktemp -d "${TMPDIR:-/tmp}/dcs-deb-XXXXXX")"
trap 'rm -rf "$TREE"' EXIT

say "packaging $PACKAGE $VERSION-$REVISION ($ARCH)"

# ---------------------------------------------------------------- the payload
if [ -z "$WHEEL" ]; then
    WHEEL="$(ls -t dist/*.whl 2>/dev/null | head -1 || true)"
fi
if [ -z "$WHEEL" ] || [ ! -f "$WHEEL" ]; then
    say "building the wheel"
    python3 -m build --wheel --outdir dist >/dev/null
    WHEEL="$(ls -t dist/*.whl | head -1)"
fi
say "unpacking $(basename "$WHEEL")"
DIST_PACKAGES="$TREE/usr/lib/python3/dist-packages"
mkdir -p "$DIST_PACKAGES"
python3 -m zipfile -e "$WHEEL" "$DIST_PACKAGES/"

# Python files belong to the system interpreter here, not to a virtualenv.
find "$DIST_PACKAGES" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

say "installing the desktop entry, the icons and the AppStream metadata"
install -D -m 644 packaging/org.kde.dcs.desktop "$TREE/usr/share/applications/org.kde.dcs.desktop"
install -D -m 644 packaging/org.kde.dcs.metainfo.xml "$TREE/usr/share/metainfo/org.kde.dcs.metainfo.xml"
for size in 16 22 24 32 48 64 128 256 512; do
    install -D -m 644 "packaging/icons/hicolor/${size}x${size}/apps/org.kde.dcs.png" \
        "$TREE/usr/share/icons/hicolor/${size}x${size}/apps/org.kde.dcs.png"
done
install -D -m 644 packaging/icons/hicolor/scalable/apps/org.kde.dcs.svg \
    "$TREE/usr/share/icons/hicolor/scalable/apps/org.kde.dcs.svg"
install -D -m 644 packaging/icons/hicolor/scalable/apps/org.kde.dcs-symbolic.svg \
    "$TREE/usr/share/icons/hicolor/scalable/apps/org.kde.dcs-symbolic.svg"

say "installing the man page and the documentation"
mkdir -p "$TREE/usr/share/man/man1"
gzip -9n -c packaging/dcs.1 > "$TREE/usr/share/man/man1/dcs.1.gz"
chmod 644 "$TREE/usr/share/man/man1/dcs.1.gz"
install -D -m 644 README.md "$TREE/usr/share/doc/$PACKAGE/README.md"
install -D -m 644 CHANGELOG.md "$TREE/usr/share/doc/$PACKAGE/CHANGELOG.md"
install -D -m 644 LICENSE "$TREE/usr/share/doc/$PACKAGE/LICENSE"
cp -r docs "$TREE/usr/share/doc/$PACKAGE/docs"
find "$TREE/usr/share/doc/$PACKAGE/docs" -name "*.png" -delete  # the sources stay in the repository
gzip -9n -c CHANGELOG.md > "$TREE/usr/share/doc/$PACKAGE/changelog.gz"
install -D -m 644 packaging/debian/copyright "$TREE/usr/share/doc/$PACKAGE/copyright"

say "installing the commands"
install -D -m 755 packaging/debian/dcs.sh "$TREE/usr/bin/dcs"
install -D -m 755 packaging/debian/dcs-gui.sh "$TREE/usr/bin/dcs-gui"

say "installing the autostart entry (inactive until a watch mode is enabled)"
install -D -m 644 packaging/debian/org.kde.dcs.watch.desktop \
    "$TREE/etc/xdg/autostart/org.kde.dcs.watch.desktop"

# ---------------------------------------------------------------- the metadata
say "writing the package metadata"
mkdir -p "$TREE/DEBIAN"
sed -e "s/@VERSION@/$VERSION/" -e "s/@REVISION@/$REVISION/" \
    packaging/debian/control.in > "$TREE/DEBIAN/control"
install -m 755 packaging/debian/postinst "$TREE/DEBIAN/postinst"
install -m 755 packaging/debian/postrm "$TREE/DEBIAN/postrm"

# dpkg can generate this itself, but only with the maintainer scripts of dh;
# doing it here keeps the package verifiable without them.
( cd "$TREE" && find . -path ./DEBIAN -prune -o -type f -print0 \
    | xargs -0 md5sum | sed 's|\./||' > DEBIAN/md5sums )
chmod 644 "$TREE/DEBIAN/md5sums"

mkdir -p "$OUT"
DEB="$OUT/${PACKAGE}_${VERSION}-${REVISION}_${ARCH}.deb"
say "building $DEB"
dpkg-deb --root-owner-group --build "$TREE" "$DEB" >/dev/null

say "the package is $(( $(stat -c%s "$DEB") / 1024 )) KiB"
dpkg-deb --info "$DEB" | sed -n '1,12p'
printf '\n%s\n' "Install it with:  sudo apt install ./$DEB"
