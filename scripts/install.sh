#!/usr/bin/env bash
# Install Ducky Can Shoot for the current user, without root.
#
#   ./scripts/install.sh              install into ~/.local
#   PREFIX=/opt/dcs ./scripts/install.sh   install somewhere else
#
# The script creates a virtual environment that can see the distribution
# packages, so a system PyQt6 (python3-pyqt6, python3-pyqt6.qtqml) is reused
# instead of being downloaded again.
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local}"
VENV="${VENV:-$HOME/.local/share/dcs/venv}"
BINDIR="$PREFIX/bin"
APPDIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONDIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

say "checking the Python version"
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit("Ducky Can Shoot needs Python 3.11 or newer (found %s)" % sys.version.split()[0])
print("python", sys.version.split()[0])
PY

say "creating the virtual environment in $VENV"
python3 -m venv --system-site-packages "$VENV"

say "installing the package"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet .

say "linking the launcher into $BINDIR"
mkdir -p "$BINDIR"
ln -sf "$VENV/bin/dcs" "$BINDIR/dcs"
ln -sf "$VENV/bin/dcs-gui" "$BINDIR/dcs-gui"

say "installing the desktop entry and the icon"
mkdir -p "$APPDIR" "$ICONDIR"
install -m 644 packaging/org.kde.dcs.desktop "$APPDIR/org.kde.dcs.desktop"
install -m 644 packaging/icons/hicolor/scalable/apps/org.kde.dcs.svg "$ICONDIR/org.kde.dcs.svg"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPDIR" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true

say "writing a default configuration if there is none"
"$BINDIR/dcs" config init >/dev/null || true

cat <<EOF

Installed.

  start the GUI          : dcs
  check the environment  : dcs doctor
  list USB devices       : dcs devices
  scan something         : dcs scan /media/$USER/STICK

Optional but recommended, for signature based scanning:

  sudo apt install clamav clamav-daemon && sudo freshclam
  sudo systemctl enable --now clamav-daemon

Make sure $BINDIR is in your PATH.
EOF
