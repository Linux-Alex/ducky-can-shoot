#!/usr/bin/env python3
"""Start a release: bump the version and move the changelog, in one step.

    ./scripts/bump-version.py 0.2.0

What it changes, and why each one is needed:

* `src/dcs/version.py` - the single source of truth, used by the interface,
  the reports and the package metadata;
* `pyproject.toml` - the wheel and the sdist carry their own version;
* `packaging/org.kde.dcs.metainfo.xml` - software centres show the newest
  release from here, and a version that never moves looks abandoned;
* `CHANGELOG.md` - the section for the tag is what the release workflow turns
  into the release notes, so "Unreleased" has to become "0.2.0 - 2026-10-15"
  with a fresh empty "Unreleased" above it;
* `README.md` - the status badge, the sample transcript and the `.deb` name in
  the quick start all show the current version, and a README that advertises an
  older one is the first thing a visitor reads wrong.

The last commit before tagging is then a normal review step: run the tests, look
at the diff, commit, and push the tag.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

#: the checkout to work on; --repo overrides it, which is what the test suite does
#: so that it never touches the real one
REPO = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO / "src" / "dcs" / "version.py"
PYPROJECT = REPO / "pyproject.toml"
METAINFO = REPO / "packaging" / "org.kde.dcs.metainfo.xml"
CHANGELOG = REPO / "CHANGELOG.md"
README = REPO / "README.md"


def use_repo(path: Path) -> None:
    """Point every file at another checkout."""
    global REPO, VERSION_FILE, PYPROJECT, METAINFO, CHANGELOG, README
    REPO = path.resolve()
    VERSION_FILE = REPO / "src" / "dcs" / "version.py"
    PYPROJECT = REPO / "pyproject.toml"
    METAINFO = REPO / "packaging" / "org.kde.dcs.metainfo.xml"
    CHANGELOG = REPO / "CHANGELOG.md"
    README = REPO / "README.md"


VERSION_RE = re.compile(r'^__version__ = "([^"]+)"$', re.M)
PYPROJECT_RE = re.compile(r'^version = "([^"]+)"$', re.M)


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    print(f"  updated {path.relative_to(REPO)}")


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    if not args or "-h" in args or "--help" in args:
        print(__doc__.strip())
        return 0
    if "--repo" in args:
        where = args.index("--repo")
        if where + 1 >= len(args):
            fail("--repo needs a path")
        use_repo(Path(args[where + 1]))
        del args[where : where + 2]
    if len(args) != 1:
        fail("usage: bump-version.py VERSION [--repo PATH]")

    version = args[0].lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        fail(f"{version!r} is not a version like 0.2.0")

    today = dt.date.today().isoformat()
    print(f"starting release {version} ({today})")

    # 1. the source of truth
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        fail(f"cannot find __version__ in {VERSION_FILE}")
    previous = match.group(1)
    print(f"  was {previous}")
    write(VERSION_FILE, VERSION_RE.sub(f'__version__ = "{version}"', text))

    # 2. the README, wherever it names the version: the status badge, the sample
    # transcript and the .deb in the quick start.  Every occurrence is the old
    # version or nothing is, so a plain replacement cannot miss one silently.
    text = README.read_text(encoding="utf-8")
    if previous in text:
        write(README, text.replace(previous, version))
    else:
        print(f"  {README.relative_to(REPO)} does not name the version")

    # 3. the packaging metadata
    text = PYPROJECT.read_text(encoding="utf-8")
    if not PYPROJECT_RE.search(text):
        fail(f"cannot find version in {PYPROJECT}")
    write(PYPROJECT, PYPROJECT_RE.sub(f'version = "{version}"', text))

    # 4. AppStream: a new <release> entry at the top of the list
    text = METAINFO.read_text(encoding="utf-8")
    summary = re.search(
        r"## \[Unreleased\]\n(.*?)(?=\n## \[)", CHANGELOG.read_text(encoding="utf-8"), re.S
    )
    entry = (
        f'    <release version="{version}" date="{today}">\n'
        f"      <description>\n"
        f"        <p>See the changelog for what changed in {version}.</p>\n"
        f"      </description>\n"
        f"    </release>\n"
    )
    if f'version="{version}"' in text:
        print(f"  {METAINFO.relative_to(REPO)} already lists {version}")
    else:
        write(METAINFO, text.replace("  <releases>\n", "  <releases>\n" + entry, 1))
    if summary is None:  # pragma: no cover - the changelog always has the section
        fail("the changelog has no [Unreleased] section")

    # 5. the changelog: name the section and open a fresh one
    text = CHANGELOG.read_text(encoding="utf-8")
    released = text.replace(
        "## [Unreleased]\n",
        f"## [Unreleased]\n\nNothing yet.\n\n## [{version}] - {today}\n",
        1,
    )
    # the link targets at the bottom follow the same pattern
    released = re.sub(
        r"^\[Unreleased\]: (\S+)/compare/v([^.]+\.)+[^.]+/?(?:\.\.\.HEAD)?$",
        lambda m: (
            f"[Unreleased]: {m.group(1)}/compare/v{version}...HEAD\n"
            f"[{version}]: {m.group(1)}/releases/tag/v{version}"
        ),
        released,
        flags=re.M,
    )
    write(CHANGELOG, released)

    print(
        "\nnext:\n"
        f"  make test                            # the suite, before anything else\n"
        f"  git diff                             # review it\n"
        f'  git commit -am "Ducky Can Shoot {version}"\n'
        f"  git push\n"
        f'  git tag -a v{version} -m "Ducky Can Shoot {version}" && git push origin v{version}\n'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
