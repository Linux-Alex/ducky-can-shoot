#!/bin/sh
# Build a self contained AppImage: Ducky Can Shoot, the Qt/Kirigami stack it
# draws with, and ClamAV with a signature database inside the image.
#
# The image is meant to run on a machine that has none of that installed, so
# the build copies the runtime instead of linking to the host's.  Run it on the
# distribution the image should be based on (Ubuntu 26.04 in the release
# workflow); the required packages are listed below and checked at the start.
#
#     sudo apt install python3-pyqt6 python3-pyqt6.qtqml python3-pyqt6.qtquick \
#         python3-requests qml6-module-org-kde-kirigami qml6-module-qtquick-controls \
#         qml6-module-qtquick-layouts qml6-module-qtquick-dialogs \
#         qml6-module-qtquick-templates qml6-module-qtqml-workerscript \
#         qt6-svg-plugins libqt6svg6 clamav
#     ./scripts/build-appimage.sh
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

VERSION=$(PYTHONPATH=src python3 -c 'from dcs.version import __version__; print(__version__)')
ARCH=$(uname -m)
APPDIR="${APPDIR:-$REPO/build/DuckyCanShoot.AppDir}"
OUT="${OUT:-$REPO/dist/DuckyCanShoot-${VERSION}-${ARCH}.AppImage}"
TOOL="${APPIMAGETOOL:-$REPO/build/appimagetool-${ARCH}.AppImage}"
LIBDIR="${LIBDIR:-/usr/lib/${ARCH}-linux-gnu}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# appimagetool extracts itself into TMPDIR; a fresh one keeps the build from
# tripping over leftovers of an earlier run by another user.
TMPDIR="$(mktemp -d)"
export TMPDIR
trap 'rm -rf "$TMPDIR"' EXIT

say "checking the build packages"
missing=""
for module in PyQt6.QtQml PyQt6.QtQuick requests; do
    python3 -c "import $module" 2>/dev/null || missing="$missing python3-$module"
done
for path in "$LIBDIR/qt6/qml/org/kde/kirigami" "$LIBDIR/libQt6Core.so.6"; do
    [ -e "$path" ] || missing="$missing $(basename "$path")"
done
command -v clamscan >/dev/null || missing="$missing clamav"
if [ -n "$missing" ]; then
    say "missing build dependencies:$missing"
    exit 1
fi

say "laying out ${APPDIR#$REPO/}"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/$ARCH-linux-gnu" "$APPDIR/usr/share/icons" \
         "$APPDIR/usr/lib/python3/dist-packages" "$APPDIR/usr/lib/dcs-python" \
         "$APPDIR/usr/share/dcs/clamav"

install -m 755 packaging/appimage/AppRun "$APPDIR/AppRun"
ln -sf AppRun "$APPDIR/usr/bin/dcs"
install -m 644 packaging/org.kde.dcs.desktop "$APPDIR/org.kde.dcs.desktop"
install -m 644 packaging/icons/hicolor/256x256/apps/org.kde.dcs.png "$APPDIR/org.kde.dcs.png"
install -m 644 packaging/icons/hicolor/256x256/apps/org.kde.dcs.png "$APPDIR/.DirIcon"
cp -a packaging/icons/hicolor "$APPDIR/usr/share/icons/"

say "copying Python and the application"
PYTHON_BIN=$(command -v python3.14 || command -v python3)
cp -a "$PYTHON_BIN" "$APPDIR/usr/bin/"
# The interpreter looks for its standard library under the prefix it was built
# for; AppRun sets PYTHONHOME to usr/, so this is where it has to be.
cp -a "/usr/lib/python3.14" "$APPDIR/usr/lib/" 2>/dev/null || cp -a "/usr/lib/python3.$(python3 -V | cut -d. -f2)" "$APPDIR/usr/lib/python3"
cp -a "$LIBDIR"/libpython3*.so* "$APPDIR/usr/lib/$ARCH-linux-gnu/" 2>/dev/null || true
# The application itself is pure Python: a copy is enough, no pip involved.
cp -a src/dcs "$APPDIR/usr/lib/dcs-python/"
# requests and its dependencies come from the distribution (the free upload
# hosts use them); anything else is not needed by the core.
for module in requests urllib3 certifi idna charset_normalizer; do
    if [ -d "/usr/lib/python3/dist-packages/$module" ]; then
        cp -a "/usr/lib/python3/dist-packages/$module" "$APPDIR/usr/lib/python3/dist-packages/"
    elif ls "/usr/lib/python3/dist-packages/${module}"*.py >/dev/null 2>&1; then
        cp -a "/usr/lib/python3/dist-packages/${module}"*.py "$APPDIR/usr/lib/python3/dist-packages/"
    fi
done
cp -a "/usr/lib/python3/dist-packages/PyQt6" "$APPDIR/usr/lib/python3/dist-packages/"
cp -a "/usr/lib/python3/dist-packages/PyQt6"*.dist-info "$APPDIR/usr/lib/python3/dist-packages/" 2>/dev/null || true
cp -a "/usr/lib/python3/dist-packages/PyQt6"*.pth "$APPDIR/usr/lib/python3/dist-packages/" 2>/dev/null || true

say "copying Qt, Kirigami and the rest of the runtime"
# Qt's own modules, its QML tree and its plugins, then the KDE frameworks the
# Kirigami module links against.  Everything else comes from the library copy
# below, which is the whole runtime directory minus build artefacts.
cp -a "$LIBDIR/qt6" "$APPDIR/usr/lib/$ARCH-linux-gnu/"
cp -a "$LIBDIR"/libQt6*.so* "$APPDIR/usr/lib/$ARCH-linux-gnu/"
for family in libKF6*.so* libkdeinit6*.so* libkworkspace6*.so*; do
    cp -a "$LIBDIR"/$family "$APPDIR/usr/lib/$ARCH-linux-gnu/" 2>/dev/null || true
done
cp -a "$LIBDIR"/libQt6* "$APPDIR/usr/lib/$ARCH-linux-gnu/" 2>/dev/null || true
tar -C /usr/lib -cf - "$ARCH-linux-gnu" 2>/dev/null | tar -C "$APPDIR/usr/lib" -xf - --overwrite 2>/dev/null || true
# Drop what only a compiler or a package build ever needs.
find "$APPDIR/usr/lib" -name "*.a" -delete
find "$APPDIR/usr/lib" -name "*.la" -delete
find "$APPDIR/usr/lib" -type d -name pkgconfig -prune -exec rm -rf {} + 2>/dev/null || true
find "$APPDIR/usr/lib" -type d -name cmake -prune -exec rm -rf {} + 2>/dev/null || true

say "copying ClamAV and fetching a signature database"
for tool in clamscan freshclam clamdscan; do
    # clamdscan only exists with clamav-daemon; the image scans with clamscan,
    # which needs no daemon at all
    if [ -x "/usr/bin/$tool" ]; then
        install -m 755 "/usr/bin/$tool" "$APPDIR/usr/bin/"
    fi
done
cp -a "$LIBDIR"/libclamav*.so* "$LIBDIR"/libfreshclam*.so* "$LIBDIR"/libclammspack*.so* \
      "$LIBDIR"/libcrypto.so* "$LIBDIR"/libssl.so* "$APPDIR/usr/lib/$ARCH-linux-gnu/" 2>/dev/null || true
cp -a /etc/clamav "$APPDIR/usr/etc" 2>/dev/null || { mkdir -p "$APPDIR/usr/etc"; cp -a /etc/clamav "$APPDIR/usr/etc/"; }
# freshclam drops to the "clamav" user when run as root, which cannot write into
# the build tree; --user=root keeps the database where the image will find it.
# As a normal user there is no privilege drop, and --user must not be passed.
FRESHCLAM_USER=""
if [ "$(id -u)" = "0" ]; then FRESHCLAM_USER="--user=root"; fi
if ! "$APPDIR/usr/bin/freshclam" $FRESHCLAM_USER \
        --datadir="$APPDIR/usr/share/dcs/clamav" \
        --log="$APPDIR/usr/share/dcs/clamav/freshclam.log" --stdout 2>&1 | tail -5; then
    say "warning: freshclam reported a problem while fetching the database"
fi
if ! ls "$APPDIR/usr/share/dcs/clamav"/*.cvd >/dev/null 2>&1; then
    say "error: no signature database was fetched; a bundled ClamAV would find nothing"
    exit 1
fi

if [ "${SKIP_PACKAGE:-0}" != "0" ]; then
    say "SKIP_PACKAGE is set: the AppDir is ready at ${APPDIR#$REPO/}, no image was written"
    exit 0
fi

say "writing the SquashFS image"
find "$APPDIR/usr/lib" -name "*.a" -delete
if [ ! -x "$TOOL" ]; then
    mkdir -p "$(dirname "$TOOL")"
    say "downloading appimagetool"
    curl -fsSL -o "$TOOL" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
    chmod +x "$TOOL"
fi
mkdir -p "$(dirname "$OUT")"
ARCH="$ARCH" "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
chmod 755 "$OUT"
say "built $OUT ($(du -h "$OUT" | cut -f1))"
