# Roadmap

This document records what the project deliberately is today, what it is
missing, and what the next milestone contains. It is a working plan, not a
promise: no date is attached to anything, and an item stays listed here until it
ships.

Two rules keep it useful:

1. **Completed work leaves this file.** If a release note says something is
   done, the roadmap must not still describe it as missing. The roadmap is
   reviewed as part of every release (see
   [`docs/PUBLISHING.md`](PUBLISHING.md)).
2. **Committed work states what "done" means.** An item that cannot be checked
   off is a wish, and wishes belong in section 6, not in a milestone.

The latest release is **0.2.4**. Everything in section 1 shipped in it and is
covered by the test suite; section 2 is the honest list of what 0.2.4 cannot do;
section 3 is the next milestone, and its two features are already implemented on
`main` for the 0.3 releases (see `CHANGELOG.md`, "Unreleased").

---

## 1. What 0.2.4 provides

| Area | What ships today |
| --- | --- |
| **Scanning** | ClamAV through `clamdscan` (daemon, `--fdpass`) or `clamscan`, chosen automatically and reported in the result; a dependency-free heuristic engine with the evidence behind every finding; optional YARA rules (`scan.engines`, `--yara-rules`) and hash intelligence (`scan.intel`, VirusTotal / MetaDefender, off until asked twice). |
| **YARA** | Engine (`src/dcs/core/scanner/yara.py`), rule packs through `dcs yara update` (Neo23x0 signature-base by default, compile-checked with the local `yara` binary), `dcs yara status`. |
| **Verdict** | clean / suspicious / infected / error / cancelled, derived from finding severities (`scan.blocking_severity`). An engine that could not run is a warning, never a clean verdict. |
| **Archiving** | zip or 7z through the external tools (streaming, per-file progress), optional volume splitting, free-space pre-check, SHA-256 of the image, Python `zipfile` fallback. |
| **Uploading** | Seven anonymous hosts behind one adapter interface (gofile, litterbox, temp.sh, x0.at, catbox, tmpfiles, pixeldrain), streaming uploads with byte progress, retries, host checksum verification. `upload.upload_when` refuses to publish a non-clean image. |
| **Reports** | Self-contained HTML (artwork embedded as a data URI), plain text, findings CSV, JSON; written atomically into `report.directory`. |
| **Email** | HTML and text body, inline artwork, verdict, numbers, findings, share link when safe, report attached, dry-run to disk. |
| **Interface** | Kirigami/Qt Quick with seven live stages, counters, throughput chart, findings as they appear, log pane; settings page; update check and self-update; ClamAV installer and signature updater. |
| **Media handling** | lsblk discovery, udisks2 mounting without root, unmount and "safe to unplug" when a job ends; Dolphin service-menu actions (`Scan with Ducky Can Shoot`, `Open in Ducky Can Shoot`); optional USB watcher (off by default) with four reaction modes and an autostart entry. |
| **CLI** | `dcs scan`, `run`, `devices`, `watch`, `doctor`, `config`, `engines`, `providers`, `yara`, `signatures`, `update`, `install-clamav`, `gui`, `version`, with JSON output and stable exit codes. |
| **Packaging** | Debian package (`make deb`), AppImage (`scripts/build-appimage.sh`), wheel and sdist; `SHA256SUMS` and a signed build-provenance attestation on every release. |
| **Tests** | 910 hermetic tests: no network, no real device, fake ClamAV front ends, plus one explicitly marked integration test for the scheduled real-ClamAV workflow; CI on Python 3.11–3.14 plus a real Qt/Kirigami environment. |

---

## 2. Known limitations

These are real limits of 0.2.4, not bugs waiting to be fixed by accident. Each
row says why it exists and what to do instead.

| Limitation | Why it exists | What to do instead |
| --- | --- | --- |
| **The medium is never written to** | The core promise of the tool. A write path (formatting, quarantine, adding a report to the stick) would make a read-only triage tool into something that can destroy evidence and needs write access. | Copy the image out; the archive and the reports live in `general.output_dir` and `report.directory`. |
| **Nothing is mounted with privileges** | Mounting goes through udisks2 and polkit as the desktop user. Running as root, or a helper with `CAP_SYS_ADMIN`, would let a hostile medium reach a privileged process. | Mount in the file manager and pass the mount point to `--path`. |
| **Linux only** | Discovery is lsblk plus udisksctl, the interface is Kirigami. Porting means different enumeration, mounting, installer and test machines. | Out of scope; nothing is tested on Windows or macOS. |
| **Uploaded archives are public to anyone with the link** | Every supported host is anonymous and keyless; none of them offers private links. 0.2.4 only warns about it in the interface, the report and `pipeline.py`. | Do not upload, use `--no-upload`, or wait for encrypted archives (0.3.0). |
| **0.2.4 has no encrypted archives** | Encryption needs a passphrase source, a rule that the password never reaches the config or the report, and a story for how the recipient learns it. A password that travels in the same email as the link protects nothing. | Upgrade to 0.3 (encrypted uploads are implemented on `main`), or upload only material you accept becoming public. |
| **0.2.4 has no job history** | A finished job leaves files in `report.directory` and nothing else; there is no index, no search and no way back to an earlier result from the interface. | Upgrade to 0.3 (the history index is implemented on `main`), or keep the report directory and open the JSON reports by hand. |
| **No translations** | Every string is English, in the QML files and the report/email templates. A language setting would have nothing to select, so `general.language` was removed instead of kept as a dead key. | English only; see 0.4.0. |
| **No per-file incremental scan** | A trustworthy incremental scan needs a baseline the user trusts and a definition of "changed" on a filesystem with unreliable mtimes. Getting that wrong produces false negatives, the worst failure for this tool. | Rescan the whole medium; slower, but it cannot lie. |
| **No resumable uploads** | Neither gofile nor the simple hosts expose a resumable protocol; a wrong implementation silently corrupts the image. | Retries with backoff, split archives (`archive.split_size_mb`), re-run. |
| **No multi-device queue** | A queue implies unattended operation, while the safety interlocks (confirm before publishing, never upload a non-clean image) assume a human is present. | Run jobs one at a time. |
| **ClamAV is driven through its command line** | `clamdscan --fdpass` is convenient and honest about the engine front end, but it serialises the scan behind one process and one connection. | Nothing yet; this is a candidate for later (section 6). |
| **No Flatpak, Fedora or Arch package** | The `.deb` and the AppImage exist; the remaining packaging is maintenance the project has not committed to. | Use the AppImage, the `.deb`, `pipx` or the source checkout. |
| **No KRunner plugin or KIO worker** | A Dolphin service menu exists (0.2.4), but a KRunner plugin and a KIO worker are convenience rather than capability. | Use the Dolphin actions or `dcs gui --device` / `dcs gui --folder`. |
| **No daemon or system service** | DCS is a user-session tool; a system service would need privilege separation, a socket API and its own threat model. | Use a systemd user timer or the watcher's autostart entry. |

---

## 3. 0.3: the private and reviewable workflow

**Status:** both features below are implemented on `main` and are waiting for the
0.3.0 / 0.3.1 releases; `CHANGELOG.md` lists what landed. The checklists stay
here until the release that carries them is out, and they are the definition of
"done" for that release, not a description of the past.

The milestone is about the two things the workflow was missing: **the upload is
private** and **the result can be revisited**. It is deliberately not about
adding another scanner or another host.

### 3.1 0.3.0 — encrypted uploads

The largest practical weakness of 0.2.4: an archive on an anonymous host is
public to anyone who has the URL. The fix is to encrypt the image before it
leaves the machine, using a format the recipient already has.

**Behaviour contract**

1. A clean medium produces an archive as it does today.
2. The job asks for a passphrase from, in order: the value handed to this run
   (a command-line flag or the interface's prompt), the environment variable
   named by `archive.passphrase_env` (default `DCS_ARCHIVE_PASSPHRASE`), or
   the helper command in `archive.passphrase_command`.
3. The passphrase is never written to the configuration, the report, the JSON
   result or the log. An archive requested with encryption but without a usable
   passphrase **fails closed**: no unencrypted archive is produced.
4. Encryption is 7z's AES-256 (the format every desktop can open); file names
   are encrypted too (`-mhe=on`) by default.
5. The recipient learns the passphrase out of band. The report and the email
   say the archive is encrypted and that the password is not in them.
6. Encryption never relaxes a safety rule: a suspicious or infected medium is
   still refused by `upload.upload_when` and by the confirmation dialog, no
   matter whether the image would have been encrypted.
7. Unencrypted local archives stay available (`archive.encrypt = false`).
   When uploading is enabled, the interface and `dcs doctor` recommend
   encryption, and the settings page offers to turn it on.

**What "done" means**

- `archive.encrypt`, `archive.passphrase_env` and `archive.passphrase_command`
  exist, are validated, and are documented in `docs/CONFIGURATION.md`.
- `dcs run --encrypt` (and the `--no-encrypt` override) work; a missing
  passphrase is an error with a message that says what to set.
- The archive stage verifies the produced file really is encrypted (a listing
  without a password must fail when headers are encrypted) and fails the stage
  if it is not.
- `ArchiveResult` carries `encrypted` / `encryption`; the HTML, text, CSV and
  JSON artefacts and the email say the archive is encrypted and never contain
  the passphrase.
- The interface has the option, a passphrase prompt, and a warning when an
  upload would be unencrypted.
- Tests: encryption produces a file that opens with the passphrase and not
  without it, no secret lands in the config/report/log/JSON, missing passphrase
  fails closed, and encryption does not bypass the verdict gate.

### 3.2 0.3.1 — job history

A finished job leaves a report on disk and nothing else; the interface cannot
show yesterday's result. A local, searchable index fixes that.

**What "done" means**

- A SQLite database under `default_data_dir()` (`history.db`) with a versioned
  schema and a real migration path: a newer release opens an older database
  instead of failing.
- One row per job: date, label, verdict, files, findings, threats, sizes,
  archive path and SHA-256, encryption flag, share link, report paths.
- Per-finding rows (path, SHA-256, severity) so label, verdict, filename and
  hash are all searchable without duplicating report bodies; report *paths* are
  stored, not copies of the files.
- `dcs history list|show|open|delete|clear|prune`, with JSON output.
- A History page in the interface: date, label, verdict, counts; search; open
  the stored report; delete one job; **Clear history** behind an explicit
  confirmation.
- Retention controls (`history.max_jobs`, `history.max_age_days`) that are
  visible from the first release, because labels and file names are sensitive.
- A `history.enabled` switch, and a documented way to remove the database.
- The database is local-only. Nothing about it is uploaded, emailed or looked
  up online.
- Tests: schema creation, migration from the previous schema version, search,
  retention, delete/clear, and that recording never copies report contents.

### 3.3 Release gate for 0.3

- `pytest`, `ruff` and `mypy` clean; coverage reported with a documented floor.
- One end-to-end test: synthetic USB folder → scan → encrypted archive → mock
  upload → report → history row.
- A package-install smoke test in regular CI (not only the release workflow).
- Service tests and streaming tests isolate their XDG state directories.

---

## 4. 0.4 and later: translations and accessibility

Internationalisation gets more expensive with every string added, so it should
land before large interface expansion, not after.

### 4.1 0.4.0 — translations

- Extract the QML strings into KDE-compatible catalogues and use the
  Ki18n/QML `i18n()` mechanism, so the interface follows the Plasma language
  setting.
- Translate the report and email templates independently of the desktop
  language: `general.language` selects the language of the artefact when it
  differs from the locale, because the recipient is usually not the operator.
- Slovenian is the first complete non-English catalogue (the maintainer's
  language), followed by whatever contributors send.
- A contributor note in `CONTRIBUTING.md` on adding or updating a language.

### 4.2 Accessibility review

Keyboard-only operation of the interface, focus order, screen-reader labels on
the custom controls, colour contrast under both Plasma colour schemes, and a
check that the reports remain readable at 200 % zoom and in a monochrome
printout. Fix what the review finds and add the fixtures that pin it.

---

## 5. Keeping this document honest

- Every release updates section 1 (add what shipped) and section 2 (remove what
  no longer applies). A release whose documentation still describes the previous
  milestone is a release defect.
- Items that depend on a measurement say so and quote no number until it has
  been measured on the documented corpus (`docs/benchmarks.md`).
- An item may be dropped. Dropping an item is cheaper than shipping a half
  feature that has to be supported forever.

---

## 6. Later ideas (not committed)

Nothing below is scheduled. They are recorded so that they are not lost, and so
that contributors do not build them twice by accident.

| Idea | Why it is not committed yet |
| --- | --- |
| **Flatpak manifest**, **Fedora** and **Arch** packages | The `.deb` and the AppImage exist. Each package is a maintenance promise, and the project would rather have two packages that work than five that lag. |
| **A second upload destination per job** (cross-provider fallback) | Needs a decision about what "the share link" means when two hosts were used, and an email template that renders more than one link well. |
| **Resumable / chunked uploads** | No supported host exposes a resumable protocol; split archives already bound the cost of a failure. |
| **KRunner plugin and KIO worker** | Convenience on top of the Dolphin actions that already exist. |
| **Multiple media in one queue** | Conflicts with the human-in-the-loop safety rules; a front-end concern if it is ever added. |
| **ClamAV over the clamd socket with a worker pool** | `clamdscan` is fast enough in practice; the socket path buys throughput at the cost of a second code path for the same engine. Revisit only if a benchmark says it matters. |
| **Per-file findings schema with a version field** | The CSV is already stable enough for spreadsheets; a versioned consumer schema is a small, separate piece of work. |
| **Rust or C++ for the hot paths** | See the measurement gate below. Nothing is rewritten before it is measured. |
| **Incremental scans** | Needs a baseline the user trusts; a wrong implementation produces false negatives. |

### The performance question

The walker, hashing and the heuristic matchers are the only CPU- or syscall
bound parts; everything else is I/O inside another process or the kernel. The
migration is therefore gated on measurements, not enthusiasm:

1. Build a reproducible benchmark corpus (several hundred thousand small files,
   a few large files, a deep hierarchy, a mixed document/archive/executable
   tree) and record the machine: CPU, disk, filesystem, ClamAV and Python
   versions. The corpus generator and the numbers live in
   `docs/benchmarks.md`.
2. Measure enumeration, heuristic scan, ClamAV scan, peak RSS, the small-file
   and large-file cases, and interface responsiveness during a very large scan.
3. Port only what the numbers identify as a meaningful share of total wall time,
   and keep the pure-Python implementation as the fallback. A meaningful share
   is decided before measuring, not after.
4. If no single part is a meaningful share, stop. The project keeps Python and
   spends the effort on detection coverage instead.

The user-visible contract — the CLI, exit codes, configuration keys, event and
stage names, `JobResult` JSON and the QML-facing backend API — does not change
in any case. Anything that crosses that boundary is a documented data shape,
never a Python object, which is what makes an optional faster implementation
invisible from the outside.
