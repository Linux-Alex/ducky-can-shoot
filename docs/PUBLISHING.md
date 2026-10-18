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
| Icon | nothing to do: the artwork is the two illustrations in `docs/images`, `scripts/make-icons.py` renders the nine theme sizes from them into `packaging/icons/hicolor`, and `scripts/install.sh` installs all of it |
| Features | enable **Issues** and **Discussions**; disable **Wiki** and **Projects** unless you want them |
| Pull requests | allow squash merging; enable "automatically delete head branches" |
| Releases | nothing to set up: pushing a tag builds and publishes them |

To make the repository show the right language mix, nothing needs doing:
`.gitattributes` marks the QML interface as its own language and keeps the
documentation out of the statistics.

## Making a release

Everything is driven by the tag. One command moves the version, then two push the
tag and the [release workflow](../.github/workflows/release.yml) does the rest:

```bash
./scripts/bump-version.py 0.2.0     # or: make version VERSION=0.2.0
```

That one command

- bumps `__version__` in `src/dcs/version.py` (the single source of truth),
- bumps `version` in `pyproject.toml`,
- adds a `<release version="0.2.0" date="...">` entry at the top of
  `packaging/org.kde.dcs.metainfo.xml`, which is what software centres show,
- turns the changelog's `[Unreleased]` section into `## [0.2.0] - <date>`, opens
  a fresh empty `[Unreleased]` above it and repoints the comparison links,
- moves the version shown in `README.md`: the status badge, the `.deb` name in
  the quick start and the sample transcript. A README that advertises the
  previous release is the first thing a visitor reads wrong.

Then:

```bash
make test                           # the suite, before anything else
git diff                            # review what moved
git commit -am "Ducky Can Shoot 0.2.0"
git push

git tag -a v0.2.0 -m "Ducky Can Shoot 0.2.0"
git push origin v0.2.0              # <- this is the release
```

The workflow then

1. builds the sdist and the wheel and checks that the wheel really carries the
   QML interface,
2. builds the `.deb` (`scripts/build-deb.sh`), extracts it and runs the packaged
   `dcs` from the extracted tree, so a package that cannot find its interface
   fails the release instead of reaching a user,
3. builds the **AppImage** on Ubuntu 26.04 (`scripts/build-appimage.sh`): it
   bundles Python, Qt, Kirigami, ClamAV and a signature database, and the job
   runs it in a bare `ubuntu:26.04` container that has none of those, scanning
   an EICAR sample - a bundled database that cannot scan fails the release,
4. writes `SHA256SUMS` over everything it built,
5. **attests** the artefacts with `actions/attest-build-provenance`, so each file
   carries a signed statement that this workflow built it from this commit,
6. creates a **GitHub release** named after the tag, whose body is the changelog
   section for that version plus an "Installation" section
   (`scripts/changelog-for-tag.py`), with `ducky_can_shoot-0.2.0-py3-none-any.whl`,
   `ducky_can_shoot-0.2.0.tar.gz`, `ducky-can-shoot_0.2.0-1_all.deb`,
   `DuckyCanShoot-0.2.0-x86_64.AppImage` and `SHA256SUMS` attached.

Checking what you downloaded:

```bash
sha256sum --check SHA256SUMS                      # against the checksum file
gh attestation verify DuckyCanShoot-*.AppImage -R Linux-Alex/ducky-can-shoot
```

The attestation is the interesting one: it says which workflow, which commit and
which repository produced the file, and `gh` verifies the signature against
GitHub's trust root without asking anybody to trust a release note.

You can watch it under **Actions**; when it finishes, the files are on
**Releases**. Nothing else to upload, and no local build step is required.

### Doing it by hand instead

If you would rather not use the workflow: run `make deb` and `python3 -m build`,
then on GitHub open **Releases -> Draft a new release**, pick or create the tag
(`v0.2.0`), write the notes, drag `dist/ducky_can_shoot-0.2.0-py3-none-any.whl`,
`dist/ducky_can_shoot-0.2.0.tar.gz`, `dist/ducky-can-shoot_0.2.0-1_all.deb`,
`dist/DuckyCanShoot-0.2.0-x86_64.AppImage` and the `SHA256SUMS` file
(`cd dist && sha256sum * > SHA256SUMS`) into the attachment box and press
**Publish release**. A hand-made release carries no build provenance: the
attestation is created by the workflow, so use it when you want verifiable
downloads. Attaching the files by hand to a tag that also triggered the
workflow would publish each artefact twice, so pick one route.

### The very first release

The 0.1.0 section already exists in the changelog, so the first release is just:

```bash
git tag -a v0.1.0 -m "Ducky Can Shoot 0.1.0"
git push origin v0.1.0
```

Use `./scripts/bump-version.py` from the second release onwards.

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

The script starts the real interface offscreen, drives it with two synthetic
media (one clean, one carrying EICAR, a Duckyscript payload, an autorun.inf, a
double extension, PowerShell and batch samples) and writes the PNGs into
`docs/images/`. It exits non-zero if QML prints any warning, which makes it a
useful smoke test on its own.

Two things keep the pictures honest. The clean medium holds no samples at all, so
the "clean verdict" shot cannot show a threat list, and the script refuses to save
a shot whose run came out differently from its caption (a clean run has to be
clean with no findings and a share link; an infected run has to have findings and
no link). The upload host is replaced by a stand-in, so re-rendering the
documentation never publishes a file.

## Known gaps before 1.0

- No Flatpak yet. There is a `.deb`, a wheel, an sdist and a self-contained
  AppImage; a Flatpak would need the USB and daemon access worked out first.
- No translations; the interface is English.
- The artefacts carry a build provenance attestation, not a GPG signature: the
  provenance covers "which workflow, which commit", not "signed by a person with
  a key you already trust". A signed `.deb` and a signed AppImage are still open
  (see `docs/ROADMAP.md`).
- The free upload hosts in `docs/research/cloud-upload-apis.md` were verified in
  January 2026. They change without notice: re-run `dcs providers` and the checks
  in that document before claiming a host works.
