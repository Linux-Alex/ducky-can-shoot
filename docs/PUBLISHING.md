# Publishing and maintaining the repository

This is the checklist for putting Ducky Can Shoot on GitHub and keeping it
healthy afterwards. It is written for the maintainer of this repository; nothing
here is needed to *use* DCS.

## First push

```bash
cd ducky-can-shoot
git remote add origin git@github.com:Linux-Alex/ducky-can-shoot.git
git push -u origin main
```

Everything the front page needs is already in the repository: the README starts
with a banner and badges, the six screenshots are in `docs/images/`, the license,
the code of conduct, the security policy, the issue forms and two workflows are
in place. The first push is enough for a presentable project.

## Repository settings worth changing by hand

GitHub does not read most of this from the repository, so set it once in the web
interface (Settings):

| Where | Value |
| --- | --- |
| Description | USB malware triage for Linux: scan a medium, archive it, upload the image and email the verdict |
| Website | leave empty, or point at your own page about the project |
| Topics | `usb` `malware` `antivirus` `security-tools` `clamav` `python` `qt` `kirigami` `kde` `forensics` `linux` |
| Social preview | upload `docs/images/banner.png` (already 1280x640, the size GitHub asks for) |
| Features | enable **Issues** and **Discussions**; disable **Wiki** and **Projects** unless you want them |
| Pull requests | allow squash merging; enable "automatically delete head branches" |
| Releases | nothing to set up: pushing a tag builds and publishes them |

To make the repository show the right language mix, nothing needs doing:
`.gitattributes` marks the QML interface as its own language and keeps the
documentation out of the statistics.

## First release

```bash
# CHANGELOG.md already has a section for 0.1.0
git tag -a v0.1.0 -m "Ducky Can Shoot 0.1.0"
git push origin v0.1.0
```

The Release workflow builds the sdist and the wheel, checks that the wheel really
contains the QML interface and the hash list, and attaches both to a GitHub
release whose body comes from the matching CHANGELOG section (see
`scripts/changelog-for-tag.py`). For the next release, add a section for the new
version to CHANGELOG.md first and tag it.

## Continuous integration

| Workflow | What it does |
| --- | --- |
| `.github/workflows/ci.yml` | the whole test suite against the distribution's Qt (including the QML interface), the same suite on Python 3.11 to 3.13 without Qt, and a ruff job |
| `.github/workflows/release.yml` | on a tag: build, verify the wheel, create the release |

Actions must be allowed once: Settings, Actions, General, "Allow all actions".
The workflows need no secrets; the tests never touch the network.

## What to check after a change to the interface

Screenshots are documentation, so they go stale. Re-render them with:

```bash
make screenshots          # or: python3 scripts/make-screenshots.py
```

The script starts the real interface offscreen, drives it with a synthetic medium
(EICAR, a Duckyscript payload, an autorun.inf, a double extension, PowerShell and
batch samples) and writes the PNGs into `docs/images/`. It exits non-zero if QML
prints any warning, which makes it a useful smoke test on its own.

## Known gaps before 1.0

- No packaged artefacts yet (AppImage, Flatpak, distribution packages): the wheel
  and `scripts/install.sh` are what exists. See `docs/ROADMAP.md`.
- No translations; the interface is English.
- No signature for the released artefacts. If you want provenance, add
  `actions/attest-build-provenance` to the release workflow.
- The free upload hosts in `docs/research/cloud-upload-apis.md` were verified in
  January 2026. They change without notice: re-run `dcs providers` and the checks
  in that document before claiming a host works.
