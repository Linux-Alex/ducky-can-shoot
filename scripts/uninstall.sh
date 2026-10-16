#!/usr/bin/env bash
# Remove the user level installation created by scripts/install.sh.
set -euo pipefail

VENV="${VENV:-$HOME/.local/share/dcs/venv}"
BINDIR="${PREFIX:-$HOME/.local}/bin"
APPDIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONROOT="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"

# the watcher must not keep running after the application is gone
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/org.kde.dcs.watch.desktop"
rm -f "$AUTOSTART"
rm -rf "$VENV"
rm -f "$BINDIR/dcs" "$BINDIR/dcs-gui"
rm -f "$APPDIR/org.kde.dcs.desktop"
for size in 16 22 24 32 48 64 128 256 512; do
    rm -f "$ICONROOT/${size}x${size}/apps/org.kde.dcs.png"
    rmdir "$ICONROOT/${size}x${size}/apps" "$ICONROOT/${size}x${size}" 2>/dev/null || true
done
printf 'Removed the launcher, the desktop entry and %s\n' "$VENV"
printf 'Removed the USB watcher autostart entry (%s)\n' "$AUTOSTART"
printf 'Configuration and reports in ~/.config/dcs and ~/.local/share/dcs were left untouched.\n'
