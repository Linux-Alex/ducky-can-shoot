# Troubleshooting

Symptoms, causes and fixes for the situations DCS runs into. If your case is missing,
`dcs doctor` prints the state of every dependency, and the log pane in the interface
shows what the pipeline is doing.

[Back to the README](../README.md)

### The USB watcher does not do anything

The watcher is off by default, and three things have to line up:

```bash
dcs watch --status
```

```text
mode            : off          <- nothing is watched until this is notify/open/open_run
watch interval  : 3s
ignore existing : True
autostart file  : ~/.config/autostart/org.kde.dcs.watch.desktop (installed)
autostart runs  : /usr/bin/dcs watch
interface       : not running
would do        : nothing; the watcher exits immediately
```

- **`mode` is `off`** - turn it on with `dcs config set watch.mode notify` (or in
  **Settings -> USB detection**).
- **Nothing reacts while the interface is open** - that is on purpose: the window
  watches for itself, so a medium cannot produce two notifications or two jobs.
  The line `interface : running (pid N)` says so. Close the window and plug the
  medium in again.
- **It only reacts to media that appear *while it runs*** - a stick that was
  already attached when the watcher started is ignored
  (`watch.ignore_existing`). Unplug it, plug it back in.
- **No notification appears** - `notify-send` comes from `libnotify-bin`:
  `sudo apt install libnotify-bin`. Without it the medium is printed on standard
  output of `dcs watch` instead, and the banner still appears in an open window.
- **Nothing happens at login** - the autostart entry is a file, check that it
  exists and that its `Exec=` starts a real launcher:
  `cat ~/.config/autostart/org.kde.dcs.watch.desktop`. `dcs config set
  watch.autostart true` writes it again.

### The settings page says "Unsaved changes" but I saved

The bar at the bottom of the settings page compares the whole draft with the file
on disk. If it stays on "Unsaved changes", the save failed and the message next
to it says why (`Could not save: ...`, usually permissions on
`~/.config/dcs/config.toml`). `Revert` throws the draft away and reloads what is
stored on disk.

**Test SMTP** always tests the values that are in the form, saved or not, so a
server can be checked before anything is written to the configuration file.

### The interface does not start: a QML module is missing

```text
QQmlApplicationEngine failed to load component
module "org.kde.kirigami" is not installed
```

The interface is Qt Quick with Kirigami, and Kirigami 6 has to come from your
distribution, from the same Qt build as the PyQt6 packages:

```bash
sudo apt install python3-pyqt6 python3-pyqt6.qtqml python3-pyqt6.qtquick \
  qml6-module-org-kde-kirigami qml6-module-qtquick-controls \
  qml6-module-qtquick-layouts qml6-module-qtquick-dialogs \
  qml6-module-qtquick-templates qml6-module-qtqml-workerscript
```

On Ubuntu that means 26.04 or newer. Ubuntu 24.04 carries no KDE Frameworks 6
packages, so no amount of Qt 6 packages will make the interface start there; use
`dcs` from the command line on such a system.

If the window opens but **the logo and every icon are empty squares**, the Qt SVG
*plugins* are missing. They are a separate package from the SVG library:

```bash
sudo apt install qt6-svg-plugins     # Debian / Ubuntu
sudo dnf install qt6-qtsvg           # Fedora
sudo pacman -S qt6-svg               # Arch
```

Qt draws themed icons, the application artwork and any SVG in the interface with
those plugins, so nothing that ships as a vector is visible without them.

`dcs doctor` reports which part is missing.

### ClamAV is missing

```text
[FAIL] engine clamav    ClamAV is not installed (install the clamav package, then run freshclam)
```

DCS can install it for you. In the interface, the message on the **New job** tab
and the engine list in **Settings** both offer an **Install ClamAV...** button:
it detects your distribution and package manager, shows every command it is
about to run, and then hands the job to pkexec (a graphical password dialog) or
to a terminal with sudo inside. DCS never sees your password, and the plan can
be copied instead of executed.

![The Install ClamAV dialog: detected distribution, package manager, privilege method, the exact commands and the streamed installation output](images/install-clamav.png)

From a terminal the same plan is available as:

```bash
dcs install-clamav --dry-run    # print the plan, the script and the commands
dcs install-clamav              # ask for confirmation, then run it through sudo
```

The installer knows APT (Debian, Ubuntu, Mint), DNF (Fedora, RHEL, Rocky),
Zypper (openSUSE), Pacman (Arch), APK (Alpine), XBPS (Void), eopkg (Solus),
Portage (Gentoo) and Homebrew. It refreshes the package index, installs the
scanner and the daemon, fetches the signatures (around 300 MB the first time)
and enables the service. Prefer to do it by hand? These are exactly the commands
the dialog shows before it runs anything:

```bash
sudo apt install clamav clamav-daemon clamav-freshclam && sudo freshclam   # Debian / Ubuntu
sudo dnf install clamav clamd clamav-update && sudo freshclam              # Fedora / RHEL
sudo pacman -S clamav && sudo freshclam                                    # Arch
dcs engines
```

Without ClamAV the scan runs with the built-in heuristics only. DCS says so: the
job records a warning, the report prints a limited-coverage note, and archive
contents inside RAR, 7z and CAB files are not inspected at all.
`dcs scan --engines heuristic` is a legal way to state "I accept that".

### clamd is not running

```text
warning: clamd is not running: the stand-alone clamscan reloads its database for every batch
```

DCS falls back to `clamscan` automatically, which is several times slower on a
stick with many small files. Start the daemon:

```bash
sudo systemctl enable --now clamav-daemon
clamdscan --ping 3
dcs engines
```

If the daemon runs but DCS still reports clamd as unreachable, check the socket
permissions and make sure the `clamdscan` in your PATH is the front end for that
daemon. Setting `scan.clamav.prefer_daemon = false` forces the stand-alone
scanner.

### No USB device is detected

`dcs devices` prints "No removable medium detected", or the list in the GUI stays
empty.

- Confirm the stick is visible to the kernel:
  `lsblk -o NAME,SIZE,FSTYPE,LABEL,TRAN,RM`.
- A device is listed when it is flagged removable, or hotplug capable, or attached
  over usb, mmc, sdio or ieee1394, **and** carries a filesystem DCS knows (vfat,
  exfat, ntfs, ext2 to ext4, btrfs, xfs, f2fs, iso9660, udf, hfsplus, apfs). A
  stick without a partition table, or with an unformatted partition, will not
  appear.
- Some USB enclosures do not set the removable bit; DCS accepts anything on a usb
  transport for that reason. If your device sits on another bus, triage the mount
  point directly with `dcs scan PATH` or `dcs run --path DIR`.
- Internal disks are never listed. That is intentional: this tool is for media a
  human plugged in.

### udisksctl is missing

```text
could not mount /dev/sdb1: udisksctl is not installed (package udisks2)
```

Install udisks2:

```bash
sudo apt install udisks2        # Debian / Ubuntu
sudo dnf install udisks2        # Fedora
```

Without it, mount the stick yourself (your desktop's file manager, or `mount` as
root) and pass the mount point with `--path`. DCS needs write permission nowhere on
the medium: it only reads.

### SMTP authentication failures

```text
[FAIL] smtp     SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted')
```

- Check host, port and security mode. Port 587 is normally `starttls`, port 465 is
  `ssl`, and `none` means the connection is unencrypted.
- The password is resolved from `DCS_SMTP_PASSWORD`, then
  `email.password_command`, then `email.password`. If no password is resolved but
  a username is set, DCS logs "an SMTP username is set but no password was
  resolved; skipping authentication" instead of failing loudly. If you see that
  line, the password never arrived.
- Gmail and most large providers require an app password rather than the account
  password, and may reject a `From` address that differs from the authenticated
  account. Point `email.from_addr` at the authenticated address.
- For an internal relay with a self-signed certificate set
  `email.insecure_tls = true`. That disables certificate verification; use it only
  on a network you trust.
- Test before a real run: `dcs doctor --smtp`.

### gofile is rate limiting or blocking

gofile rate limits aggressively and per IP address: roughly ten calls in five
minutes to the API host get HTTP 429, and repeated excess can get the originating
address blocked. The important operational detail is that **uploads do not need the
API host, only server discovery does** (see
[docs/research/cloud-upload-apis.md](research/cloud-upload-apis.md)). DCS
therefore caches the store host it picked, falls back to a list of known store
hosts, treats folder creation as optional, and retries with exponential backoff,
re-picking a host on every attempt.

If it still fails:

```bash
dcs providers                              # the alternatives and their limits
dcs run --path /mnt/stick --provider litterbox
```

Or change `upload.provider` in the config, or export `DCS_PROVIDER=litterbox`.
Behind a shared NAT or a VPN the rate limit is shared with everybody else on that
address, so a different network may work immediately.

### The archive does not fit or there is not enough free space

```text
not enough free space in /home/user/.local/share/dcs: the image needs about 24.0 GiB
but only 4.2 GiB is free
```

The archive is written to `general.output_dir`, which defaults to
`~/.local/share/dcs` and is often on a small root filesystem. DCS refuses to start
when less than `archive.min_free_space_ratio` (default 1.1) times the source size
is free, because a compressed image can turn out larger than expected and a
half-written archive is worse than no archive.

- Put the output on a bigger filesystem: `--output-dir /mnt/big/dcs` or
  `general.output_dir` in the config.
- Split the image so that each volume fits where it is going:
  `archive.split_size_mb = 1900`. Splitting needs the external `zip` or `7z` tool,
  not the Python fallback.
- Skip the archive entirely with `--no-archive` if you only want the scan and the
  verdict.
- `archive.size_warn_gb` (default 20) produces a warning above that size. It is
  advice, not a limit.

### The image is too big for the chosen host

Every host has a published limit: 200 MiB on catbox, 100 MiB on tmpfiles, 1 GiB on
litterbox and x0.at, 4 GiB on temp.sh, 20 GiB on pixeldrain with a key, and no
published limit on gofile. DCS checks the size before uploading and reports it
instead of failing in the middle of a transfer. Use `dcs providers` to see the
limits, or split the archive.

---

