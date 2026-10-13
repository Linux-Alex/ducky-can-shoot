# Security Policy

DCS is a security tool that handles untrusted media, so its own safety properties
matter as much as its detections. This document states which versions are
supported, how to report a problem privately, what the tool defends against and
what it does not, and which guarantees users may rely on.

## Supported versions

DCS is pre-1.0. Only the most recent release is supported with security fixes;
fixes are made on the current development branch and released as a new patch
version. Older releases are expected to be upgraded rather than patched.

| Version | Supported |
| --- | --- |
| 0.1.x (current release series) | Yes: security fixes are released for the latest 0.1.x patch |
| Older releases | No: upgrade to the latest release |
| The development branch | Not a released version; not supported for production use |

Report a problem against a released version, and state the exact version
(`dcs version --json`) in your report. A problem that only reproduces on an
unreleased branch is still worth telling us about, but it is handled as a normal
bug until it reaches a release.

## Reporting a vulnerability privately

**Do not open a public issue, pull request or discussion for a security problem.**
A public report gives everybody the exploit before there is a fix.

Use GitHub's private vulnerability reporting instead:

1. Open the repository's Security tab and choose "Report a vulnerability", or go
   directly to the private advisory form at
   `https://github.com/Linux-Alex/ducky-can-shoot/security/advisories/new`.
2. Describe the issue with enough detail to reproduce it.

A useful report contains:

- the affected version and how it was installed (distribution package, pipx, venv
  from a source checkout), the distribution and the Python version;
- what an attacker controls (the medium, the network, the configuration file, the
  upload host) and what they gain;
- a step-by-step reproduction, including any file names, archive structures or
  configuration values needed - a minimal reproducer beats a description;
- the output of `dcs doctor --offline` and the relevant log lines at
  `--log-level DEBUG`;
- whether the issue is already public anywhere.

To collect the environment details in one go:

```bash
dcs version --json                # version and license
dcs doctor --offline || true      # environment and configuration checks (exit 1 when a check fails)
dcs config show                   # effective configuration, secrets masked as ***
```

Please redact file names, paths and share links taken from a real medium, and never
send us live malware: describe it, or send a hash.

What to expect:

- We will confirm the report and ask follow-up questions if needed.
- If the report is valid, we fix it on the development branch, add a regression
  test, credit you in the advisory unless you prefer otherwise, and release a
  patched version.
- If we cannot reproduce it, we will say so and explain what we tried rather than
  leaving the report unanswered.
- We will not ask you to keep a problem secret indefinitely: if a fix is not
  happening, we will tell you so and you are free to publish.

## Threat model

### What DCS defends against

DCS is built for one situation: **a medium you have already decided not to trust**,
and a machine you do not want that medium plugged into casually.

It defends against an attacker who controls the *contents* of the medium:

- **Hostile file names.** Names with path separators, control characters, Unicode
  bidirectional overrides, newlines or absurd length. Names are sanitised before
  they are used as archive names or report file names, and ClamAV is fed a file
  list so that paths never travel through a command line.
- **Hostile documents and scripts.** Macro documents, PDFs with automatic
  JavaScript, HID/DuckyScript payloads, download cradles, double-extension
  executables, autorun.inf boot scripts, suspicious shortcuts and registry
  persistence are reported with the evidence that triggered them.
- **Resource exhaustion by content.** Archives that expand enormously (zip bomb
  heuristics), an oversized file (`scan.max_file_size_mb`), a runaway number of
  findings (`scan.max_findings`) and a medium too large for the target filesystem
  (`archive.min_free_space_ratio`) all stop the job instead of filling a disk.
- **Concealed content.** Encrypted archives, password-protected ZIPs and archive
  formats the built-in engine cannot open are reported as coverage gaps rather than
  silently counted as clean.
- **Unnoticed publication.** The default configuration cannot publish a link to
  malicious files, and a non-clean image requires an explicit opt-in to upload.

The user's own machine is protected by the fact that DCS never executes anything
from the medium and never writes to it.

### What DCS does not defend against

- **A compromised machine running DCS.** DCS is an ordinary user process. There is
  no sandbox, no privilege separation, no seccomp filter and no container. Malware
  that is already running can read the configuration, the reports and the archive,
  and can watch the process.
- **A kernel, filesystem or firmware exploit triggered by attaching the device.**
  If the bug fires when the partition table is parsed, it fires before DCS is
  started, and DCS cannot help. Use a disposable machine or a hardware write blocker
  for media of unknown origin.
- **An attacker on the network path to the upload host or the mail server.** The
  TLS of those services is the only protection there. `email.insecure_tls = true`
  removes even that, which is why it is off by default and documented as dangerous.
- **A malicious or compromised upload provider.** Anything you upload is readable
  by the operator of that service, and by anyone who obtains the link. Every host
  DCS supports serves files publicly by URL.
- **A malicious configuration file or environment.** Somebody who can edit your
  configuration or environment can point DCS at their own SMTP server or upload
  host, or set a `password_command` that runs an arbitrary program as you. The
  configuration is trusted input by design. Do not run DCS with a configuration
  you did not write.
- **Detection misses.** A clean verdict means "no engine found anything", not "this
  medium is safe". The heuristics are conservative and the coverage is explicitly
  bounded; see the limitations below.
- **False alarms as a security property.** DCS errs towards reporting; a suspicious
  finding is not proof of malware either.
- **Physical attacks and social engineering.** A stick that looks like something
  else, a "found it in the car park" story, or a human who plugs the medium in
  before running DCS are all outside the tool's reach.

### Known coverage limitations

These are documented rather than fixed, and they matter when you interpret a clean
verdict:

- The built-in engine cannot look inside RAR, 7z or CAB archives. Their contents
  are only inspected when ClamAV is enabled, and the report says so.
- RAR and 7z contents are not scanned by the built-in engine at all, even with
  ClamAV, if the ClamAV engine is unavailable.
- Password-protected and encrypted archives cannot be scanned by anything on the
  machine; they are reported as findings precisely because their contents are
  unknown.
- Files above `scan.max_file_size_mb` (4 GiB by default) are skipped and listed as
  a coverage gap.
- The heuristic engine is not a signature scanner: it catches behaviour and
  indicators, not specific malware families.
- A stale ClamAV signature database produces a warning (`scan.clamav.max_database_age_days`)
  but the scan still runs; DCS never updates signatures itself.

## Safety guarantees

These are the invariants users may rely on. Each one is expected to be covered by
a test, and a report that shows one of them failing is a security issue rather
than a normal bug.

### 1. A malicious image is never uploaded by default

- `upload.upload_when` defaults to `clean`: when the verdict is `suspicious` or
  `infected`, the upload stage is skipped, and it records why.
- The archive is skipped under the same default (`archive.when = clean`), so in the
  default configuration an unclean medium is not even packed.
- Even with `upload_when = always`, the pipeline refuses to publish a non-clean
  archive unless the caller explicitly requested it (`--yes` on the CLI, or the
  corresponding explicit choice in the GUI). This is a separate check, not a
  consequence of the configuration value.
- The GUI asks for confirmation before a job that will upload, naming the host and
  stating that the link is public (`general.confirm_before_upload`, on by default).
  The CLI prints the same warning unless `--yes` is given.

A user who wants to publish a triage image of an infected medium can do so, but
never by accident: three independent defaults have to be changed.

### 2. Nothing from the scanned medium is executed

- The heuristic engine only reads bytes: file headers, up to 64 KiB for the cheap
  checks, up to 2 MiB for scripts and Office documents and up to 4 MiB for PDFs.
  It never unpacks an archive to disk, never resolves a shortcut, never opens a
  special file (device node, fifo, socket - these are reported as findings) and
  never runs a script or a macro.
- ClamAV runs as a separate process, receives a file list, and is invoked with an
  argument vector rather than a shell, so no file name can be interpreted as a
  command.
- The same rule applies to `zip`, `7z`, `lsblk` and `udisksctl`: an argument vector,
  no shell, with a C locale forced so that parsing does not depend on the user's
  locale.
- Archive names, report file names and upload file names are sanitised before use.

### 3. The scanned medium is never modified

- No partition table, no filesystem and no file on the medium is written. The only
  device operation DCS performs is mounting through udisks2 (`udisksctl mount`),
  which is also how a desktop file manager mounts a stick.
- `udisksctl unmount` is offered in the GUI as an explicit user action.
- The archive, the reports and the work files are always written to the configured
  output and report directories, never to the source.

### 4. Scanning happens locally, and secrets stay local

- No file content, file name, hash or finding is transmitted during the scan. The
  only outgoing traffic is the archive you chose to upload and the SMTP message you
  configured.
- The HTML report is self contained: inline CSS, no external fonts, images or
  scripts, nothing that phones home when it is opened or attached to an email.
- File names, paths, threat names and evidence strings are HTML-escaped in the
  report and in the email, so a hostile file name cannot inject markup into either.
- Secrets (SMTP password, gofile token, pixeldrain API key) are resolved from the
  environment or a helper command when available, are masked as `***` in
  `dcs config show`, in the GUI settings page and in the configuration snapshot
  stored inside the JSON result, and are never written to the log.
- A configuration file written by DCS is created with mode `0600`.

### 5. Cancellation is honest

Cancelling stops the work at the next safe point (between files, batches or upload
chunks). No stage is left half-written: an interrupted archive is reported as
failed, and an interrupted job reports verdict `cancelled` rather than a partial
clean result.

## Hardening recommendations

- Run DCS on a machine you are willing to lose, or at least as an unprivileged
  user with no access to important data. If you need a stronger boundary, run it
  inside a virtual machine or a container with only the medium passed through.
- Keep the default `upload_when = clean` and `archive.when = clean` unless you have
  a specific reason to publish an unclean image, and keep
  `general.confirm_before_upload` enabled.
- Keep `email.insecure_tls` off, and `scan.follow_symlinks` off.
- Prefer an upload host whose operator and retention you have actually considered.
  The archive may contain personal data, and a public link is the only access
  control there is. If the material is sensitive, do not upload it: use
  `--no-upload` and move the image over a channel you control.
- Keep ClamAV and its signature database current; `freshclam` is yours to run, DCS
  only warns about a stale database.
- Read the report's coverage gaps section: it names the files that were skipped and
  whether a real antivirus engine took part in the scan.

## Scope for reports

In scope (please report):

- any way to make DCS upload an image when the verdict is not clean without an
  explicit opt-in;
- any way to make DCS execute, extract or follow something from the medium;
- any write to the scanned medium;
- command or argument injection through a file name, a path, a label, a device
  name or a configuration value;
- path traversal or overwriting of files outside the configured output and report
  directories;
- leaking a secret (a password, a token or an API key) into a log, a report, a
  result file, an email or a command line;
- markup or header injection into the HTML report or the email;
- an adapter that hands out more than the intended permissions, or that sends
  credentials to a host other than the configured one.

Out of scope (please use the normal tracker, or do not report at all):

- a malware sample that DCS does not detect, or a false positive, unless it
  demonstrates a systemic flaw (a rule that cannot work, or a check that silently
  skips files it claims to cover);
- vulnerabilities in ClamAV, Qt, Kirigami, Python or the `requests` library -
  report those upstream;
- the policy, availability or retention of an upload provider, or the fact that
  uploaded files are public: that is documented behaviour, not a vulnerability;
- the consequences of running DCS with a configuration, environment or wrapper
  script supplied by somebody else (see "what DCS does not defend against");
- physical attacks, and devices that compromise a machine before DCS runs.

## Disclosure

We prefer coordinated disclosure: a private report, a fix on the development
branch with a regression test, then a release and a public advisory (GitHub
security advisory in the repository, credited to the reporter unless they prefer
otherwise). If you need a CVE, say so in the report and we will help request one.
