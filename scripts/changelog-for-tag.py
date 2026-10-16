#!/usr/bin/env python3
"""Write the body of a GitHub release: the changelog section plus install steps.

    python3 scripts/changelog-for-tag.py v0.2.1 --output release-notes.md

The changelog part is extracted from CHANGELOG.md; the installation part is
generated here, with the file names of this version in it.  `--changelog-only`
leaves the installation part out.

Everything is plain text written with Python: a release body may contain fenced
code blocks, and a shell heredoc would happily *run* what is between the
backticks instead of copying it (that is how the first two releases ended up
with the output of "apt install" and "pip install" where the commands should
have been).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: where the project lives; read from the version module so there is one place
VERSION_FILE = REPO / "src" / "dcs" / "version.py"

INSTALLATION = """## Installation

The Debian package pulls in Python, PyQt6, Kirigami and the Qt SVG plugins;
ClamAV is recommended but optional.

### Debian, Ubuntu, Kubuntu and anything else with apt

```bash
sudo apt install ./ducky-can-shoot_{version}-1_all.deb
```

### Any distribution with Python 3.11 and PyQt6

```bash
pip install ducky_can_shoot-{version}-py3-none-any.whl
```

### From the source tree, without root

```bash
git clone {homepage}.git
cd ducky-can-shoot && ./scripts/install.sh
```

Then start the interface with `dcs` and check the environment with
`dcs doctor`.  **New to Ducky Can Shoot?** The
[README]({homepage}#quick-start) walks through the first run in a few minutes.
"""


def section_for(text: str, version: str) -> str:
    """The body of the "## [version]" section, without the heading itself."""
    pattern = re.compile(rf"^## \[{re.escape(version)}\].*?$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    rest = text[match.end() :]
    end = rest.find("\n## ")
    return (rest if end == -1 else rest[:end]).strip()


def homepage() -> str:
    """The project URL, as the application itself knows it."""
    try:
        text = VERSION_FILE.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - only without a checkout
        return "https://github.com/Linux-Alex/ducky-can-shoot"
    match = re.search(r'^APP_HOMEPAGE = "([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "https://github.com/Linux-Alex/ducky-can-shoot"


def notes(version: str, changelog: Path, *, installation: bool = True) -> str:
    """The complete release body for *version*."""
    body = section_for(changelog.read_text(encoding="utf-8"), version)
    if not body:
        print(f"warning: no section for {version} in {changelog}", file=sys.stderr)
        body = f"Release {version}. See CHANGELOG.md for the details."

    parts = [body, "---"]
    if installation:
        parts.append(
            INSTALLATION.format(version=version, homepage=homepage()).strip()
        )
    parts.append(
        "---\n\n"
        f"Everything in this release: [CHANGELOG.md]({homepage()}/blob/main/CHANGELOG.md) · "
        f"[documentation]({homepage()}/tree/main/docs) · "
        f"[report a problem]({homepage()}/issues)"
    )
    return "\n\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="version to extract, with or without a leading v")
    parser.add_argument("--changelog", default=str(REPO / "CHANGELOG.md"))
    parser.add_argument("--output", help="write here instead of standard output")
    parser.add_argument(
        "--changelog-only",
        action="store_true",
        help="leave the installation section out",
    )
    args = parser.parse_args()

    body = notes(
        args.version.lstrip("v"),
        Path(args.changelog),
        installation=not args.changelog_only,
    )
    if args.output:
        Path(args.output).write_text(body, encoding="utf-8")
    else:
        print(body, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
