#!/usr/bin/env bash
# Install Ducky Can Shoot for the current user, without root.
#
#   ./scripts/install.sh              install into ~/.local
#   PREFIX=/opt/dcs ./scripts/install.sh   install somewhere else
#
# The optional USB watcher - the thing that opens or notifies you when a stick
# is plugged in - is off unless you ask for it:
#
#   ./scripts/install.sh --watch=notify     off | notify | open | open_run
#   ./scripts/install.sh --no-watch         same as --watch=off
#   WATCH_MODE=open_run ./scripts/install.sh
#
# Without an option on the command line the script asks, and "do nothing" is
# the default answer.
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

#: off (the default) | notify | open | open_run
WATCH_MODE="${WATCH_MODE:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --watch=*) WATCH_MODE="${1#*=}" ;;
        --watch) WATCH_MODE="${2:-}"; shift ;;
        --no-watch) WATCH_MODE="off" ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

case "$WATCH_MODE" in
    ""|off|notify|open|open_run) ;;
    *) printf 'watch mode must be off, notify, open or open_run (got %s)\n' "$WATCH_MODE" >&2
       exit 2 ;;
esac

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

# ---------------------------------------------------------------- USB watcher
if [ -z "$WATCH_MODE" ] && [ -t 0 ]; then
    cat <<'MENU'

Watch for newly plugged media?  Nothing is watched unless you choose a mode.

  1) do nothing (default)
  2) notify me, with a button that opens Ducky Can Shoot on the new medium
  3) open Ducky Can Shoot with the new medium already selected
  4) open it and start the job automatically (scan, archive, upload, email)

MENU
    printf 'Choose 1-4 [1]: '
    read -r answer || answer=""
    case "$answer" in
        2) WATCH_MODE="notify" ;;
        3) WATCH_MODE="open" ;;
        4) WATCH_MODE="open_run" ;;
        *) WATCH_MODE="off" ;;
    esac
fi
WATCH_MODE="${WATCH_MODE:-off}"

WATCH_SUMMARY="off"

if [ "$WATCH_MODE" != "off" ]; then
    say "enabling the USB watcher (mode: $WATCH_MODE)"
    "$BINDIR/dcs" config set watch.mode "$WATCH_MODE" >/dev/null
    "$BINDIR/dcs" config set watch.autostart true >/dev/null
    # The entry is written by DCS itself, so it is exactly what the settings
    # page would write and can be turned off again from there.
    DCS_EXEC="$BINDIR/dcs" "$VENV/bin/python" - <<'PY'
import os

from dcs.core import autostart

print("autostart entry:", autostart.enable(os.environ["DCS_EXEC"]))
PY
    WATCH_SUMMARY="$WATCH_MODE (starts with your session)"
else
    say "the USB watcher stays off"
    "$BINDIR/dcs" config set watch.mode off >/dev/null 2>&1 || true
    "$BINDIR/dcs" config set watch.autostart false >/dev/null 2>&1 || true
    DCS_STATE=off "$VENV/bin/python" - <<'PY' 2>/dev/null || true
from dcs.core import autostart

autostart.disable()
PY
fi

cat <<EOF

Installed.

  start the GUI          : dcs
  check the environment  : dcs doctor
  list USB devices       : dcs devices
  scan something         : dcs scan /media/$USER/STICK
  USB watcher            : $WATCH_SUMMARY
                           change it any time in Settings, or with:
                           dcs config set watch.mode notify|open|open_run

Optional but recommended, for signature based scanning:

  sudo apt install clamav clamav-daemon && sudo freshclam
  sudo systemctl enable --now clamav-daemon

Make sure $BINDIR is in your PATH.
EOF
