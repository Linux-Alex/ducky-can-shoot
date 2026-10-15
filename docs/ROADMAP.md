# Roadmap

This is a working plan, not a promise. It records what 0.1 does on purpose, what
comes next, and how the maintainer intends to move the hot paths out of Python
without breaking the contract that the CLI, the tests and the GUI share.

Anything in this document that depends on a measurement says so. No performance
number is quoted until it has been measured on a documented corpus.

## 1. What 0.1 deliberately does not do

These are omissions by design, not bugs. Each one is a decision that keeps the
first release small, safe and reviewable.

| Not implemented | Why | What to do instead |
| --- | --- | --- |
| **Writing to the medium** | The tool's core promise is that the medium is untouched. Any write path (formatting, quarantine, renaming, adding a report to the stick) would turn a read-only triage tool into something that can destroy evidence and that must be trusted with write access. | Copy the image out and work on the copy. The archive and the report live in `general.output_dir` and `report.directory`. |
| **Mounting with privileges** | DCS mounts through udisks2 and polkit as the desktop user. Running as root, or shipping a helper with CAP_SYS_ADMIN, would let a hostile medium reach a privileged process. | Mount the stick in your desktop file manager and pass the mount point with `--path`. |
| **Windows and macOS** | Device discovery is lsblk plus udisksctl, and the GUI is Kirigami. Supporting other platforms means different enumeration, different mounting, a different installer and a different set of test machines. | Out of scope for now. The core logic is portable in principle, but nothing is tested there. |
| **Incremental scans** | A real incremental scan needs a trustworthy baseline (a hash index of the previous state) stored somewhere the user trusts, plus a definition of what "changed" means on a filesystem with no reliable mtimes. Getting that wrong produces false negatives, which is the worst possible failure mode for this tool. | Rescan the whole medium. It is slower but it cannot lie. |
| **YARA** | YARA rules are their own ecosystem: rule sources, versioning, licensing, quality control and false positive management. A half-hearted YARA integration would give users a false sense of coverage. | ClamAV signatures plus the built-in heuristics. YARA is a candidate for a later release with a real rule policy (see below). |
| **Resuming an interrupted upload** | Neither gofile nor the simple hosts expose a resumable upload protocol; the adapters would have to re-upload a partial image or implement chunked upload against an API that does not offer it. A wrong implementation silently corrupts the image. | Retries with exponential backoff, split archives (`archive.split_size_mb`) so a failure costs one volume instead of the whole image, and a re-run. |
| **Multi-device queues** | A queue implies unattended operation, and the safety interlocks (confirmation before publishing, refusal to upload a non-clean image) assume a human is present. Unattended triage of several sticks is a policy decision, not a feature. | Run jobs one at a time. The pipeline is already stateless per run, so a queue is a front-end concern if it is ever added. |
| **Password-protected archives** | Encrypting the image would be the natural mitigation for public-host uploads, but it needs a key management story (where does the password live, how does the recipient learn it, what happens if it is lost) before it is safe to ship. A password that travels in the same email as the link protects nothing. | Do not upload, or upload only material you can accept becoming public. This is the single most requested missing feature; see the open question in section 3. |
| **A daemon or a service** | DCS is a user-session tool. A system service would need privilege separation, a socket API and a threat model of its own. | Run the CLI from a script or a systemd user timer. |

Two more things that are missing rather than refused:

- **No translations yet.** The strings are English and live in the QML files and
  the report templates, so a language setting would have nothing to select. It was
  removed from the configuration rather than kept as a dead key; when the i18n work
  lands it comes back together with the catalogues.
- **No packaged artefacts yet.** Installation is from source; there is no AppImage,
  Flatpak, snap or distribution package.

## 2. Short term: what 0.2 should contain

Ordered by value per unit of risk. Each item states what "done" means.

### 2.1 ClamAV through the clamd socket, in parallel workers

Today the ClamAV engine shells out to `clamdscan --fdpass` with a file list and
parses its output, which is convenient and honest about the engine front end, but
serialises the whole scan behind one process and one connection.

What "done" means:

- Talk to `clamd` directly over its UNIX socket (`INSTREAM` for file content,
  `SCAN`/`CONTSCAN` with `--fdpass` semantics where possible), keeping the
  command line path as a fallback when no socket is configured.
- Scan with a small pool of worker threads or processes, sized from a new
  configuration key, so disk throughput and clamd CPU are used at the same time.
- Keep the existing `ScanContext.tick_file()` contract so progress stays correct,
  and make the resulting scan the same set of `Finding` objects.
- Prove it with the fake clamd in the test suite plus one integration test against
  a real daemon, and record the before/after wall time on the reference corpus in
  `docs/benchmarks.md`.

### 2.2 A job history database with a browsable UI

Every run already produces a complete `JobResult` as JSON. Keeping those in one
place turns DCS from a one-shot tool into something you can look back into.

What "done" means:

- A small SQLite database under `default_data_dir()` (`~/.local/share/dcs`) with
  one row per job plus the findings, written by the report stage and readable
  without the medium.
- A history view in the GUI (a new tab or a page in the drawer) listing past jobs
  by date, label, verdict and size, with per-job drill-down into the same
  information the result page shows.
- Search and filtering by verdict, label, path and hash.
- Export of a past job back to the HTML, text, CSV and JSON artefacts.
- A retention policy (age or count) and a documented way to delete a row, because
  a triage database accumulates sensitive file names.
- The schema is a documented, versioned contract; a newer release must migrate an
  older database rather than fail.

### 2.3 Per-file findings export

The CSV already lists findings with severity, engine, category, threat, path,
size, sha256 and detail. The next step is a format other tools can consume
directly.

What "done" means:

- A documented, stable CSV and JSON schema for the findings themselves
  (independent of the job wrapper), with a version field.
- An option to include the scanned file list in an export without embedding it in
  the HTML report.
- A short note in the documentation on how to import the CSV into a spreadsheet or
  feed it to another scanner, including the meaning of each severity.

### 2.4 Packaged artefacts

Installation instructions are a wall of distribution packages today.

What "done" means, in order of increasing effort:

- A working Flatpak manifest, so that the GUI (PyQt6, QtQuick and Kirigami) is
  available on distributions that do not package it in the right versions.
- An AppImage for the same reason, with a documented limitation: the AppImage
  cannot provide `udisksctl`, `clamscan` or `zip`, so it must use the host's.
- Distribution packaging for Fedora and Arch as well: the Debian package exists
  (`make deb`), and the same script is the template for the others. The CLI and
  the GUI travel in one package, since the GUI only drags in Qt and Kirigami
  through its dependencies.
- A release workflow that builds the artefacts, runs the test suite against them
  and attaches the checksums to the release.

### 2.5 Translations

What "done" means:

- Strings out of the QML files and the report and email templates into a catalogue
  that the KDE translation infrastructure can consume, using the Ki18n/QML
  `i18n()` mechanism so the interface follows the Plasma language setting.
- `general.language` finally consumed: it selects the language for the report and
  the email when it differs from the desktop locale, which matters because the
  recipient is often not the operator.
- A contributor note in `CONTRIBUTING.md` on adding a language.

### 2.6 KRunner and KIO integration

The last item, because it is convenience rather than capability.

What "done" means:

- A KRunner plugin that offers "Triage USB medium" and lists currently attached
  removable devices, starting a job with the configured defaults.
- A KIO worker (or at minimum a service menu / Dolphin action) that runs a scan on
  the selected folder and opens the resulting report.
- Neither of these may bypass the confirmation that a public upload requires.

### 2.7 Candidate items that need a decision first

- **Archive encryption.** The right mitigation for public-host uploads. Needs: a
  key source (a passphrase from the environment or a keyring, never the config
  file), a decision about whether an encrypted image may be uploaded when the
  verdict is not clean (it should not - an encrypted malware image is still
  malware distribution), and documentation of how the recipient gets the
  passphrase out of band. Until all three are settled, `upload_when = clean` plus
  the confirmation dialog remain the whole story.
- **YARA support**, behind an explicit rule-source policy: rules must be pinned to
  a revision, licensed compatibly, and every rule must come with its evidence and
  a false-positive test.
- **A second upload destination per job** (cross-provider fallback), which the
  research document recommends. This needs the pipeline to decide what "the share
  link" means when two hosts were used, and the email template to render more than
  one link well.

## 3. The Rust and C++ migration

The maintainer wants the hot paths out of Python. The plan below is staged so that
each step can be stopped without leaving the project half-migrated, and so that
the user-visible contract survives every step.

The guiding rule: **the CLI, the configuration schema, the event names and the
`JobResult` JSON are the contract; the implementation language is not.** Anything
that crosses the boundary is a documented data shape, never a Python object, so a
rewrite of the inside is invisible from the outside.

### What is actually slow

Only three things in DCS are genuinely CPU or syscall bound:

1. **The file walk.** `walk_files()` uses `os.scandir` with a manual stack, a
   stat per entry, fnmatch against the exclude patterns and a `FileEntry` plus a
   relative path string per file. On a stick with hundreds of thousands of small
   files this runs twice (enumeration pass and scan pass).
2. **Hashing.** `sha256_file()` is called for the archive and, when
   `scan.hash_findings` is on, for every finding. OpenSSL via `hashlib` releases
   the GIL, so this scales with disk speed rather than Python, but the per-call
   overhead and the read chunk loop are Python.
3. **Heuristic content matching.** The built-in engine runs a set of regexes and
   substring searches over up to 2 MiB (4 MiB for PDFs) per file, plus entropy
   computation and a ZIP parsing step. This is pure Python byte and string work
   and is the most obviously improvable part.

Everything else - the archive tools, ClamAV, HTTP uploads, SMTP - is I/O bound and
spends its time inside another process or in the kernel.

### Phase 0: measure before touching anything

Nothing moves until there are numbers, because the current implementation is
correct and a rewrite costs review time that could go into detection rules.

- **Reference corpus.** A generated tree plus a real-world tree, both documented:
  several hundred thousand small files, a few large files, a mixed directory with
  documents, archives and executables, and a deep hierarchy to stress path
  handling. It must be reproducible (a script that regenerates it, with a fixed
  seed) and its composition must be recorded next to the results.
- **Reference machine.** The numbers only mean something with the CPU, the disk,
  the filesystem, the ClamAV version and the Python version written down.
- **Harness.** `docs/benchmarks.md` holds the commands and the results.
  `hyperfine` (or a small timing script) for wall clock, `perf` and `py-spy` for
  where the time goes, `/usr/bin/time -v` for peak RSS, and a JSON output mode of
  the CLI (`dcs scan --json`, `dcs run --json`) so results are parsed rather than
  eyeballed.
- **Baseline table.** For each benchmark below: the current value, the variance
  across runs, and the share of total wall time. Without the share, a 30 percent
  improvement in the walker may be 2 percent of the job.

### Phase 1: a Rust cdylib for walk, hash and content matching

A single extension module, built as a `cdylib`, exposing a small, boring API:

| Function | Replaces | Contract |
| --- | --- | --- |
| `walk(root, opts) -> entries` | `util.walk_files()` | Options: follow symlinks, include hidden, exclude globs, max files. Returns `(relative_path, size, mtime, is_symlink, is_special)`, or a stream in the same shape, plus unreadable paths reported through a callback. |
| `sha256(path, limit)` | `util.sha256_file()` | Same digest, same optional byte limit. |
| `scan_bytes(data, kind) -> indicators` | the content checks in `scanner/heuristic.py` | Returns the same indicator identifiers and evidence strings the current rules produce, so `heuristic.py` keeps building `Finding` objects. |

Non-negotiable properties of phase 1:

- **It is optional.** The pure Python implementation stays and remains the
  fallback. A distribution that cannot build Rust must still get a working DCS, and
  the tool must say which implementation it used (a line in `dcs doctor` and one
  in the JSON result).
- **The rules stay data.** The indicator tables in `scanner/rules.py` remain the
  source of truth; the Rust side consumes them (generated code or a shared data
  file) instead of duplicating them. A rule must never exist in two places.
- **No behavioural drift.** The extension returns the same findings for the same
  input. This is tested with a corpus-based equivalence test: run both
  implementations over the reference corpus and compare the complete `ScanResult`
  minus timings.
- **No new hard dependency at runtime.** The extension is built from source in the
  wheel and linked statically; it must not require a Rust toolchain on the user's
  machine.

### Phase 2: the gate that decides whether to continue

Phase 1 ends with a decision, made from the benchmark table, not from enthusiasm.
Proposed thresholds, to be agreed before measuring:

| Benchmark | Question | Suggested gate for continuing |
| --- | --- | --- |
| B1 walk | How much of a scan is enumeration plus stat? | Port stays only if the walker is a meaningful share of total wall time on the small-file corpus; if it is a few percent, keep Python. |
| B2 hash | Is hashing disk bound or Python bound? | If the extension moves less than a tenth of the difference between the measured rate and the raw disk rate, keep Python. |
| B3 content matching | How much of the heuristic engine is regex work? | Port the matchers only for the file kinds that dominate the time (measure per kind, do not port blindly). |
| B4 end to end | What does a full scan gain? | If the end-to-end gain is small, stop: the complexity is not paid for. |
| B5 memory | Does peak RSS stay flat on a huge tree? | Any regression here blocks the phase, streaming is a feature. |
| B6 upload | Is there Python overhead left in the upload path? | Compare against a raw upload of the same file. If the difference is within noise, leave the adapters in Python. |
| B7 GUI | Is the interface the bottleneck at the configured refresh rate? | Measure frame times at `general.ui_refresh_ms`; only a real problem justifies the C++ front end. |

If Phase 2 stops the migration, the project still ends up with a faster walker and
matcher behind an optional module, which is a win on its own.

### Phase 3: a C++/Qt6 core with a QML front end

Only if Phase 2 shows that the remaining time is in the scan loop itself, and that
the Python threading and object model (not the algorithms) is the cost.

Shape of the target:

```text
  +--------------------+        +---------------------------+
  | QML interface      |<------>| dcs-core (C++ / Qt6)      |
  | (Kirigami, as now) |        | walk, hash, match, engines|
  +--------------------+        | archive, upload, notify   |
                                | config, events, results   |
                                +-------------+-------------+
                                              |
                                   +----------v-----------+
                                   | compatibility shim   |
                                   | python3 -m dcs ...   |
                                   | import dcs.core ...  |
                                   +----------------------+
```

- The C++ core owns the pipeline, the models and the event stream. The Qt signals
  the GUI uses today become the native mechanism instead of a bridge over a Python
  thread.
- **Python remains as a shim** with the same module names: `dcs.core.pipeline`,
  `dcs.core.scanner`, `dcs.core.config` and `dcs.core.result` must keep working for
  anyone who scripted against them, delegating to the C++ core (pybind11,
  `shiboken`, or a small C API plus `ctypes`). The shim is allowed to be slow; it
  exists for compatibility, not for speed.
- The CLI keeps its exact interface: same subcommands, same flags, same exit codes,
  same JSON.

### What stays in Python, and why

| Stays Python | Why |
| --- | --- |
| Upload adapters (`core/upload/*`) | The hosts change constantly (see the research document: three of seven surveyed providers changed or died during one afternoon of testing). Editing a small Python file is the cheapest possible maintenance for churning HTTP. They are network bound. |
| SMTP notification | `smtplib` is in the standard library, the logic is a few hundred lines and the failure modes are server specific. |
| Report and email templates | String formatting and HTML, exercised by human eyes. Rewriting it in C++ would slow down every change to a report users read. |
| Configuration | TOML parsing, dataclasses and secret resolution are not hot and benefit from being trivially inspectable. The schema is the contract; a rewrite would risk silent key drift. |
| CLI argument parsing | `argparse` gives the help text, the subcommands and the error messages for free, and the CLI is the automation surface. |
| Tests and fakes | The fake ClamAV front ends and the `CollectingSink` are test infrastructure; porting them buys nothing. |
| The heuristic rule tables | Data, not code. They stay where reviewers can read them, even if the matching engine moves. |

### What the migration must not break

| Contract | Where it is defined | How it is protected |
| --- | --- | --- |
| `JobResult` JSON keys and types | `core/result.py` | Golden-file tests: the same job produces the same JSON (timings and ids normalised) before and after each phase. |
| Event and stage names | `core/events.py` | The names are asserted in tests; the GUI preselects rows from `STAGE_ORDER`, so a rename shows up as a missing row. |
| CLI exit codes | `exit_code_for()` in `cli.py` | Tests assert 0, 1 and 2 for a clean, an infected and a broken run. |
| Configuration schema | `core/config.py` | `dcs config show` is compared against a committed reference file; unknown keys keep being ignored. |
| Provider ids and their behaviour | `core/upload/registry.py` | The registry list is part of the documented interface; `dcs providers` is a test fixture. |
| The QML-facing backend API | `gui/backend.py` | Property and slot names are what the QML imports; renaming them is a breaking change for any third-party QML. |

Mechanically, that means every phase starts by committing the golden files and the
contract tests, so the migration is measured against fixed output rather than the
new implementation's own idea of correct.

## 4. Keeping the project healthy

### Tests

- `PYTHONPATH=src python3 -m pytest` is the entry point; `pytest` configuration
  lives in `pyproject.toml` (`testpaths = ["tests"]`, quiet output, deprecation
  warnings from `dcs.*` promoted to errors).
- Unit tests must not need a network, a real USB stick, a real ClamAV or a display.
  The fakes in `tests/fakes` and temporary directories cover all of it.
- Every detection rule gets a positive test (the evidence is present and the
  finding is produced) and, where the rule is easy to set off, a negative test (a
  benign file that must not produce it).
- Every stage gets a test that proves the skip rule that guards it, including the
  safety rules: an infected medium must not produce an archive and must not upload.
- Coverage is a guide, not a target. A rule change without a test is not reviewable.
- One optional, explicitly marked integration test hits a real ClamAV installation
  if `clamscan` is present; it is skipped otherwise.

### Packaging

- The version lives in `src/dcs/version.py` and is mirrored in `pyproject.toml`;
  a release bumps both and the tag matches.
- Wheel and sdist are built with `hatchling`; the QML directory and the hash
  database are force-included, so an installed package works without the source
  tree.
- Distribution packaging ships the CLI and the GUI separately where a distribution
  prefers that, because the GUI requires Qt and the Kirigami QML module.
- Installing must never require network access at build time beyond the declared
  dependencies, which is why the configuration writer is hand written instead of
  depending on `tomlkit`.

### Security policy

- `SECURITY.md` documents supported versions, the private reporting channel, the
  threat model, and the invariants that must never regress.
- The invariants are testable and therefore tested: never write to the scanned
  medium, never execute anything from it, never upload a non-clean image without an
  explicit opt-in, never log a secret.
- A report about a bypass of one of those invariants is treated as a release
  blocker, not as a normal bug.
- Anything that touches device mounting, subprocess invocation or file paths gets
  an explicit review note in the pull request describing the failure mode it
  guards against.

### Release checklist

1. The test suite passes on a clean checkout with `PYTHONPATH=src`.
2. `ruff` and `mypy` are clean (both are configured in `pyproject.toml`).
3. `dcs doctor` runs clean on a machine with ClamAV, udisks2, `zip` and `7z`.
4. A full job has been run against a real USB stick: clean medium, infected medium
   (an EICAR file is enough), cancelled run, and a run with `--no-upload`.
5. The reports (HTML, text, CSV, JSON) have been opened and read.
6. A dry-run email has been inspected in a mail client, including the HTML
   alternative and the attachments.
7. The provider list in `dcs providers` has been re-verified against the research
   document, and anything that stopped working is documented rather than silently
   left in place.
8. The version is bumped, the changelog entry written, the tag signed.
9. `README.md`, `docs/CONFIGURATION.md` and `docs/ARCHITECTURE.md` still describe
   the code that ships - a release whose documentation lies is a regression.
10. A signed artefact plus its checksum is published, and the previous release's
    upgrade path (config file, job history database) is confirmed.
