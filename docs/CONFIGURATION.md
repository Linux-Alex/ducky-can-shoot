# Configuration

DCS is configured with a single TOML file. Everything in this document is
derived from `src/dcs/core/config.py` and can be reproduced:

```bash
dcs config show          # the effective configuration, secrets masked
dcs config show --show-secrets
dcs config path          # which file would be read and written
dcs config init          # create the per-user file if it does not exist
```

## File locations and layering

Sources are applied in this order, and a later source wins:

| Order | Source | Notes |
| --- | --- | --- |
| 1 | built-in defaults | The dataclass defaults in `dcs/core/config.py`. |
| 2 | `/etc/dcs/config.toml` | System wide. Skipped when the file does not exist. |
| 3 | `~/.config/dcs/config.toml` | Per user; respects `XDG_CONFIG_HOME`. This is what the GUI settings page writes. |
| 4 | the file passed to `--config PATH` | Replaces the per-user file and becomes the save target. |
| 5 | environment variables | See the table further down. |

Unknown keys are ignored with a debug message rather than treated as an error, so
a configuration written by a newer version still loads. Values are coerced to the
declared type (a string `"true"` becomes a boolean, `"587"` becomes an integer).

A file written by DCS is created atomically (temporary file plus rename) with mode
`0600`, because it may contain a password if the user chose to store one.

The repository also ships `examples/config.toml`, the same file with commentary
about what each section decides and why the secrets belong elsewhere.

## Derived paths

| Path | Where it comes from | Default |
| --- | --- | --- |
| Archive output | `general.output_dir` | `$XDG_DATA_HOME/dcs` (usually `~/.local/share/dcs`) |
| Reports | `report.directory` | `$XDG_DATA_HOME/dcs/reports` |
| Work files | no configuration key | `$XDG_DATA_HOME/dcs/work` |
| Config file | layering above | `$XDG_CONFIG_HOME/dcs/config.toml` |

A leading `~` in either configured path is expanded. The directories are created
on demand.

## Sections

Every key in the default file is listed here. Defaults are the values DCS uses
when nothing overrides them.

### `[general]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `confirm_before_upload` | bool | `true` | Ask before a job that will upload data. The GUI shows a dialog naming the host; the CLI prints a notice unless `--yes` is given. Set to `false` only on a machine where uploading is unremarkable. |
| `keep_archive` | bool | `true` | Keep the local archive after a successful upload. When set to `false` the archive parts are deleted once every part has been uploaded. |
| `output_dir` | string | `""` | Where archives are written. Empty means the XDG default. `--output-dir` overrides it for one run, and `DCS_ARCHIVE_DIR` overrides it globally. |
| `ui_refresh_ms` | int | `100` | Minimum delay between two progress updates, in milliseconds. It is the throttle of `ProgressMeter`, so a large value reduces GUI and terminal traffic during a scan. |

### `[scan]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `engines` | list of strings | `["clamav", "heuristic"]` | Engine ids to run, in order: `clamav`, `heuristic`, `yara`, `intel`. Anything else is a configuration problem, and so is an empty list. `--engines` and `DCS_ENGINES` (comma separated) override it. |
| `max_file_size_mb` | int | `4096` | Files larger than this are not handed to the engines and are listed in the report under "coverage gaps". `0` means no limit. The heuristic engine also honours it. |
| `max_findings` | int | `0` | Stop the scan after this many findings, recording a warning. `0` means keep going. Useful for a first look at a very dirty stick. |
| `max_files` | int | `0` | Stop the walk after this many files. `0` means all of them. |
| `follow_symlinks` | bool | `false` | Follow symbolic links while walking. Off by default: a link on a hostile stick can point anywhere on the triaging machine. |
| `include_hidden` | bool | `true` | Include dot files and dot directories. Hidden files are exactly where a payload likes to sit, so this is on by default. |
| `hash_findings` | bool | `true` | Compute the SHA-256 of every finding (up to the first 64 MiB of each file) so the report can be compared against other tools. Slower on a medium with large findings. |
| `exclude_globs` | list of strings | `["System Volume Information/*", "$RECYCLE.BIN/*", "*.tmp"]` | fnmatch patterns skipped during the walk. A pattern without a slash also matches the file name anywhere in the tree. |
| `blocking_severity` | string | `"high"` | The lowest severity that makes the verdict `infected`. One of `info`, `low`, `medium`, `high`, `critical`; the effective threshold is never lower than `high`. Setting `medium` makes the tool stricter. |

### `[scan.clamav]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | When false, the `clamav` engine is skipped even if it is listed in `scan.engines`. This is the switch to flip if ClamAV is installed but unusable. |
| `prefer_daemon` | bool | `true` | Prefer `clamdscan` (which talks to a running clamd and is much faster on many small files) over the stand-alone `clamscan`. If the daemon does not answer, DCS falls back automatically and says so. |
| `clamdscan_binary` | string | `"clamdscan"` | Name or path of the daemon client. |
| `clamscan_binary` | string | `"clamscan"` | Name or path of the stand-alone scanner. |
| `extra_args` | list of strings | `[]` | Extra command line arguments appended to every ClamAV invocation, for example `["--max-filesize=100M"]`. |
| `batch_size` | int | `400` | How many files go into one invocation (through a file list). The effective value is at least 32. Larger batches mean fewer database loads with `clamscan` and a coarser progress granularity. |
| `max_database_age_days` | int | `7` | Warn in `dcs engines`, the GUI and the report when the newest signature file is older than this. DCS never runs `freshclam` for you. |

### `[scan.yara]`

YARA rules: the second signature engine, and the one whose rules you choose.
It is **off by default** because an engine with no rules would report "clean"
without having looked at a single pattern.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Run the `yara` engine when it is listed in `scan.engines`. |
| `binary` | string | `"yara"` | The binary to run. The classic `yara` command line is what DCS drives; YARA-X's `yr` is reported as an installation problem instead of being run with flags it does not know. |
| `rules` | list of strings | `[]` | Rule files, or directories of `.yar`/`.yara` files, walked recursively. Empty means `$XDG_DATA_HOME/dcs/yara`, `/usr/share/yara`, `/usr/local/share/yara` and `/etc/yara`. |
| `extra_args` | list of strings | `[]` | Extra arguments appended to the YARA invocation. |
| `severity` | string | `"high"` | Severity of a match when the rule's tags say nothing. A rule tagged `malware`, `ransomware`, `suspicious`, `pua`, `info` and the like overrides it. |
| `timeout_s` | int | `0` | Stop the pass after this many seconds. `0` means no limit; a rule pack with a pathological regex can otherwise run for a long time. |
| `threads` | int | `0` | Workers YARA starts. `0` lets YARA decide from the CPU count. |

A match becomes a finding that names the rule, with the tags and the
namespace in its detail line, so the report says *why* a file matched. YARA
reads every file it is pointed at, so `scan.max_file_size_mb` does not apply to
it; the engine's own `timeout_s` is the way to bound a pass.

### `[scan.intel]`

Known-hash lists, and optional reputation lookups. The shipped list in
`dcs/data/hashes.txt` is used by the heuristic engine as well, so this engine
adds your own lists on top and, when you opt in, asks a provider about the
hashes it does not know.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Run the `intel` engine when it is listed in `scan.engines`. |
| `hash_files` | list of strings | `[]` | Extra block lists, one `sha256 <name>` per line, `#` comments allowed. An exported MalwareBazaar dump or an internal list both fit. |
| `lookup` | string | `"off"` | `off` keeps everything on the machine; `hash` sends the SHA-256 of unknown files to the providers; `upload` also allows sending the files themselves (see `upload_unknowns`). |
| `virustotal_api_key` | string | `""` | VirusTotal API v3 key. Secret. |
| `virustotal_api_key_command` | string | `""` | Command that prints the key, e.g. `pass show dcs/virustotal`. |
| `metadefender_api_key` | string | `""` | MetaDefender Cloud key. Secret. |
| `metadefender_api_key_command` | string | `""` | Command that prints the key. |
| `max_hash_size_mb` | int | `256` | Files larger than this are not hashed for lookups. `0` means no limit. Hashing a 4 GiB image costs more than the lookup is worth. |
| `max_lookups` | int | `200` | Stop asking after this many lookups in one run. `0` means no limit. Keeps a free-tier quota from being spent by one 20 000 file stick. |
| `rate_per_minute` | int | `4` | Requests per minute against the providers. The free VirusTotal tier allows 4; a paid key can be raised a lot. |
| `cache_ttl_hours` | int | `168` | How long an answer is reused. Answers live in `$XDG_CACHE_HOME/dcs/intel/answers.json`, so re-scanning the same stick costs nothing. |
| `cache_dir` | string | `""` | Where the cache lives (empty = the XDG path above). |
| `upload_unknowns` | bool | `false` | The second switch that upload mode needs: with `lookup = "upload"`, files the providers do not know are sent for analysis. Every uploaded path is named in the result warnings. |
| `upload_max_size_mb` | int | `32` | Only files up to this size are uploaded (VirusTotal answers to 32 MiB directly). |
| `timeout_s` | int | `60` | Seconds to wait for one provider answer. |

A provider that refuses the key, runs out of quota or stops answering is
reported as a warning and dropped for the rest of the run: a scan whose
lookups failed is never presented as a clean opinion.

### `[archive]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Master switch for the archive stage. `--no-archive` disables it for one run, but only when the request does not explicitly ask for archiving. |
| `format` | string | `"zip"` | `"zip"` or `"7z"`. Anything else is a configuration problem. |
| `compression_level` | int | `6` | `0` stores without compressing, `1` is fastest, `9` smallest. Values outside 0 to 9 are clamped. |
| `split_size_mb` | int | `0` | Split the image into volumes of this size in MiB; `0` produces one file. zip volumes are `.z01`, `.z02`, ... plus the final `.zip`; 7z volumes are `.001`, `.002`, .... Splitting needs the external tool. |
| `size_warn_gb` | int | `20` | Warn (log, GUI and report) when the source is larger than this, because free hosts often refuse very large images. `0` disables the warning. |
| `when` | string | `"clean"` | When to build the image: `clean` (only when the scan found nothing), `always` (even for an infected medium) or `never`. `clean` is the safety default. |
| `use_device_label` | bool | `true` | Use the medium's label in the archive file name (`DCS_<label>_<timestamp>.zip`). When false the name uses `usb`. |
| `min_free_space_ratio` | float | `1.1` | Refuse to archive unless the target filesystem has at least this multiple of the source size free. The archive is never allowed to be written into a full filesystem. |

### `[upload]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Master switch for the upload stage. `--no-upload` disables it for one run. |
| `provider` | string | `"gofile"` | Adapter id: `gofile`, `pixeldrain`, `litterbox`, `catbox`, `tempsh`, `x0at`, `tmpfiles`. See `dcs providers`. An unknown id falls back to gofile with a warning. `--provider` and `DCS_PROVIDER` override it. |
| `upload_when` | string | `"clean"` | `clean` (only a clean image is uploaded), `always` or `never`. Anything but `clean` may publish malware; see the security note below. |
| `timeout_s` | int | `1800` | Read timeout for one upload attempt, in seconds; the connect timeout for an upload is fixed at 30 seconds. The small API calls around it (server discovery, folder creation) use a 15 second connect and a 60 second read timeout instead. |
| `retries` | int | `2` | Extra attempts per volume after a failure, with exponential backoff (2s, 4s, ... capped at 30s for gofile and 20s for the simple hosts). The default is therefore up to three attempts. |
| `delete_archive_after_upload` | bool | `false` | Delete the local archive once all volumes uploaded. `general.keep_archive = false` has the same effect; either one is enough. |

### `[upload.gofile]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `token` | string | `""` | A gofile guest or account token. Empty means DCS uploads anonymously and adopts the `guestToken` the host returns, reusing it for the remaining volumes of the same job. |
| `token_command` | string | `""` | Helper command whose standard output is the token, for example `"pass show dcs/gofile"`. Runs with a 15 second timeout. |
| `folder_prefix` | string | `"DCS"` | Present in the schema but not consumed: a split image is grouped into a folder named `<label> - <job id>` by the pipeline. |

The gofile adapter needs no token at all to upload. It caches the store host it
picked, falls back to a list of known store hosts when `api.gofile.io` is rate
limited, and treats the optional folder step as best effort, because the API host
limits aggressively while the storage hosts keep answering
(see [research/cloud-upload-apis.md](research/cloud-upload-apis.md)).

### `[upload.pixeldrain]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `api_key` | string | `""` | pixeldrain API key. pixeldrain no longer accepts anonymous uploads, so the provider is unusable without one. |
| `api_key_command` | string | `""` | Helper command whose standard output is the key. |
| `base_url` | string | `""` | Mirror or self hosted instance. Empty keeps the host compiled into the adapter; a value replaces scheme and host (a matching path prefix is collapsed, so `https://mirror.example/api` plus the adapter path `/api/file/{name}` gives `https://mirror.example/api/file/{name}`). |

### `[upload.x0at]`, `[upload.tempsh]`, `[upload.catbox]`, `[upload.tmpfiles]`, `[upload.litterbox]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `base_url` | string | `""` | Same meaning as above: a mirror of that host's API, or empty for the built-in URL. The section name matches the provider id exactly, which is what `dcs providers` and the GUI combo box list. |

### `[email]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch for the notification stage. Off until an SMTP server and a recipient are configured, because an installation with neither cannot send anything and the settings page would open with a warning. `--no-email` disables it for one run; `--to ADDRESS` on the command line sends anyway. |
| `smtp_host` | string | `""` | SMTP server. Required when `enabled` is true; `DCS_SMTP_HOST` overrides it. |
| `smtp_port` | int | `587` | Server port. `DCS_SMTP_PORT` overrides it. |
| `security` | string | `"starttls"` | `starttls` (upgrade a plain connection), `ssl` (implicit TLS, usually port 465) or `none`. |
| `username` | string | `""` | SMTP user. `DCS_SMTP_USERNAME` overrides it. Authentication happens only when both a username and a password are resolved. |
| `password` | string | `""` | SMTP password. Prefer `DCS_SMTP_PASSWORD` or `password_command`. |
| `password_command` | string | `""` | Helper command whose standard output is the password. |
| `from_addr` | string | `""` | Sender address. Required when `enabled` is true; `DCS_SMTP_FROM` overrides it. |
| `from_name` | string | `"Ducky Can Shoot"` | Display name of the sender. |
| `to_addrs` | list of strings | `[]` | Primary recipients. `DCS_SMTP_TO` (comma separated) and `--to` override it. At least one recipient (here, in `cc_addrs` or in `bcc_addrs`) is required. |
| `cc_addrs` | list of strings | `[]` | Carbon copy recipients. |
| `bcc_addrs` | list of strings | `[]` | Blind carbon copy recipients. They receive the message and are not listed in the headers. |
| `reply_to` | string | `""` | `Reply-To` header. |
| `subject_template` | string | `"[DCS] {verdict} - {device} ({files} files)"` | Subject template. Placeholders: `{verdict}`, `{device}`, `{files}`, `{threats}`, `{job}`, `{link}`. An unknown placeholder falls back to `[DCS] <verdict> - <device>` instead of failing. |
| `attach_report` | bool | `true` | Attach the report to the message. Which report kinds are attached follows `report.format`; the findings CSV is attached in every case. |
| `always_send` | bool | `true` | Send even when the medium is clean. Set to `false` to be mailed only when something was found; the stage is then skipped with the reason recorded. |
| `timeout_s` | int | `60` | Socket timeout for the SMTP conversation, in seconds. |
| `insecure_tls` | bool | `false` | Skip TLS certificate verification. Only for a trusted internal relay with a self-signed certificate; it removes the protection against a man in the middle on that hop. |
| `dry_run_dir` | string | `""` | Where `--dry-run-email` writes the `.eml` file. Empty means `~/.local/share/dcs/outbox`. |

### `[report]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Write reports at all. When false the report stage is skipped; note that the pipeline still tries to write a failure report at the end of a crashed run if no report exists yet and this switch is on. |
| `directory` | string | `""` | Where reports are written. Empty means `~/.local/share/dcs/reports`. `DCS_REPORT_DIR` overrides it. |
| `format` | string | `"html"` | `html`, `text` or `both`: which report kinds are attached to the email. All four files (HTML, text, findings CSV and JSON) are always written to disk. |
| `include_file_list` | bool | `false` | Include the list of every scanned file in the report. Off by default: on a large stick this makes the report enormous. The list is capped at 20 000 entries. |
| `max_findings` | int | `500` | Maximum number of findings rendered into the HTML and text reports. The CSV always contains every finding. |
| `write_json` | bool | `true` | Also write a JSON dump of the whole `JobResult` next to the reports, for automation. |

### `[gui]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `remember_window_size` | bool | `true` | Store the window size in the configuration when the application quits, and start with it next time. Sizes below 400x300 are ignored. |
| `window_width` | int | `1180` | Remembered window width. Ignored when `remember_window_size` is false, in which case the window opens at 1200x820. |
| `window_height` | int | `780` | Remembered window height. |
| `show_log_pane` | bool | `true` | Show the log pane in the live view when a job starts. |
| `auto_select_new_device` | bool | `true` | Select the first removable medium automatically when nothing is selected yet. |
| `switch_to_result` | bool | `true` | Jump to the result page automatically when a job finishes. |

There is no theme key: the interface deliberately follows the Plasma colour
scheme and icon theme, because Kirigami 6 exposes colour sets rather than a
light/dark override.

### `[watch]`

What happens when a medium is plugged in while Ducky Can Shoot is running, or
while you are logged in. Everything here is **off by default**: nothing is
watched until `mode` says otherwise.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | str | `"off"` | `off`, `notify`, `open` or `open_run` (see below). |
| `interval_s` | float | `3.0` | Seconds between two `lsblk` polls. Values below 0.5 are refused: the watcher reads the block device list, nothing more. |
| `ignore_existing` | bool | `true` | Ignore media that were already attached when the watcher started, so a stick plugged in yesterday does not trigger anything at login. |
| `notify_timeout_ms` | int | `20000` | How long the notification (and its **Open Ducky Can Shoot** button) stays on screen. |
| `autostart` | bool | `false` | Also write `~/.config/autostart/org.kde.dcs.watch.desktop`, so `dcs watch` starts with the desktop session. The settings page keeps this key and the file in sync. |

The four modes, weakest first:

| Mode | What happens when a medium appears |
| --- | --- |
| `off` | Nothing at all. No process watches, no notification, no window. |
| `notify` | A desktop notification with an **Open Ducky Can Shoot** button. Nothing is read from the medium until you press it (or press **Scan it now** on the banner in the window). |
| `open` | Ducky Can Shoot starts with the new medium already selected. The scan still waits for you. |
| `open_run` | Ducky Can Shoot starts and runs the whole job: scan, archive, upload, and email when `email.to_addrs` is not empty. Without a recipient it stops after the report and the link is shown on the result page. |

Two more rules matter in practice:

* **One event per physical disk.** A stick with two partitions is one thing you
  plugged in, so it produces one notification and at most one job; the partition
  with a mounted filesystem is the one that is used.
* **Exactly one reaction.** While a window is open it does the watching itself
  and the background watcher stays quiet (it checks the marker `dcs` writes into
  `$XDG_RUNTIME_DIR` when the interface starts). Close the window and the
  background watcher takes over again.

```bash
dcs config set watch.mode notify        # then plug a stick in
dcs config set watch.autostart true     # survive a logout
dcs watch --status                      # what is configured right now
```

## The complete default file

This is exactly what DCS writes the first time it saves a configuration, and it
is a valid starting point to copy into `~/.config/dcs/config.toml` (or
`/etc/dcs/config.toml`). Every value shown is the built-in default; remove the
lines you do not want to override.

```toml
[general]
confirm_before_upload = true
keep_archive = true
output_dir = ""
ui_refresh_ms = 100

[scan]
engines = ["clamav", "heuristic"]
max_file_size_mb = 4096
max_findings = 0
max_files = 0
follow_symlinks = false
include_hidden = true
hash_findings = true
exclude_globs = ["System Volume Information/*", "$RECYCLE.BIN/*", "*.tmp"]
blocking_severity = "high"

[scan.clamav]
enabled = true
prefer_daemon = true
clamdscan_binary = "clamdscan"
clamscan_binary = "clamscan"
extra_args = []
batch_size = 400
max_database_age_days = 7

[scan.yara]
enabled = false
binary = "yara"
rules = []
extra_args = []
severity = "high"
timeout_s = 0
threads = 0

[scan.intel]
enabled = true
hash_files = []
lookup = "off"
virustotal_api_key = ""
virustotal_api_key_command = ""
metadefender_api_key = ""
metadefender_api_key_command = ""
max_hash_size_mb = 256
max_lookups = 200
rate_per_minute = 4
cache_ttl_hours = 168
cache_dir = ""
upload_unknowns = false
upload_max_size_mb = 32
timeout_s = 60

[archive]
enabled = true
format = "zip"
compression_level = 6
split_size_mb = 0
size_warn_gb = 20
when = "clean"
use_device_label = true
min_free_space_ratio = 1.1

[upload]
enabled = true
provider = "gofile"
upload_when = "clean"
timeout_s = 1800
retries = 2
delete_archive_after_upload = false

[upload.gofile]
token = ""
token_command = ""
folder_prefix = "DCS"

[upload.pixeldrain]
api_key = ""
api_key_command = ""
base_url = ""

[upload.x0at]
base_url = ""

[upload.tempsh]
base_url = ""

[upload.catbox]
base_url = ""

[upload.tmpfiles]
base_url = ""

[upload.litterbox]
base_url = ""

[email]
enabled = false
smtp_host = ""
smtp_port = 587
security = "starttls"
username = ""
password = ""
password_command = ""
from_addr = ""
from_name = "Ducky Can Shoot"
to_addrs = []
cc_addrs = []
bcc_addrs = []
reply_to = ""
subject_template = "[DCS] {verdict} - {device} ({files} files)"
attach_report = true
always_send = true
timeout_s = 60
insecure_tls = false
dry_run_dir = ""

[report]
enabled = true
directory = ""
format = "html"
include_file_list = false
max_findings = 500
write_json = true

[gui]
remember_window_size = true
window_width = 1180
window_height = 780
show_log_pane = true
auto_select_new_device = true
switch_to_result = true

[watch]
mode = "off"
interval_s = 3.0
ignore_existing = true
notify_timeout_ms = 20000
autostart = false
```

A practical starting point for a workstation that should upload the image and mail
the link. Email is off in a fresh configuration (a server and a recipient have to
be configured first), so it is switched on here:

```toml
[email]
enabled = true
smtp_host = "smtp.example.org"
smtp_port = 587
security = "starttls"
username = "dcs@example.org"
password_command = "pass show mail/dcs"   # or export DCS_SMTP_PASSWORD
from_addr = "dcs@example.org"
to_addrs = ["analyst@example.org"]

[upload]
provider = "gofile"
upload_when = "clean"

[archive]
format = "zip"
split_size_mb = 1900                      # keep each volume under 2 GB
```

## Environment variables

| Variable | Affects | Notes |
| --- | --- | --- |
| `DCS_SMTP_PASSWORD` | `email.password` | Secret. Highest priority for the mail password. |
| `DCS_SMTP_USERNAME` | `email.username` | Overrides the stored user name. |
| `DCS_GOFILE_TOKEN` | `upload.gofile.token` | Secret. Highest priority for the gofile token. |
| `DCS_PIXELDRAIN_API_KEY` | `upload.pixeldrain.api_key` | Secret. Highest priority for the pixeldrain key. |
| `DCS_PROVIDER` | `upload.provider` | Provider id, same values as `--provider`. |
| `DCS_UPLOAD_WHEN` | `upload.upload_when` | `clean`, `always` or `never`. |
| `DCS_SMTP_HOST` | `email.smtp_host` | |
| `DCS_SMTP_PORT` | `email.smtp_port` | Non-numeric values are ignored with a warning. |
| `DCS_SMTP_FROM` | `email.from_addr` | |
| `DCS_SMTP_TO` | `email.to_addrs` | Comma separated; replaces the list. |
| `DCS_ENGINES` | `scan.engines` | Comma separated; replaces the list. |
| `DCS_LOOKUP` | `scan.intel.lookup` | `off`, `hash` or `upload`. |
| `DCS_VIRUSTOTAL_API_KEY` | `scan.intel.virustotal_api_key` | Secret. Highest priority for the VirusTotal key. |
| `DCS_METADEFENDER_API_KEY` | `scan.intel.metadefender_api_key` | Secret. Highest priority for the MetaDefender key. |
| `DCS_ARCHIVE_DIR` | `general.output_dir` | Where archives are written. |
| `DCS_REPORT_DIR` | `report.directory` | Where reports are written. |
| `DCS_LOG_LEVEL` | logging | Default for `--log-level` and for the GUI. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `NO_COLOR` | CLI output | Any value disables the ANSI colours, which are also disabled when stdout is not a terminal. |

The environment is applied after the configuration files, so exporting
`DCS_PROVIDER` overrides whatever the file says.

## Secret resolution order

Five values are treated as secrets: the SMTP password, the gofile token, the
pixeldrain API key and the VirusTotal and MetaDefender keys. Each is resolved the
same way, first hit wins:

1. **the environment variable** (`DCS_SMTP_PASSWORD`, `DCS_GOFILE_TOKEN`,
   `DCS_PIXELDRAIN_API_KEY`, `DCS_VIRUSTOTAL_API_KEY`, `DCS_METADEFENDER_API_KEY`);
2. **the helper command** from the configuration (`email.password_command`,
   `upload.gofile.token_command`, `upload.pixeldrain.api_key_command`,
   `scan.intel.virustotal_api_key_command`, `scan.intel.metadefender_api_key_command`),
   run with a 15 second timeout; the trimmed standard output is used, and a
   non-zero exit code is logged and ignored;
3. **the literal value in the file** (`email.password`, `upload.gofile.token`,
   `upload.pixeldrain.api_key`, `scan.intel.virustotal_api_key`,
   `scan.intel.metadefender_api_key`).

The SMTP user name is resolved separately: `DCS_SMTP_USERNAME` first, then
`email.username`.

Two consequences worth knowing:

- An **empty** environment variable is treated as "not set" (the check is on
  truthiness), so `DCS_SMTP_PASSWORD=""` does not mask the file value; it simply
  does nothing.
- Authentication happens only when **both** a user name and a password resolve.
  With a user name but no password, DCS logs "an SMTP username is set but no
  password was resolved; skipping authentication" and sends unauthenticated,
  which most relays reject. That log line is the fastest way to diagnose a
  password that never arrived.

## Why the config file should never contain a password

DCS deliberately lets secrets live outside the file:

- A configuration file ends up in a dotfiles repository, a backup, a container
  image or a screenshot of `dcs config show`. A password stored there leaks with
  every copy of that artefact. DCS masks secrets as `***` in `dcs config show`,
  in the GUI settings page and in the configuration snapshot stored inside the
  JSON result, but that only protects the places DCS controls.
- The environment and a helper command can be fed by a real secret manager
  (`pass`, `gpg`, `systemd-creds`, a keyring) that is unlocked per session, so
  the secret is never at rest in a readable file.
- Rotating a secret means re-exporting it, not editing a file on every machine.

If you must store one, remember that DCS writes the file with mode `0600` and
never logs secret values - but it cannot protect a file you copied elsewhere.

A good pattern is a system-wide `/etc/dcs/config.toml` with everything except the
secrets, and per-user environment variables (or a `password_command` pointing at
the user's own keyring) for the rest.

## Validation

DCS checks the configuration before it is used, both in `dcs doctor` and in the
GUI:

- `scan.engines` must contain only `clamav`, `heuristic`, `yara` and `intel`, and
  must not be empty.
- `scan.yara.severity` must be one of `info`, `low`, `medium`, `high`, `critical`.
- `scan.intel.lookup` must be `off`, `hash` or `upload`;
  `scan.intel.upload_unknowns` requires `lookup = "upload"`, and
  `scan.intel.rate_per_minute` must be at least 1.
- `archive.format` must be `zip` or `7z`.
- `archive.when` must be `clean`, `always` or `never`.
- `upload.upload_when` must be `clean`, `always` or `never`.
- `report.format` must be `html`, `text` or `both`.
- With `email.enabled = true`: `smtp_host`, `from_addr` and at least one recipient
  must be set, and `email.security` must be one of `starttls`, `ssl`, `none`.

`dcs doctor` prints the resulting list and fails (exit code 1) while anything is
unresolved. The GUI shows the same list as an inline message on the settings
page, where it also disables **Save Settings** until the draft is valid again.
A fresh configuration passes all of it: email starts out disabled, so the
settings page of a new installation shows no warning at all.
None of these are fatal on their own: a job still runs, minus the stage that could
not be configured.
