#!/usr/bin/env bash
# Build every artefact this machine can build, and print the exact commands that
# publish them.
#
#   ./scripts/release.sh                    wheel, sdist, .deb, SHA256SUMS
#   ./scripts/release.sh --check            run pytest, ruff and mypy first
#   ./scripts/release.sh --appimage         also build the self contained AppImage
#   ./scripts/release.sh --tag              create the annotated v<version> tag
#   ./scripts/release.sh --push             push the commit and the tag (implies --tag)
#
# The AppImage needs an Ubuntu 26.04-like host (Python 3.14, Qt 6, Kirigami 6,
# ClamAV with a signature database) and is rebuilt by the release workflow on
# every tag anyway, so it is optional here.
#
# Releasing from a Windows machine? src/dcs/version.py and the changelog are
# bumped on a Linux checkout; scripts/release-windows.ps1 then pushes the tag
# and waits for the workflow. This script is the Linux half.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

CHECK=no
APPIMAGE=no
TAG=no
PUSH=no
while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=yes ;;
        --appimage) APPIMAGE=yes ;;
        --tag) TAG=yes ;;
        --push) TAG=yes; PUSH=yes ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

VERSION="$(PYTHONPATH=src python3 -c 'from dcs.version import __version__; print(__version__)')"
TAGNAME="v$VERSION"
say "release $VERSION ($TAGNAME), from $here"

# ------------------------------------------------------------------ the tree
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    say "the tree has uncommitted changes - build without --tag, then commit them"
    if [ "$TAG" = yes ]; then
        die "refusing to tag a dirty tree"
    fi
fi

if [ "$TAG" = yes ] && [ -n "$(git tag --list "$TAGNAME")" ]; then
    die "$TAGNAME already exists; bump the version or delete the tag deliberately"
fi

# ------------------------------------------------------------------ the gates
if [ "$CHECK" = yes ]; then
    say "running the test suite"
    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 -m pytest -q

    command -v ruff >/dev/null || die "ruff is not installed (pipx install ruff, or use uvx ruff@<version>)"
    say "checking the style"
    ruff check .

    command -v mypy >/dev/null || die "mypy is not installed (python3 -m pip install --user mypy)"
    say "checking the types"
    PYTHONPATH=src mypy
fi

# ------------------------------------------------------------------ artefacts
# dist/ is the release payload: everything in it is attached and checksummed,
# so a stale file from an earlier release must not survive.
rm -rf dist
mkdir -p dist

say "building the wheel and the sdist"
python3 -m build --outdir dist

say "building the Debian package"
./scripts/build-deb.sh

if [ "$APPIMAGE" = yes ]; then
    say "building the AppImage"
    ./scripts/build-appimage.sh
fi

say "writing SHA256SUMS"
( cd dist && sha256sum ./* > SHA256SUMS && cat SHA256SUMS )

say "artefacts in dist/"
ls -l dist

# ------------------------------------------------------------------ the tag
if [ "$PUSH" = yes ] && [ "$TAG" = yes ]; then
    say "pushing the commit to origin"
    git push origin HEAD
fi

if [ "$TAG" = yes ]; then
    say "creating the annotated tag $TAGNAME"
    git tag -a "$TAGNAME" -m "Ducky Can Shoot $VERSION"
fi

if [ "$PUSH" = yes ]; then
    say "pushing the tag (this starts the release workflow)"
    git push origin "$TAGNAME"
    say "watch it with: gh run watch \$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
    say "or from Windows: pwsh -File scripts/release-windows.ps1"
else
    cat <<EOF

next:
  git add -A && git commit -m "Ducky Can Shoot $VERSION"   # if not committed yet
  git push
  git tag -a $TAGNAME -m "Ducky Can Shoot $VERSION"
  git push origin $TAGNAME        # <- this is the release
EOF
fi
