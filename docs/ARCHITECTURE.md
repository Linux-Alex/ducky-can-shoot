# Architecture

This document describes how DCS is put together: the module map, the event and
sink design that lets the CLI, the GUI and the tests share one core, the threading
model, the exact behaviour of every pipeline stage, the data structures that form
the public contract, and how to extend the tool.

Everything here is derived from the source in `src/dcs`; file and symbol names are
the real ones.

## Design goals

1. **One core, several front ends.** The scan, archive, upload, report and notify
   logic is written once, in `dcs.core, and is driven by the CLI, by the GUI and by
   the test suite without branching.
2. **The core does not import Qt.** `dcs.core has no module-level Qt import (the
   single exception is a function-local import inside
   `dcs.core.device.open_in_file_manager`, a desktop convenience helper that is not
   used by the pipeline). That is what makes a headless CLI and fast unit tests
   possible.
3. **Everything observable is an event.** The core never prints, never opens a
   window and never knows who is listening. Progress, findings, stage transitions,
   log lines and the final summary travel through one protocol.
4. **One result object.** `JobResult` is the only definition of "what happened in
   a job". Reports, email, CLI output, JSON dumps and the GUI are all derived from
   it.
5. **Failures are reported, not raised.** A hostile or broken medium is the normal
   case for this tool, so unreadable paths, crashed engines and failed uploads
   produce warnings and a verdict rather than a traceback.

## Module map

```text
                            front ends
    +----------------+    +-----------------+    +------------------+
    | dcs/cli.py     |    | dcs/gui/        |    | tests/           |
    | CliSink        |    | SignalSink      |    | CollectingSink   |
    | exit_code_for  |    | models.py       |    | fakes/clamscan   |
    +--------+-------+    +--------+--------+    +---------+--------+
             |                     |                       |
             +----------+----------+-----------+-----------+
                        |                      |
              EventSink protocol     JobRequest / JobResult
              (dcs.core.events)      (dcs.core.pipeline, dcs.core.result)
                        |                      |
                 +------v----------------------v------+
                 |        dcs.core.pipeline          |  Pipeline.run()
                 |  inspect -> scan -> verdict ->    |
                 |  archive -> upload -> report ->   |
                 |  email                            |
                 +--+------+------+------+------+-----+
                    |      |      |      |      |
     +--------------+  +---+--+ +-+----+ |  +---+---------------+
     | dcs.core     |  |scanner| |archi-| |  | dcs.core.report   |
     | .device      |  |package| |ver.py| |  | render_html/text  |
     | lsblk/udisks |  |       | |zip/7z| |  | render_csv        |
     +--------------+  +---+---+ +------+ |  +-------------------+
                           |              |
              +------------+---------+    |  +---------------------+
              | scanner/clamav.py    |    |  | dcs.core.notify     |
              | scanner/heuristic.py |    +--+ smtp.py, templates  |
              | scanner/rules.py     |       +---------------------+
              | scanner/base.py      |
              +----------------------+       +---------------------+
                                             | dcs.core.upload     |
                 dcs.core.config ----------->| registry -> gofile  |
                 dcs.core.events            | simple (host specs) |
                 dcs.core.result            +---------------------+
                 dcs.core.log
                 dcs.core.util
```

Dependency direction is strictly downwards: the front ends import `dcs.core, the
core never imports a front end, and no core module imports another front end. The
logging module (`dcs.core.log`) is the only one that knows about the GUI's needs,
and even then only through a plain listener callback.

### What each module owns

| Module | Responsibility |
| --- | --- |
| `dcs/version.py` | Version, application id, home page, license strings. Single source of truth; `pyproject.toml` mirrors the version. |
| `dcs/cli.py` | Argument parsing, the `CliSink` progress renderer, the doctor check list, and the exit-code mapping. |
| `dcs/core/config.py` | The configuration dataclasses, TOML load/merge/save, secret resolution, validation. |
| `dcs/core/events.py` | `Stage`, `StageState`, `Severity`, `Verdict`, `Finding`, `StageProgress`, `ProgressMeter`, `ScanStats` and the sink implementations. |
| `dcs/core/result.py` | `JobResult`, `EmailResult`, `StageTiming` and their `to_dict()` contract. |
| `dcs/core/pipeline.py` | `JobRequest`, `Pipeline` and the per-run `_RunContext` that implements the seven stages and every skip rule. |
| `dcs/core/scanner/__init__.py` | `Scanner` (two pass walk, batching, engine dispatch, progress) and `compute_verdict()`. |
| `dcs/core/scanner/base.py` | `ScanEngine` protocol, `EngineStatus`, `ScanContext`. |
| `dcs/core/scanner/clamav.py` | ClamAV probing (clamdscan vs clamscan), file-list batches, output parsing. |
| `dcs/core/scanner/heuristic.py` | The built-in engine: name, hash, magic, PE/ELF/OLE/ZIP/PDF/LNK checks and text indicators. |
| `dcs/core/scanner/rules.py` | All detection data: extension sets, indicator tables, regexes, thresholds, limits. No logic. |
| `dcs/core/archiver.py` | Archive creation through `zip`/`7z` with a Python fallback, volume collection, capacity checks. |
| `dcs/core/upload/base.py` | `UploadProvider` protocol, `BaseProvider`, `UploadResult`, `ProgressReader`. |
| `dcs/core/upload/registry.py` | `provider_ids()`, `provider_catalog()` and `build_provider()`. |
| `dcs/core/upload/gofile.py` | The gofile adapter, with store-host caching and fallback hosts. |
| `dcs/core/upload/simple.py` | One implementation driven by `SimpleHostSpec` for the single-POST hosts. |
| `dcs/core/notify/smtp.py` | `Notifier`: message construction, STARTTLS/SSL, auth, dry-run to `.eml`, connection test. |
| `dcs/core/notify/templates.py` | Subject line and the text and HTML bodies. |
| `dcs/core/report.py` | HTML (self contained), text, findings CSV and JSON writers. |
| `dcs/core/device.py` | lsblk parsing, removable-media filtering, udisksctl mount/unmount. |
| `dcs/core/installer.py` | Package-manager detection, the ClamAV installation plan, the generated script and its runner (pkexec, terminal, sudo). |
| `dcs/core/util.py` | Formatting, the tolerant file walker, hashing, atomic writes, subprocess helpers. |
| `dcs/core/log.py` | Logging setup plus a ring buffer with listeners, so the GUI can show the tail. |
| `dcs/gui/backend.py` | The object QML talks to: models, device polling, job control, settings, desktop integration. |
| `dcs/gui/worker.py` | `PipelineWorker` (runs one job on a thread) and `SignalSink`. |
| `dcs/gui/models.py` | Five list models: devices, stages, findings, log, throughput. |
| `dcs/gui/app.py` | Qt application setup, QML import paths, window geometry persistence. |
| `dcs/gui/qml/` | `Main.qml` plus `views/` and `components/`. No logic worth testing. |

## The event and sink design

The core pushes five kinds of event into an object that satisfies the structural
`EventSink` protocol:

```python
class EventSink(Protocol):
    def on_stage(self, stage: Stage, state: StageState, message: str = "") -> None: ...
    def on_progress(self, progress: StageProgress) -> None: ...
    def on_finding(self, finding: Finding) -> None: ...
    def on_log(self, level: str, message: str) -> None: ...
    def on_summary(self, summary: dict[str, Any]) -> None: ...
```

Four implementations ship with the project, and they are the whole reason the same
core drives every front end:

| Sink | Used by | Behaviour |
| --- | --- | --- |
| `NullSink` | default argument | Throws everything away, so a core function can be called without a sink. |
| `CollectingSink` | tests | Records stages, progress, findings, log lines and summaries; exposes `final_summary`, `stage_states()` and `max_percent` for assertions. |
| `MultiSink` | fan-out | Forwards to several sinks and swallows exceptions, so one broken listener cannot stop a job. |
| `CliSink` (`dcs/cli.py`) | `dcs scan`, `dcs run` | One updating line on a terminal, plain lines otherwise, plus a finding line per finding. |
| `SignalSink` (`dcs/gui/worker.py`) | the GUI | A `QObject` whose methods emit Qt signals carrying plain dictionaries. |

Events are always plain dataclasses or dictionaries. Nothing that crosses the
boundary holds a Qt object, a file handle or a live thread:

- `StageProgress.to_dict()` pre-formats the numbers (`bytesProcessedText`,
  `rateText`, `etaText`, `percent`) so the GUI and the CLI never re-implement
  formatting.
- `Finding.to_dict()` adds `name`, `sizeText` and `severityRank` so a model can sort
  without knowing about the `Severity` enum.
- `JobResult.to_dict()` is the summary passed to `on_summary`, the JSON written by
  the report stage, the payload of `dcs run --json` and the dictionary the GUI
  renders the result page from.

`SignalSink` deliberately does **not** inherit from `EventSink`: the protocol is
structural, and inheriting it would drag `ABCMeta` into a metaclass conflict with
`QObject`.

Progress throttling lives in `ProgressMeter`, not in the sinks: it keeps a sliding
window of samples (8 seconds by default), computes a smoothed rate, derives an ETA
only when the rate is meaningful, and returns `None` instead of a snapshot when the
last emission is more recent than `general.ui_refresh_ms`. Forcing a snapshot
(`force=True`) is used at batch boundaries and for the final state, so a stage
always ends at 100 percent.

## Threading model

The core is single-threaded. Concurrency exists only where the front end adds it:

| Thread | What runs there |
| --- | --- |
| main (GUI) thread | Qt event loop, all models, the backend facade, QML. |
| one `QThread` per job | `PipelineWorker.run()`, which calls `Pipeline.run()`. |
| short-lived plain threads | `AsyncRunner`, used by the settings page for "Test SMTP" and "Test provider". |

The pipeline runs on a worker thread because a scan of a large stick takes minutes;
moving it off the GUI thread is what keeps the window responsive. The mechanics:

1. `Backend.startJob()` creates a `QThread` and a `PipelineWorker`, calls
   `worker.moveToThread(thread)` and connects `thread.started` to `worker.run`.
2. The worker owns a `SignalSink` (created on the GUI thread, connected to the
   worker's signals) and a `threading.Event` for cancellation.
3. `worker.run()` calls `Pipeline(self.config).run(request, sink=self.sink,
   cancel=self.cancel_event)`. Every event the core emits therefore becomes a Qt
   signal.
4. Qt delivers a signal emitted from a non-GUI thread through the event loop of the
   thread that owns the receiver, so the connected slots (`StagesModel.set_state`,
   `Backend._on_progress`, `FindingsModel.append`, `Backend._on_summary`, ...) run on
   the GUI thread. **The models only ever mutate on the GUI thread.**
5. `worker.finished` (or `failed`) quits the thread; `thread.finished` deletes both
   objects.

Two more marshalling details are worth knowing, because they are easy to get
wrong:

- The application log is a Python logging ring buffer that can be written from any
  thread. `LogModel` is a Qt model, so the ring listener wraps each record in
  `QTimer.singleShot(0, ...)` to hand it to the GUI thread. Before the window
  exists, records are copied out of the ring in one batch.
- Cancellation is a `threading.Event`. The core checks it between files, between
  batches and between upload chunks; the GUI sets it from the GUI thread. Nothing
  is interrupted mid-syscall, so "Cancel" means "stop after the current item",
  which the dialog says explicitly.

Because the core has no Qt imports, none of this is required to use it: the CLI
runs the pipeline on the calling thread, and the tests run it with a
`CollectingSink`.

## Installing the optional ClamAV engine

ClamAV is optional, and a machine without it only runs the built-in heuristics.
Installing a package needs root, and a GUI process has no terminal to type a
password into, so `dcs/core/installer.py` splits the problem in three:

1. **Detection.** `detect_package_manager()` walks a fixed order (apt, dnf,
   zypper, pacman, apk, xbps, eopkg, emerge, brew) and returns the first
   manager whose binary exists. `distro_name()` reads `/etc/os-release` for
   the human readable name. Package names differ per family, so they live in
   the `PACKAGE_MANAGERS` table rather than in code.
2. **A plan, never a command.** `build_plan()` produces an `InstallPlan`:
   the ordered `InstallStep`s (refresh index, install, fetch signatures,
   enable the service), a rendered shell script, and the privilege method.
   Nothing is executed at this point, so both the GUI dialog and
   `dcs install-clamav --dry-run` can show exactly what would run.
3. **Privilege delegation.** `_argv_for()` turns the plan into one command
   line: `pkexec /bin/bash <script>` when a polkit agent is running (the
   graphical password dialog on Plasma), `<terminal> -e bash -c 'sudo ...'`
   when one is not, plain `sudo bash <script>` for the CLI, or nothing at all
   when DCS already runs as root.

The generated script is the whole contract with the system: a linear list of
fixed commands, written with mode 0700 into the cache directory, printed in
full in the interface. Each step is wrapped in a `step` shell function that
prints `=== DCS step N: title ===` and `=== DCS step N finished with exit X ===`
markers, which the runner parses back into per-step outcomes; a failing required
step exits with `10 * N` so the failure can be attributed without guessing.

The password never enters DCS: polkit or sudo collects it, and the runner only
streams what those print. Cancellation terminates the whole process group
(`start_new_session=True` plus `os.killpg`), because killing the shell alone
would leave a running package manager behind; output is drained on a helper
thread so a silent step such as `freshclam` stays cancellable.

Afterwards `verify()` re-probes ClamAV through the scanner module, so the
result page, the log and `dcs engines` all report the same truth.

## Stage-by-stage flow

`Stage` is an ordered enum; `STAGE_ORDER` defines the sequence and the GUI shows
exactly these rows:

| # | Stage | Title | What it does |
| --- | --- | --- | --- |
| 1 | `inspect` | Inspect device | Resolve the source to a mounted directory, gather drive metadata, check free space, choose the engines. |
| 2 | `scan` | Antivirus scan | Walk every file and run the enabled engines. |
| 3 | `verdict` | Evaluate verdict | Combine findings into `clean`, `suspicious` or `infected`. |
| 4 | `archive` | Create archive image | Compress the medium into one image (optionally split). |
| 5 | `upload` | Upload to cloud | Push the image to the configured host. |
| 6 | `report` | Build report | Render HTML, text, CSV and JSON. |
| 7 | `email` | Send notification | Send the verdict and the share link over SMTP. |

Every stage moves through `StageState`: `pending`, `running`, `done`, `failed`,
`skipped` or `cancelled`. The pipeline announces all seven as `pending` before the
first stage starts, so a fresh window already knows the shape of the job;
`_RunContext._record` keeps a `StageTiming` per stage for the timeline in the
report.

Two bookkeeping steps run in the `finally` block of `Pipeline.run()`, because a
cancelled or crashed run would otherwise leave rows stuck on "pending":

- `settle_pending()` marks every stage that never ran as `skipped`, with the reason
  "the run was cancelled" or "the run ended before this stage";
- `refresh_timings()` rebuilds `JobResult.timings` in `STAGE_ORDER`, so the report
  timeline contains the stages that finished after the report itself was written.

The order of the stage calls also encodes the cancellation contract:
`inspect`, `decide` and `report` always run, while `scan`, `archive`, `upload` and
`notify` are guarded by `if not cancel.is_set()`. A cancelled job therefore still
produces a verdict and a report rather than a truncated result.

### 1. inspect

- If the request names a device (`device_path`, or a `source_path` starting with
  `/dev/`), the device is looked up with lsblk. A device that is not mounted is
  mounted through `udisksctl mount` (polkit prompts if it has to); a failure
  raises, which fails the stage.
- Otherwise `device_for_path()` finds the block device that backs the directory
  (longest mount point wins). This is advisory only: scanning a plain directory is
  supported.
- The source must be an existing directory. The label becomes the device's display
  name (label, else model, else kernel name), else the directory name.
- The output directory is created; free space is logged.
- Warnings, never failures: a plain directory has no drive metadata, and a source
  that is not on removable media gets "make sure you really want to triage it".

### 2. scan

- `Scanner(config, collect_files=report.include_file_list,
  ui_interval_ms=general.ui_refresh_ms)` is built; `statuses()` probes every
  configured engine.
- **Skip rule**: when no configured engine is available, the stage is marked
  `skipped` with the reason "no scan engine is available", a `ScanResult` with
  verdict `error` is recorded, the job verdict becomes `error` and the warning
  "no usable antivirus engine: the medium was not scanned" is added. Nothing is
  archived or uploaded afterwards, because the verdict is not clean.
- Pass 1 enumerates the tree to count files and bytes (`files_total`,
  `bytes_total`) so percentages mean something. Nothing is kept in memory except
  counters, so a million-file stick is fine.
- Pass 2 walks the tree again and builds batches of at most
  `max(32, scan.clamav.batch_size)` entries. Files larger than
  `scan.max_file_size_mb` are not scanned; they are collected in
  `ScanResult.skipped_large` and rendered as a coverage gap in the report. Special
  files (device nodes, fifos, sockets) are passed to the engines so the heuristic
  engine can report them.
- Each batch is handed to every usable engine in order. Only the last engine
  receives `ctx.tick`, so two engines cannot count the same file twice.
- Findings are de-duplicated by (path, threat), their SHA-256 is filled in when
  `scan.hash_findings` is on (first 64 MiB), and each one is emitted through
  `on_finding` and mirrored into `on_log`.
- `scan.max_findings` stops the scan by setting the cancel event, with a warning
  explaining why.
- An engine that raises on a batch does not lose the run: the exception is logged,
  recorded as a warning and the next batch continues.
- When the walk finishes, files the engines never ticked are counted as processed
  (unless the run was cancelled), and `ScanStats.errors` combines the engine error
  lists with the unreadable paths (truncated to 200 entries).
- The stage ends `done` (or `cancelled`) with a message such as
  "3 threatening file(s) found".

### 3. verdict

The verdict is computed by `compute_verdict()` and copied onto the job. Stage
failure case: if `ScanResult` does not exist at all (the scan was not requested),
the verdict is `error` and the stage is marked `failed` with "the scan did not
run".

```python
def compute_verdict(findings, *, blocking_severity="high",
                    cancelled=False, files_scanned=0, errors=0):
    if cancelled:
        return Verdict.CANCELLED
    if errors and files_scanned == 0:
        return Verdict.ERROR
    if not findings:
        return Verdict.CLEAN
    blocking = severity_rank(blocking_severity)
    highest = max(severity_rank(finding.severity) for finding in findings)
    if highest >= max(blocking, severity_rank(Severity.HIGH)):
        return Verdict.INFECTED
    if highest >= severity_rank(Severity.MEDIUM):
        return Verdict.SUSPICIOUS
    return Verdict.CLEAN
```

In words: a cancelled run is `cancelled`; a run that produced only errors and
scanned nothing is `error`; no findings is `clean`; anything at or above the
effective threshold (never lower than `high`) is `infected`; anything at `medium` or
above is `suspicious`; `low` and `info` findings are reported but do not change the
verdict. `JobResult.threat_count` counts findings at `high` or above, which is the
number the report and the email call "threatening files".

### 4. archive

The exact skip rules, in the order they are evaluated (`_archive_wanted()`):

1. the request disabled archiving for this run (`--no-archive`);
2. the request did not ask explicitly and `archive.enabled` is false;
3. `archive.when = "never"`;
4. `archive.when = "clean"` and the verdict is not `clean` - the reason recorded is
   "the medium is ...: no archive was created and nothing was uploaded, so malware
   never leaves this machine".

A skip also stores the reason in `JobResult.upload_skipped_reason`, which is what
the report, the email and the result page show instead of a link.

When the archive is wanted:

- `plan_source()` counts files and bytes and remembers the relative path to size
  mapping (capped at 200 000 entries) so archive progress can be byte accurate.
- An empty medium is skipped with "the medium is empty: nothing to archive".
- A source above `archive.size_warn_gb` produces a warning that free hosts may
  refuse the image.
- The name is `DCS_<label>_<YYYYmmdd-HHMMSS>.<zip|7z>`, with `label` replaced by
  `usb` when `archive.use_device_label` is false; the name is sanitised.
- `Archiver.create()` first checks capacity: the target filesystem must have at
  least `archive.min_free_space_ratio` times the source size free, otherwise the
  stage fails with a message naming both numbers.
- The external tool is used when present (`zip` for zip, `7z` or `7za` for 7z);
  it is run with the source directory as the working directory and asked to walk
  "." itself, which keeps the command line short and memory flat. `-r` with a
  compression level for zip, `a -t7z -mx=N` for 7z, plus the split flags
  (`-s <n>m` or `-v<n>m`) when `archive.split_size_mb` is set.
- Progress comes from parsing the tools' own output lines ("adding: path",
  "+ path"), matched against the size map. A cancelled run terminates the child
  process.
- Return codes 0, 1 (warnings) and 18 (some files unreadable) are tolerated; a run
  that produced no file at all fails the stage.
- Without the tools, the Python `zipfile` fallback writes the archive, cannot
  split (it says so in a warning) and reports progress itself.
- Volumes are collected afterwards: zip volumes are ordered `.z01, .z02, ..., .zip`
  (the `.zip` carries the central directory and must be uploaded last), 7z volumes
  are ordered `.001, .002, ...`.
- Finally the primary volume is hashed (SHA-256) for the report.

### 5. upload

Skip rules (`_upload_wanted()`), in order:

1. the request disabled uploading (`--no-upload`);
2. the request did not ask explicitly and `upload.enabled` is false;
3. there is no archive, or the archive failed ("there is no archive to upload");
4. `upload.upload_when = "never"`;
5. `upload.upload_when = "clean"` and the verdict is not `clean` - "nothing was
   uploaded, so no link to malicious files can leak";
6. the verdict is not `clean` and the caller did not pass `assume_yes` ("refusing
   to publish a suspicious image without confirmation").

Rule 6 is the belt to rule 5's braces: it also covers `upload_when = "always"`, so
publishing a non-clean image always takes a deliberate `--yes` (CLI) or an
explicit GUI action.

When the upload is wanted:

- `build_provider(config, secrets=...)` instantiates the configured adapter and
  `availability()` decides whether it can be used at all (a reachable store host
  for gofile, an API key for pixeldrain). An unusable provider fails the stage with
  an explanation instead of trying.
- A warning is logged before the first byte: the file will be publicly reachable
  by anyone who has the link.
- A multi-volume image is grouped into one folder when the adapter supports it
  (gofile); a failure there is logged and ignored.
- Each volume is uploaded in order, with a progress callback fed by
  `ProgressReader` (which reports every 1 MiB chunk written to the socket) and with
  the cancel event honoured inside `read()`. A failed volume stops the loop.
- `JobResult.share_link` is the folder link when every volume landed in one folder,
  the single link for one volume, or the newline separated list of links.
- With `upload.delete_archive_after_upload` or `general.keep_archive = false` the
  local parts are deleted once every part uploaded successfully.

### 6. report

Skipped entirely when `report.enabled` is false. Otherwise `write_reports()` writes
four files into `report.directory` (or the XDG default), atomically:

| File | Content |
| --- | --- |
| `<stem>.html` | Self contained report with inline CSS, a verdict card, statistics, drive metadata, the engine table, findings with their evidence, archive and upload details, the notification block, the timeline, warnings and coverage gaps. Adapts to a dark colour scheme. |
| `<stem>.txt` | The same information as plain text, for a mailbox without HTML. |
| `<stem>.findings.csv` | Every finding: severity, engine, category, threat, path, size, sha256, detail. Always complete, regardless of `report.max_findings`. |
| `<stem>.json` | `JobResult.to_dict()`, when `report.write_json` is on. |

The stage fails only when no HTML file could be written; a missing JSON or CSV is
logged, not fatal.

The pipeline also calls the report stage from the `finally` block of a crashed run
when no report exists yet, so a failed job still leaves an artefact behind.

Because the report is written before the email stage runs, the JSON file is
rewritten once at the very end of `Pipeline.run()` (`_refresh_json_report()`) with
the final summary, so the machine readable artefact matches what the CLI prints
and what the GUI shows, including the email outcome.

### 7. email

Skip rules (`_email_wanted()`), in order:

1. the request disabled email (`--no-email`);
2. the request did not ask explicitly and `email.enabled` is false;
3. no recipient is configured (neither the request nor `email.to_addrs`);
4. `email.always_send = false` and the medium is clean.

Recipients passed for one run (`--to`) are merged into the configuration so the
message headers, the `EmailResult` and the report agree.

`Notifier` builds a multipart message with a text and an HTML alternative, sets
`X-DCS-Job` and `X-DCS-Verdict` headers, and attaches the reports selected by
`report.format` plus the findings CSV. With `--dry-run-email` (or `email_dry_run`
in a `JobRequest`) the message is written as a `.eml` file into `email.dry_run_dir`
(or `~/.local/share/dcs/outbox`) and nothing is sent. The stage is marked `done`
with the difference spelled out, and a delivery failure is a stage failure plus a
warning, never an exception that would lose the rest of the result.

## Skip rules at a glance

| Stage | Skips when |
| --- | --- |
| scan | no configured engine is available (verdict becomes `error`) |
| archive | `--no-archive`; or `archive.enabled = false`; or `archive.when = never`; or `archive.when = clean` and the verdict is not clean; or the medium is empty; or the archive failed |
| upload | `--no-upload`; or `upload.enabled = false`; or the archive is missing or failed; or `upload.upload_when = never`; or `upload_when = clean` and the verdict is not clean; or the verdict is not clean and `assume_yes` was not given; or the provider is unusable |
| report | `report.enabled = false` |
| email | `--no-email`; or `email.enabled = false`; or no recipient; or `always_send = false` and the medium is clean |

A skipped stage is not an error: it is recorded as `skipped` with a human readable
reason, which appears in the log, in the report timeline, in the GUI and in
`JobResult.upload_skipped_reason` for the two stages whose absence the recipient
needs to understand.

## Data structures and the `to_dict()` contract

Every structure that crosses a boundary is a dataclass with a `to_dict()` method.
The conventions are deliberate:

- keys are lowerCamelCase in JSON, snake_case in Python;
- pre-formatted display strings (`sizeText`, `durationText`, `bytesProcessedText`,
  `rateText`, `etaText`) are computed once in the core, so the CLI, the QML views
  and the templates never format numbers themselves;
- an unknown ETA is `None` in Python and `-1.0` in JSON, because QML properties are
  typed and a null float is awkward there;
- enums are serialised as their lowercase value (`str(Verdict.INFECTED) ==
  "infected"`), which is why every enum overrides `__str__`.

| Structure | Defined in | Fields | `to_dict()` keys |
| --- | --- | --- | --- |
| `Finding` | `core/events.py` | path, threat, engine, severity, category, size, sha256, detail, line | `path`, `name`, `threat`, `engine`, `severity`, `severityRank`, `category`, `size`, `sizeText`, `sha256`, `detail`, `line` |
| `StageProgress` | `core/events.py` | stage, state, processed, total, bytes_processed, bytes_total, current, message, rate, elapsed, eta, percent | `stage`, `title`, `icon`, `state`, `processed`, `total`, `bytesProcessed`, `bytesTotal`, `bytesProcessedText`, `bytesTotalText`, `current`, `message`, `rate`, `rateText`, `elapsed`, `elapsedText`, `eta`, `etaText`, `percent` |
| `EngineStatus` | `core/scanner/base.py` | engine_id, label, available, version, detail, warnings | `id`, `label`, `available`, `version`, `detail`, `warnings` |
| `ScanStats` | `core/events.py` | files_scanned, bytes_scanned, files_skipped, bytes_total, files_total, errors, duration, engines, unreadable | `filesScanned`, `bytesScanned`, `filesSkipped`, `filesTotal`, `bytesTotal`, `errors`, `duration`, `durationText`, `engines`, `bytesScannedText`, `bytesTotalText` |
| `ScanResult` | `core/scanner/__init__.py` | root, verdict, findings, stats, engines, warnings, unreadable, skipped_large, started_at, finished_at, cancelled, file_list | `root`, `verdict`, `maxSeverity`, `findings`, `findingCount`, `infectedCount`, `stats`, `engines`, `warnings`, `limitedCoverage`, `unreadableCount`, `skippedLarge`, `startedAt`, `finishedAt`, `cancelled`, `fileList` |
| `ArchivePart` | `core/archiver.py` | path, size | `path`, `size`, `sizeText` |
| `ArchiveResult` | `core/archiver.py` | ok, parts, source_bytes, source_files, duration, tool, command, error, warnings | `ok`, `parts`, `partCount`, `primaryPath`, `sourceBytes`, `sourceBytesText`, `sourceFiles`, `archiveBytes`, `archiveBytesText`, `duration`, `tool`, `command`, `error`, `warnings` |
| `UploadResult` | `core/upload/base.py` | ok, link, provider, path, size, duration, detail, raw, error, warnings | `ok`, `link`, `provider`, `path`, `size`, `duration`, `detail`, `error`, `warnings` |
| `EmailResult` | `core/result.py` | ok, sent, dry_run, recipients, subject, server, duration, error, message_path, body_preview | `ok`, `sent`, `dryRun`, `recipients`, `subject`, `server`, `duration`, `error`, `messagePath`, `bodyPreview` |
| `StageTiming` | `core/result.py` | stage, state, seconds, message | `stage`, `state`, `seconds`, `secondsText`, `message` |
| `JobResult` | `core/result.py` | job_id, source_path, source_label, device, verdict, scan, archive, archive_sha256, uploads, upload_skipped_reason, share_link, report_paths, email, warnings, timings, started_at, finished_at, duration, cancelled, config_snapshot, app_version | `jobId`, `appVersion`, `startedAt`, `finishedAt`, `duration`, `durationText`, `sourcePath`, `sourceLabel`, `device`, `verdict`, `safe`, `threatCount`, `findingCount`, `filesScanned`, `bytesScanned`, `bytesScannedText`, `archive`, `archiveBytes`, `archiveBytesText`, `archiveSha256`, `uploads`, `uploadLinks`, `shareLink`, `uploadSkippedReason`, `reports`, `email`, `warnings`, `timings`, `cancelled`, `scan` |

`JobResult` also exposes derived properties the renderers use: `safe` (verdict is
clean), `findings` (empty when there is no scan), `findings_sorted` (severity
descending, then path), `threat_count` (findings at `high` or above), `files_scanned`,
`bytes_scanned`, `archive_bytes`, `upload_links`, `stage_states` and
`failed_stages()`.

`config_snapshot` is a full configuration dump with secrets masked to `***`, so a
result file can be audited later without leaking a password.

## Error handling philosophy

The nature of the tool decides the strategy: a medium that is damaged, badly
formatted or actively hostile is the expected input, not an exception.

- **Unreadable paths are data, not failures.** `walk_files()` calls an `on_error`
  callback and keeps going; the scanner records the path in `ScanResult.unreadable`
  and the report prints it under "coverage gaps".
- **An engine that crashes loses a batch, not the run.** The batch is caught,
  logged and recorded as a warning; the other engine still runs.
- **Upload and archive failures become stage failures plus warnings**, so the
  report and the email still get written and the user learns why there is no link.
- **The pipeline has a top-level catch.** Any unexpected exception marks the
  currently running stage as `failed`, adds "internal error: ..." to the warnings
  and forces the verdict to `error` when nothing was scanned. The `finally` block
  then writes a report if none exists and always emits the summary, so a crash on a
  weird medium still produces a result object.
- **The CLI never dumps a traceback at the user.** `main()` catches everything,
  logs it at debug level with the traceback and prints a one-line error, returning
  exit code 2.
- **Delivery failures do not raise.** `Notifier.send()` returns an `EmailResult`
  with `ok=False` and an `error` string; the pipeline turns that into a failed stage
  and a warning.
- **Sinks are defensive.** `MultiSink` swallows exceptions from its children and
  `ScanContext.tick_file()` swallows exceptions from the progress callback, because
  a bug in a progress bar must never abort a scan.
- **No exception is used for control flow across stages.** Skip decisions are
  boolean returns plus a reason string.

## How to add a stage

A stage is a method on `_RunContext` plus a place in the sequence. Concretely:

1. Add the member to the `Stage` enum in `core/events.py`, in execution order, and
   to `STAGE_ORDER`. Add entries to `STAGE_TITLES`, `STAGE_DESCRIPTIONS` and
   `STAGE_ICONS` as well: the GUI builds its stage list from those dictionaries, so
   a missing entry shows up as a blank row.
2. Add the call to `_RunContext.stages_pending()` (the `order` list) and to
   `Pipeline.run()`, with the same `if not cancel.is_set()` guard the other stages
   use.
3. Write the method. Follow the existing shape: `self._begin(stage, message)`,
   optional early `self._skip(stage, reason)`, work with progress through
   `self.sink.on_progress(...)`, then `self._finish(stage, state, message)`. Record
   anything the report needs on `self.result`.
4. Add a field to `JobResult` (and to its `to_dict()`) if the stage produces
   something the report, the email or the GUI must show.
5. Report the stage in `render_html`, `render_text` and, if it matters to the
   recipient, in the email templates.
6. Test it with a `CollectingSink` and assert on `stage_states()` and
   `final_summary`, the way the existing tests do.

Do not let a new stage raise: the pipeline's top-level handler is a safety net for
bugs, not a design.

## How to add an engine

```python
class MyEngine:
    engine_id = "myengine"
    label = "My engine"

    def status(self) -> EngineStatus: ...
    def scan_batch(self, entries: Sequence[FileEntry], ctx: ScanContext) -> Iterable[Finding]: ...
```

- Register it in `Scanner.build_engines()` and accept its id in the configuration
  validation (`AppConfig.validation_problems()`) and in the CLI's `--engines` help.
- Yield `Finding` objects; do not append them to a result yourself.
- Call `ctx.tick_file(size, path)` for every file you finished, so progress still
  works when your engine is the last one in the list. It is a no-op when the
  orchestrator did not install a tick (because another engine reports it).
- Honour `ctx.cancelled`. Long external commands should be terminated rather than
  left running.
- Never raise out of `scan_batch`; report problems through `ctx.log(...)`.
- Report availability honestly in `status()`: `available`, `version`, `detail` and
  `warnings` are all shown in the GUI, in `dcs engines` and in the report, and a
  missing engine is one of the most common support questions.

## Contracts that must not change silently

Three things are effectively public API, because scripts, tests and the whole GUI
depend on them:

1. **The `JobResult.to_dict()` key names.** The JSON report, `dcs run --json` and
   every QML binding on the result page read them.
2. **The event and stage names.** `Stage` and `StageState` values appear in the
   JSON, in the models and in the report timeline; the stage list is preseeded from
   `STAGE_ORDER` before a job starts.
3. **The CLI exit codes** (0 clean, 1 suspicious or infected, 2 error or
   cancelled), which is why the mapping lives in a single function,
   `exit_code_for()`.

Adding keys and stages is fine; renaming or reordering them breaks users.

## Testing

The test suite drives the same core the GUI uses, with no Qt:

- `CollectingSink` records every event, so a test can assert on stage transitions,
  progress percentages and findings without a display.
- `tests/fakes/clamscan` and `tests/fakes/clamdscan` are Python stand-ins for the
  ClamAV front ends. They implement just enough of the CLI (`--version`, `--ping`,
  `--file-list`, `--verbose`, `--no-summary`) and derive results from file names,
  which makes it possible to test the parsing, the batch handling and the error
  paths deterministically.
- The pipeline is exercised through `Pipeline(config).run(JobRequest(...),
  sink=..., cancel=threading.Event())` with temporary directories, so no test needs
  a real USB stick or a network.
