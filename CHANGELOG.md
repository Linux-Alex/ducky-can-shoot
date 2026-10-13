# Changelog

All notable changes to Ducky Can Shoot are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0 the
command line, the configuration keys and the JSON result contract can still
change between minor releases; each such change is listed below.

## [Unreleased]

### Added

- **Install ClamAV from the interface.** When the engine is missing, the New
  job tab and the Settings page offer an **Install ClamAV...** button. DCS
  detects the distribution and package manager (APT, DNF, Zypper, Pacman, APK,
  XBPS, eopkg, Portage, Homebrew), shows every command it will run, writes them
  to an inspectable script, and hands that script to pkexec (a graphical
  password dialog), to a terminal with sudo inside, or to plain sudo when it
  already runs on a terminal. The password is collected by polkit or sudo, never
  by DCS. The installation output is streamed into the dialog, each step is
  reported separately, the run can be cancelled, and the engine is re-probed
  when it finishes.
- `dcs install-clamav [--dry-run] [--terminal] [--yes] [--json]`, the same plan
  from the command line, exit code 0 on success and 2 on failure.

### Fixed

- **Uploads larger than 1 MiB were truncated.** DCS wrapped the file in a
  ProgressReader whose read() returned a single chunk when called without an
  argument - and that is exactly how requests reads a file part (it buffers it
  in memory). Every upload larger than the chunk size therefore sent only its
  first megabyte while reporting success, so the download link pointed at a
  corrupt archive. Uploads now stream a multipart body built by DCS itself: the
  file is read chunk by chunk, the progress bar shows real byte positions
  instead of jumping from 0 to 100 percent, memory stays flat for a 30 GB image,
  and the Content-Length is exact. gofile's server side checksum is compared
  with the local file, so a truncated transfer fails and is retried instead of
  being published. A regression test uploads several megabytes to a local HTTP
  server and asserts every byte arrives.
- **Readable text on a highlighted row.** The removable-media list drew its icon
  in the highlight colour and the mount status in green, both on top of the
  selection colour, which is unreadable with Kubuntu's light blue highlight.
  Every colour on a selected row now comes from the theme's highlighted palette.
- **The live view is usable again.** The pipeline rows derived their height from
  a layout that was anchored with anchors.fill, a binding loop that Qt resolved
  by collapsing every row onto the same position, so the seven stage rows (and
  every label and progress bar in them) were drawn on top of each other.
- **The headline tiles show numbers instead of dashes.** They reported the last
  progress event of whichever stage was running, and an upload has no file
  count, so "Files inspected" and "Rate" turned into "-". The tiles now read the
  scan stage and, once the job is over, the final summary (with the average rate).
- **The ClamAV installation log scrolls and follows the tail.** It is a list
  view now, so the scrollbar works, new lines keep the newest line visible, and
  scrolling up pauses the follow until you return to the end.
- No more "Cannot read property 'top' of null" from the Breeze style: DCS draws
  its own progress bar instead of using the QtQuick Controls one, whose style
  implementation logs those errors while it builds its segments.
- No more "Binding loop detected" for the Kirigami drawer handle, and no more
  "Could not register app ID" from the desktop portal: the global drawer is gone
  (the tab bar already carries every destination, and the drawer added a
  hamburger row above it) and the desktop file id is only claimed when a
  .desktop file is actually installed.
- Delegate widths in the layouts use Layout.fillWidth instead of binding width
  to the parent, which is what a ColumnLayout needs.
- Cancelling an installation (or a job) now stops the whole process group and no
  longer waits for the next line of output to notice.
- `config`: a value that cannot be converted to its field type is ignored with a
  warning instead of being stored as a string, and the TOML writer escapes
  control characters, so a value containing a newline no longer produces a file
  that tomllib rejects (which silently reset every setting).

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
