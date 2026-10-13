# Changelog

All notable changes to Ducky Can Shoot are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0 the
command line, the configuration keys and the JSON result contract can still
change between minor releases; each such change is listed below.

## [Unreleased]

## [0.1.0] - 2026-01

First release. Everything below is new.

### Added

- **Headless core** (`dcs.core`) with no Qt imports: the CLI, the tests and the
  graphical interface all run the same pipeline through a single event stream.
- **Two scan engines.** ClamAV (clamdscan with `--fdpass` when a daemon is
  running, otherwise the stand-alone clamscan fed one large batch) and a
  dependency-free heuristic engine that reports the evidence behind every
  finding: Duckyscript/HID payload scripts, PowerShell encoded commands,
  download cradles and evasion flags, pipe-into-shell and base64-into-shell,
  double extensions, autorun.inf launch entries, Office macros with automatic
  entry points, OOXML remote templates, PDF JavaScript and launch actions,
  packed PE files with process-injection or keylogging APIs, high-entropy
  binaries, suspicious shortcuts, registry persistence and defence evasion,
  ransomware precursors, zip bombs, password protected archives and a
  SHA-256 block list.
- **Drive discovery** through lsblk plus udisks2 (`udisksctl`): removable media
  are listed with label, size, filesystem, bus and model, and can be mounted
  without root. Any directory can be triaged instead of a device.
- **Archiving** to zip (external `zip`) or 7z, with optional volume splitting,
  a free-space pre-check and a pure Python zipfile fallback when the external
  tools are missing.
- **Upload adapters** behind one interface: gofile.io by default, plus
  litterbox, temp.sh, x0.at, catbox, tmpfiles.org and pixeldrain. Uploads report
  byte level progress, honour cancellation and retry with backoff.
- **Reports** in four formats - self contained HTML, plain text, findings CSV and
  a machine readable JSON - written atomically next to each other.
- **SMTP notification** with an HTML and text body, the verdict, the numbers,
  the striking findings, the download link when the medium is clean, and the
  report attached. A dry run writes the message to disk instead of sending it.
- **Kirigami interface** (PyQt6 + QML) with a live view of all seven stages,
  per-stage progress, a throughput chart, files and bytes counters, the current
  file, findings as they appear, a log pane and a result page with the verdict,
  the share link, the artefacts and a timeline.
- **Command line** with `gui`, `devices`, `scan`, `run`, `doctor`, `config`,
  `engines`, `providers` and `version`, and an exit code contract of 0 (clean),
  1 (suspicious or infected) and 2 (error or cancelled).
- **Packaging and documentation**: desktop entry, scalable icon, a user level
  install script, an annotated example configuration, README, architecture,
  configuration reference, roadmap, security policy, contribution guide and a
  verified survey of the free upload hosts.

### Safety

- A medium that is not clean is scanned, reported and emailed but **never**
  archived and **never** uploaded, so malicious files never leave the machine
  through this tool. Publishing a non-clean image additionally requires an
  explicit confirmation.
- Nothing found on the medium is ever executed, and the medium is never written
  to.

[Unreleased]: https://github.com/ducky-can-shoot/ducky-can-shoot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ducky-can-shoot/ducky-can-shoot/releases/tag/v0.1.0
