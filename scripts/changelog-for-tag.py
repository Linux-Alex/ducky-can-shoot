#!/usr/bin/env python3
"""Print the CHANGELOG section for a version, for a GitHub release body.

    python3 scripts/changelog-for-tag.py 0.1.0 --output release-notes.md

Falls back to a one line note when the version has no section yet, so a release
is never blocked by a missing changelog entry (it prints a warning instead).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def section_for(text: str, version: str) -> str:
    """The body of the "## [version]" section, without the heading itself."""
    pattern = re.compile(rf"^## \[{re.escape(version)}\].*?$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    rest = text[match.end() :]
    end = rest.find("\n## ")
    return (rest if end == -1 else rest[:end]).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="version to extract, with or without a leading v")
    parser.add_argument("--changelog", default=str(REPO / "CHANGELOG.md"))
    parser.add_argument("--output", help="write here instead of standard output")
    args = parser.parse_args()

    version = args.version.lstrip("v")
    text = Path(args.changelog).read_text(encoding="utf-8")
    body = section_for(text, version)
    if not body:
        print(f"warning: no section for {version} in {args.changelog}", file=sys.stderr)
        body = f"Release {version}. See CHANGELOG.md for the details."
    if args.output:
        Path(args.output).write_text(body + "\n", encoding="utf-8")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
