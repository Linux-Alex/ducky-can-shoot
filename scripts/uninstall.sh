#!/usr/bin/env bash
# Remove the user level installation created by scripts/install.sh.
set -euo pipefail

VENV="${VENV:-$HOME/.local/share/dcs/venv}"
BINDIR="${PREFIX:-$HOME/.local}/bin"
APPDIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONDIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

rm -rf "$VENV"
rm -f "$BINDIR/dcs" "$BINDIR/dcs-gui"
rm -f "$APPDIR/org.kde.dcs.desktop"
rm -f "$ICONDIR/org.kde.dcs.svg"
printf 'Removed the launcher, the desktop entry and %s\n' "$VENV"
printf 'Configuration and reports in ~/.config/dcs and ~/.local/share/dcs were left untouched.\n'
