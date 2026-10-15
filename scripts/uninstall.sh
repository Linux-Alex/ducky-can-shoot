#!/usr/bin/env bash
# Remove the user level installation created by scripts/install.sh.
set -euo pipefail

VENV="${VENV:-$HOME/.local/share/dcs/venv}"
BINDIR="${PREFIX:-$HOME/.local}/bin"
APPDIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONDIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

# the watcher must not keep running after the application is gone
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/org.kde.dcs.watch.desktop"
rm -f "$AUTOSTART"
rm -rf "$VENV"
rm -f "$BINDIR/dcs" "$BINDIR/dcs-gui"
rm -f "$APPDIR/org.kde.dcs.desktop"
rm -f "$ICONDIR/org.kde.dcs.svg"
printf 'Removed the launcher, the desktop entry and %s\n' "$VENV"
printf 'Removed the USB watcher autostart entry (%s)\n' "$AUTOSTART"
printf 'Configuration and reports in ~/.config/dcs and ~/.local/share/dcs were left untouched.\n'
