# Changelog

All notable changes to Ducky Can Shoot are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0 the
command line, the configuration keys and the JSON result contract can still
change between minor releases; each such change is listed below.

## [Unreleased]

Nothing yet.

## [0.2.0] - 2026-10-16

### Changed

- **The interface follows the KDE Human Interface Guidelines now.** The
  guidelines were read page by page and the differences fixed:

  - **Navigation.** Five destinations that are not a linear workflow are a
    `Kirigami.NavigationTabBar` ("with five or fewer destinations, use a
    Kirigami.NavigationTabBar"); it is also the control for immutable tabs, which
    is what these are.
  - **Messages.** The home made toast popup is gone: messages the user may
    ignore go through `showPassiveNotification`. A system notification is only
    sent while the window is *not* in the foreground, so plugging a medium in no
    longer pops a notification on top of the window that already shows the same
    thing.
  - **Settings page.** Invalid input is shown in a `Kirigami.InlineMessage` and
    disables **Save Settings** until the draft is valid again; the password uses
    `Kirigami.PasswordField`; the sentence about leaving it empty sits inline
    under the field instead of inside the placeholder (a placeholder is an
    example, not an instruction); and the longer explanation of the autostart
    entry moved into a `Kirigami.ContextualHelpButton`, because three sentences
    of inline description overrun the card.
  - **Text.** Interactive labels are in title case ("Save Settings", "Open Report
    Folder", "New Job"), while labels in front of a control, checkbox labels and
    combo box items stay in sentence case, and every action that opens a dialog
    ends with the real ellipsis character `…` instead of three periods.
  - **Links.** The homepage on the About page is an underlined link instead of a
    button ("to open URLs, use underlined web-style links, not buttons").
  - **Long jobs.** While a job runs, its completion percentage is reported to the
    task manager (`setBadgeNumber`), which the guidelines ask for on a long task
    the user started.
  - **Spacing.** Title, description and messages form a group with
    `smallSpacing`, and the bar at the bottom of the settings page uses
    `mediumSpacing` between its buttons.
- **Email is off in a fresh configuration.** An installation with neither an SMTP
  server nor a recipient cannot send anything, so the default said one thing and
  the validator said another: the settings page of a new installation opened with
  a warning and a disabled Save button. Email is now enabled once a server and a
  recipient are configured, `dcs run --to ADDRESS` still sends without that (an
  explicit recipient is an instruction to send), and a job reports missing
  settings as an actionable reason instead of failing.
- **The application icon is the project's own artwork.** A duck holding a pistol
  whose muzzle faces the viewer, wings on both sides - one hand written SVG in
  `packaging/icons/hicolor/scalable/apps/org.kde.dcs.svg`, drawn on a light badge
  so the dark gun stays visible on a dark panel, next to a Breeze style symbolic
  variant (`org.kde.dcs-symbolic.svg`: one colour through `currentColor`, with
  the eyes and the barrel cut out by `fill-rule="evenodd"`) and the nine bitmap
  sizes a distribution package still ships. `scripts/make-icons.sh` (and
  `make icons`) renders the bitmaps from the SVG, `scripts/install.sh` installs
  the whole set into `hicolor` and `scripts/uninstall.sh` removes it again, and
  the About page shows the same drawing inside the window.

### Added

- **A Debian package.** `make deb` (or `scripts/build-deb.sh`) builds
  `dist/ducky-can-shoot_<version>-1_all.deb`: architecture independent, bundling
  nothing, depending on the distribution's Python, PyQt6, Kirigami and Qt SVG
  plugins. It installs `dcs` and `dcs-gui` in `/usr/bin`, the desktop entry, the
  icon set, the AppStream metadata, a man page and
  `/etc/xdg/autostart/org.kde.dcs.watch.desktop`, so the USB watcher can run from
  login on a system wide installation too (it stays inactive until a watch mode is
  enabled). Maintenance scripts refresh the desktop and icon caches, the package
  carries `md5sums` and a machine readable copyright, and the CI builds and
  smoke tests it on every push and attaches it to every release.
- **A release helper.** `scripts/bump-version.py 0.2.0` (or
  `make version VERSION=0.2.0`) bumps the version in `src/dcs/version.py` and
  `pyproject.toml`, adds the release to the AppStream metadata and turns the
  changelog's `[Unreleased]` section into the new version with a fresh empty one
  above it - the four places a version lives, in one command, so a release cannot
  go out half bumped.

- **The settings page keeps its buttons where you can see them, and "Test SMTP"
  tests what is on screen.** The form is much taller than the window, so the
  "Save settings" button at the end of it was easy to miss: changes could be left
  unsaved without noticing, and the message that said so appeared at the top of
  the page, out of sight while scrolling. The page now has a bar at the bottom
  that never scrolls away. It says **Unsaved changes** or where the last save
  landed, offers Revert and Save settings, and the SMTP section reports the result
  of the connection test next to the button that asked for it. "Test SMTP" uses
  the values currently in the form rather than the ones that were saved last,
  which is why it used to fail and then work after saving.
- **USB detection: react when a medium is plugged in.** Off by default, four
  modes in **Settings -> USB detection** (or `[watch] mode`): `off` does nothing,
  `notify` shows a desktop notification with an **Open Ducky Can Shoot** button,
  `open` starts the interface with the new medium already selected, and
  `open_run` starts the whole job - scan, archive, upload and, when a recipient
  is configured, the email; without a recipient it stops after the report and
  shows the link on the result page. The watcher polls lsblk every three seconds:
  no root, no udev rule, no extra dependency, and it never mounts, reads or scans
  anything by itself. A stick with two partitions is one medium and produces at
  most one job, and while a window is open it does the watching itself, so the
  background watcher never reacts a second time.
- `dcs watch [--mode ...] [--interval S] [--once] [--status] [--json]`, the same
  listener from the command line, and `dcs gui --device NODE [--autorun]`, which
  is how a newly plugged medium is handed to a fresh window.
- **Start the watcher with the session, if you want it.** `dcs config set
  watch.autostart true`, the checkbox on the settings page, and
  `scripts/install.sh --watch=notify|open|open_run` all write one autostart
  entry. The installer asks, and "do nothing" is the default answer.
- `dcs config set KEY VALUE` and `dcs config --options`: change one option from a
  script with the same coercion and the same validation the loader uses, so a
  value the configuration calls broken is refused instead of stored.

- **Repository polish for the first public release**: a banner and badges, a front
  page organised around a quick start and the situation the tool is for, issue
  forms that ask the questions worth asking, a pull request template, a release
  workflow that builds the wheel and attaches it to a GitHub release whose body
  comes from this file, a code of conduct, a maintainer checklist in
  docs/PUBLISHING.md, a Makefile for the usual development tasks, and
  scripts/make-screenshots.py, which regenerates the documentation screenshots
  from the real interface and fails if QML prints a warning.
- The long reference sections moved out of the README into docs/CLI.md,
  docs/TROUBLESHOOTING.md, docs/THREAT-MODEL.md and docs/EXTENDING.md, so the
  front page can be read in a couple of minutes.
- An explicit disclosure of the AI assistance used to build this project, in the
  README and in CONTRIBUTING.md, which now states what is expected from
  AI-assisted contributions: disclose, understand, test, cite.
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


- `dcs watch --start`, `--stop` and `--test`, plus the watcher pid and log in
  `dcs watch --status`.

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
- **The status bar is no longer drawn two thirds tall.** The window footer slot
  is filled by a styled container (Kirigami.AbstractApplicationHeader, and a
  ToolBar with an explicit height too), which paints its background at its own
  implicit height while the window content, raised above it by Kirigami, covers
  the rest: screenshots showed a bar cut off after about eighteen of its
  forty-seven pixels. The bar is now the last row of the page layout, where it
  is exactly as tall as it says it is and always reaches the bottom edge.
  scripts/make-screenshots.py also nudges the window by one pixel before every
  grab, because the offscreen platform otherwise reuses a stale region, and it
  refuses to write an image whose status bar does not reach the bottom.
- **No empty band above the navigation.** Kirigami builds a toolbar row for the
  current page and reserves its height (48 px with the Breeze style) even when
  the page has no title, no actions and no drawer, so after removing the global
  drawer an empty strip was left where the hamburger had been. The page now sets
  Kirigami.ApplicationHeaderStyle.None, and a test asserts that the tab bar
  starts at the top of the window.
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
- **The package list was missing the Qt SVG plugins.** `libqt6svg6` is the
  library, but the icon engine and the image format plugin that actually draw an
  SVG live in `qt6-svg-plugins`, a separate package. An installation that
  followed the README showed empty squares where the icons and the application
  artwork should be. The README, the CI job and the troubleshooting page now say
  so, and a test loads the artwork through Qt to catch a missing plugin.
- **The CI job that tests the real interface no longer fails on a missing
  package.** It installed Kirigami 6 from apt on `ubuntu-latest`, which still
  means Ubuntu 24.04 - a release that has no KDE Frameworks 6 packages at all,
  so `qml6-module-org-kde-kirigami` does not exist there and apt aborted the
  step after eight seconds. The job now asks for `ubuntu-26.04`, the release
  this project targets, where Qt 6.10 and Kirigami 6.24 are packaged. The
  workflow actions moved to their current majors (checkout 7, setup-python 7,
  ruff-action 4, upload-artifact 7, download-artifact 8), which also clears the
  Node 20 deprecation warning, and the core matrix now covers Python 3.14 as
  well as 3.11, 3.12 and 3.13.


- **The notification now has its button, and the watcher actually runs.** Two
  things stopped "plug a medium in while DCS is closed" from working:

  - **Nothing was listening.** The watcher is started by an autostart entry, and
    an entry written *after* a login only takes effect at the next one, so between
    the two there was no process at all. Enabling a mode now starts the watcher
    immediately (`dcs watch --start`, called by the settings page and by the
    installer), closing the window hands the watching over to it instead of
    stopping, and the settings page shows whether it is running next to a
    **Start Now** button. A pid file makes starting it twice harmless, and
    everything it does is written to `~/.local/state/dcs/watch.log`.
  - **The notification arrived without its Open button.** `notify-send` refuses
    to send actions on systems where it believes the server cannot show them -
    Plasma 6.6 answers "Actions are not supported by this notifications server",
    in spite of advertising the `actions` capability - so DCS talks to
    `org.freedesktop.Notifications` directly: one `Notify` call with the button,
    then a signal monitor for `ActionInvoked`. Notifications are also marked
    resident so the button does not disappear while the user reads the mail
    first, the button is withdrawn again when nobody presses it, and the wait runs
    on its own thread so a second medium plugged in meanwhile is still noticed.
    `notify-send` stays as the fallback for a machine without `gdbus`, and
    `dcs watch --test` says which of the two is in use.
- **Reinstalling no longer turns the USB watcher off.** `scripts/install.sh`
  without an answer to its question (a pipe, a script, a package build) fell
  through to "off" and quietly disabled the mode, removed the autostart entry and
  left the user wondering why the notifications stopped. It now leaves the
  configured mode alone in that case, and `--watch=MODE` still says what to do.

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

[Unreleased]: https://github.com/Linux-Alex/ducky-can-shoot/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Linux-Alex/ducky-can-shoot/releases/tag/v0.2.0
[0.1.0]: https://github.com/Linux-Alex/ducky-can-shoot/releases/tag/v0.1.0
