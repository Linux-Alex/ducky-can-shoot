# Command line reference

Every subcommand, flag and exit code of the `dcs` command. The graphical interface runs the same code,
so everything documented here has an equivalent in the window.

[Back to the README](../README.md)

Global options, valid for every subcommand:

| Option | Default | Meaning |
| --- | --- | --- |
| `--config PATH` | the layered default files | Configuration file to use; it also becomes the file a save would target. |
| `--log-level LEVEL` | `DCS_LOG_LEVEL`, else `WARNING` | Verbosity of the application log on stderr; one of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `--version` | | Print `Ducky Can Shoot <version>` and exit. |
| `-h`, `--help` | | Help. |

Running `dcs` with no subcommand is the same as `dcs gui`.

### `dcs gui [--device NODE] [--autorun]`

| Option | Meaning |
| --- | --- |
| `--device NODE` | Open the interface on this medium (`/dev/sdb1`, a label or a mount point) and mount it if needed. |
| `--autorun` | Start the job on that medium as soon as the window is up. Used by `dcs watch` in `open_run` mode. |

Starts the graphical interface. If PyQt6 is missing, the command explains what to
install and exits with code 2. Accepts the global options only.

### `dcs devices [--json]`

Lists removable media found through lsblk: node, label, size, filesystem, bus and
mount point. `--json` prints the full device dictionaries.

```bash
dcs devices
dcs devices --json | jq '.[] | select(.mounted == false)'
```

Always exits 0, even when nothing is plugged in.

### `dcs scan PATH [--engines IDS] [--yara-rules PATH] [--lookup MODE] [--max-file-size-mb N] [--json] [--quiet]`

Scans a directory and prints what it found. This is the scan stage on its own: no
archive, no upload, no email and no report.

| Option | Meaning |
| --- | --- |
| `PATH` | Mount point or directory to scan (required). |
| `--engines IDS` | Comma separated engine ids: `clamav`, `heuristic`, `yara`, `intel`. |
| `--yara-rules PATH` | YARA rule file or directory; repeatable, and it switches the `yara` engine on. |
| `--lookup MODE` | Reputation lookups for unknown files: `off` (default), `hash` (SHA-256 only) or `upload`. |
| `--upload-unknowns` | Also send unknown files to the provider. Prints a notice, because it publishes them to that service. |
| `--max-file-size-mb N` | Skip files larger than N MiB (0 means no limit). |
| `--json` | Print the complete `ScanResult` as JSON instead of the summary. |
| `--quiet` | Suppress progress output. |

```bash
dcs scan /media/$USER/USBSTICK
dcs scan /media/$USER/USBSTICK --engines heuristic --max-file-size-mb 512
dcs scan /media/$USER/USBSTICK --engines heuristic,yara --yara-rules ~/yara-rules
dcs scan /media/$USER/USBSTICK --engines heuristic,intel --lookup hash
dcs scan /media/$USER/USBSTICK --json > scan.json
```

Exit code: 0 clean, 1 suspicious or infected, 2 error or cancelled.

### `dcs run [options]`

Runs the complete job. Either `--device` or `--path` must be given.

| Option | Default | Meaning |
| --- | --- | --- |
| `--device NODE` | | Block device such as `/dev/sdb1`; mounted through udisks2 first if needed. |
| `--path DIR` | | Directory to triage instead of a device. |
| `--label NAME` | device label, else directory name | Name used in reports, archive file names and the email. |
| `--to ADDR` | `email.to_addrs` | Recipient; repeatable, and comma or semicolon separated inside one value. |
| `--provider ID` | `upload.provider` | Upload provider id, see `dcs providers`. |
| `--engines IDS` | `scan.engines` | Comma separated engine ids: `clamav`, `heuristic`, `yara`, `intel`. |
| `--yara-rules PATH` | | YARA rule file or directory; repeatable, and it switches the `yara` engine on. |
| `--lookup MODE` | `scan.intel.lookup` | `off`, `hash` or `upload` reputation lookups. |
| `--upload-unknowns` | off | Send unknown files to the provider for analysis. |
| `--output-dir DIR` | `general.output_dir` | Where the archive is written. |
| `--no-archive` | off | Skip the archive stage. |
| `--no-upload` | off | Never upload anything. |
| `--no-email` | off | Do not send the email. |
| `--dry-run-email` | off | Write the message to disk as `.eml` instead of sending it. |
| `--yes` | off | Do not ask anything; this is also what allows publishing a non-clean image. |
| `--json` | off | Print the full `JobResult` as JSON. |
| `--quiet` | off | Suppress progress output. |

```bash
dcs run --device /dev/sdb1 --to analyst@example.org
dcs run --path /mnt/stick --label "Field stick 7" --provider litterbox --yes
dcs run --path /mnt/stick --no-upload --no-email --json | jq .verdict
```

Exit code: 0 clean, 1 suspicious or infected, 2 error or cancelled.

### `dcs update [--check] [--yes] [--json]`

Asks GitHub for the newest published release and compares it with the copy
that is running. `--check` (or no flag) only reports; `--yes` downloads the
package and installs it.

| Option | Default | Meaning |
| --- | --- | --- |
| `--check` | off | Report only. Exit code 1 means a newer release exists. |
| `--yes` | off | Download the release package and install it. A `.deb` needs your password: sudo asks for it in this terminal. |
| `--json` | off | Print the check as JSON instead of a sentence. |

```bash
dcs update --check                  # is there a newer release?
dcs update --check --json | jq .   # the same question for a script
dcs update --yes                    # download, install, restart the watcher
```

Exit codes: 0 up to date (or installed), 1 a newer release exists, 2 the check
or the installation failed. The interface does the same thing from the
settings page, where it can also restart itself afterwards.
### `dcs doctor [--json] [--offline] [--smtp]`

Checks the Python version, the configuration for values that would break a run,
every configured scan engine, the external tools (`lsblk`, `udisksctl`, `zip`,
`7z`) and the writability of the output and report directories. With network
access it also probes the configured upload provider; `--offline` skips that, and
`--smtp` adds a connection test against the mail server.

```bash
dcs doctor
dcs doctor --offline --json
```

Exit code: 0 when every check passed, **1 when at least one check failed**, 2 for
an unexpected error. Note that this differs from `scan` and `run`, where 1 means
"the medium is not clean".

### `dcs install-clamav [--dry-run] [--terminal] [--yes] [--json]`

Installs the optional ClamAV engine through the package manager of the machine
it runs on. It prints the plan (distribution, package manager, the exact
commands, the generated script) and asks for confirmation before anything runs;
sudo collects the password in this terminal, so DCS never handles it.

| Option | Meaning |
| --- | --- |
| `--dry-run` | Print the plan, the commands and the generated script; execute nothing. |
| `--json` | With `--dry-run`: the same plan as JSON, for scripts. |
| `--terminal` | Open a terminal emulator instead of using the graphical pkexec dialog. |
| `--yes` | Do not ask for confirmation. |

Exit code 0 when the installation succeeded, 2 when it failed or was cancelled.

### `dcs config [init|show|path|set] [--show-secrets] [--options]`

| Action | Meaning |
| --- | --- |
| `show` (the default) | Print the effective configuration as TOML, with secrets masked. |
| `init` | Write a default configuration file if none exists yet. |
| `path` | Print the configuration file that is in use. |
| `set KEY VALUE` | Change one dotted option and save the file. |

`--show-secrets` prints stored values unmasked; do not pipe that into a log.
`--options` prints every settable option name, one per line.

`set` coerces the value the same way the loader does (`"false"` becomes `false`,
`"5"` becomes `5`, a comma separated list becomes a list) and **refuses** a value
the configuration itself calls broken, leaving the old value in place:

```bash
dcs config path
dcs config show
dcs config --options
dcs config set watch.mode notify
dcs config set email.to_addrs "me@example.org, ops@example.org"
dcs config set archive.format rar        # error: must be zip or 7z (exit 2)
```

Exit code: 0 normally, 2 for an unknown action, an unknown option or a value that
was refused.

### `dcs watch [--mode MODE] [--interval SECONDS] [--once] [--status] [--json]`

Listens for media that are plugged in *while this command runs* and does what
`watch.mode` says (see [the `[watch]` section](CONFIGURATION.md#watch)). This is
the command the autostart entry runs; it needs no root, no udev rule and no D-Bus
connection, only `lsblk`.

| Option | Meaning |
| --- | --- |
| `--mode off\|notify\|open\|open_run` | Override the configured mode for this run only. |
| `--interval SECONDS` | Override `watch.interval_s`. |
| `--once` | Print the media attached right now and exit (a snapshot, not a listener). |
| `--start` | Start a watcher in the background (unless one is already running) and exit. This is what the settings page and the installer call. |
| `--stop` | Stop the background watcher and exit. |
| `--test` | Send one test notification and report whether its button came back. Use this when a notification arrives without a button. |
| `--status` | Print the mode, the autostart file, the interval, and whether an interface, a watcher and the log file are there. |
| `--json` | One JSON object per medium, for scripts. |

Modes:

| Mode | What the watcher does with a new medium |
| --- | --- |
| `off` | Exits immediately with a note about how to turn it on (so the autostart entry is harmless). |
| `notify` | Shows a notification with an **Open Ducky Can Shoot** button (`notify-send --wait`); pressing it starts `dcs gui --device NODE`. |
| `open` | Starts `dcs gui --device NODE`. |
| `open_run` | Starts `dcs gui --device NODE --autorun`, which mounts the medium and runs the whole job. |

While a window is open, `dcs watch` stays quiet: the interface watches for itself,
so a medium never produces two notifications or two jobs. Closing the window hands
the watching back to the background process, which the interface starts if it is
not running yet.

`--start` is idempotent: a pid file in `$XDG_RUNTIME_DIR/ducky-can-shoot/watch.pid`
makes a second watcher stand down, so the autostart entry and the settings page can
both ask for one. A detached watcher writes what it does to
`$XDG_STATE_HOME/dcs/watch.log` (`~/.local/state/dcs/watch.log`), which
`--status` prints.

```bash
dcs watch --status                            # mode, autostart file, watcher, log
dcs watch --start                             # make sure something is listening now
dcs watch --test                              # does the notification button work?
dcs watch                                     # listen in the foreground
dcs watch --mode notify --interval 1          # try it out without saving anything
dcs watch --once --json                       # what is attached right now
```

The notification is sent over D-Bus (`org.freedesktop.Notifications`) rather than
through `notify-send`, because `notify-send` drops the action buttons on systems
where it believes the server cannot show them - Plasma 6.6 says exactly that - and
a notification without its button is useless for "plug a medium in, click Open".
`notify-send` is still the fallback on a machine without `gdbus`.

Exit code: 0 normally, 2 for an unknown mode.

### `dcs engines [--json]`

Shows each configured scan engine, whether it is available, its version and, when
it is not usable, why (ClamAV missing, clamd not answering, no signature database
found). Always exits 0.

### `dcs providers [--json]`

Lists the upload providers with their label, whether an API key is required, their
size limit and a note about retention. Always exits 0.

### `dcs version [--json]`

Prints the application name, the version and the license. Always exits 0.

### Exit-code contract

| Code | Meaning |
| --- | --- |
| `0` | Clean: the run completed and the verdict is `clean`. Also used by the informational commands (`devices`, `engines`, `providers`, `version`, `config`) and by a `doctor` run in which every check passed. |
| `1` | The medium is not clean: verdict `suspicious` or `infected`. For `doctor`: at least one check failed. |
| `2` | The run failed, was cancelled or could not be evaluated (`error`, `cancelled`), a required argument was missing, or an unexpected exception was caught. |

The mapping lives in one function, `exit_code_for`, and is part of the documented
interface: scripts may rely on it.

---

