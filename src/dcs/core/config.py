"""Configuration: dataclasses, TOML load/save and secret resolution.

Layering (later wins)
---------------------
1. built-in defaults
2. /etc/dcs/config.toml            (system wide)
3. ~/.config/dcs/config.toml       (per user; written by the GUI)
4. the file passed with --config
5. environment variables           (DCS_* and documented per-secret vars)

Secrets are never required in the file: every secret has an environment
variable and an optional shell command fallback, so the config can live in a
dotfiles repository without leaking passwords.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin

from dcs.core.log import get_logger

log = get_logger("config")

SYSTEM_CONFIG = "/etc/dcs/config.toml"

#: every engine id `scan.engines` accepts
ENGINE_IDS = ("clamav", "heuristic", "yara", "intel")

#: how far the reputation lookups may go
LOOKUP_MODES = ("off", "hash", "upload")

#: severities, in the order the events module ranks them
SEVERITY_NAMES = ("info", "low", "medium", "high", "critical")


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def xdg_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def default_config_path() -> Path:
    return xdg_config_home() / "dcs" / "config.toml"


def default_data_dir() -> Path:
    return xdg_data_home() / "dcs"


def default_reports_dir() -> Path:
    return default_data_dir() / "reports"


def default_work_dir() -> Path:
    return default_data_dir() / "work"


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


@dataclass
class GeneralConfig:
    #: ask before starting a job that will upload data
    confirm_before_upload: bool = True
    #: keep the local archive after a successful upload
    keep_archive: bool = True
    #: unmount the medium when the job finishes, so it can be unplugged safely
    unmount_after_job: bool = True
    #: directory for archives, reports and work files ("" = XDG default)
    output_dir: str = ""
    #: UI refresh interval for the live view, milliseconds
    ui_refresh_ms: int = 100


@dataclass
class ClamAVConfig:
    enabled: bool = True
    #: prefer clamdscan (daemon, much faster) over clamscan
    prefer_daemon: bool = True
    clamdscan_binary: str = "clamdscan"
    clamscan_binary: str = "clamscan"
    #: extra command line arguments appended to every invocation
    extra_args: list[str] = field(default_factory=list)
    #: files handed to one engine invocation
    batch_size: int = 400
    #: warn when the signature database is older than this many days
    max_database_age_days: int = 7


@dataclass
class YaraConfig:
    """YARA rules: the second signature engine, and the one whose rules are yours.

    It is off by default because it needs rules to be worth anything: a YARA
    engine with an empty rule set reports "no threats" without having looked at
    a single pattern, which is worse than being switched off.
    """

    enabled: bool = False
    #: the binary to run (the classic `yara` command line is what DCS drives)
    binary: str = "yara"
    #: rule files, or directories of .yar/.yara files ("" = the usual places)
    rules: list[str] = field(default_factory=list)
    #: extra command line arguments appended to every invocation
    extra_args: list[str] = field(default_factory=list)
    #: a match is at least this severe unless the rule's tags say otherwise
    severity: str = "high"
    #: stop a pass after this many seconds (0 = no limit)
    timeout_s: int = 0
    #: scanning threads (0 = let YARA decide from the CPU count)
    threads: int = 0


@dataclass
class IntelConfig:
    """Known hashes: local block lists, plus optional reputation lookups.

    `lookup = "off"` keeps everything on the machine.  The two online modes are
    the only places where anything about the medium leaves it, and the upload
    mode is the only one that sends file *contents*: it therefore has to be
    asked for twice (`lookup = "upload"` and `upload_unknowns = true`) and
    every uploaded file is named in the report.
    """

    #: the extra hash files below are read whenever this is on
    enabled: bool = True
    #: "sha256 <name>" per line, e.g. an exported MalwareBazaar or internal list
    hash_files: list[str] = field(default_factory=list)
    #: off | hash | upload
    lookup: str = "off"
    virustotal_api_key: str = ""
    virustotal_api_key_command: str = ""
    metadefender_api_key: str = ""
    metadefender_api_key_command: str = ""
    #: files larger than this are not hashed for lookups (MiB, 0 = no limit)
    max_hash_size_mb: int = 256
    #: stop after this many lookups in one run (0 = no limit)
    max_lookups: int = 200
    #: requests per minute against the providers; the free tiers are strict
    rate_per_minute: int = 4
    #: how long a cached answer is trusted, in hours
    cache_ttl_hours: int = 168
    #: where answers are cached ("" = $XDG_CACHE_HOME/dcs/intel)
    cache_dir: str = ""
    #: send unknown files to the providers (needs lookup = "upload")
    upload_unknowns: bool = False
    #: only files up to this size are uploaded (MiB)
    upload_max_size_mb: int = 32
    #: seconds to wait for one provider answer
    timeout_s: int = 60


@dataclass
class ScanConfig:
    #: engine ids: clamav, heuristic, yara, intel
    engines: list[str] = field(default_factory=lambda: ["clamav", "heuristic"])
    #: skip scanning individual files larger than this (MiB); 0 = no limit
    max_file_size_mb: int = 4096
    #: stop after this many findings (0 = keep going)
    max_findings: int = 0
    #: stop after this many files (0 = all); handy for a quick look
    max_files: int = 0
    follow_symlinks: bool = False
    include_hidden: bool = True
    #: compute sha256 for every finding (slower, better reports)
    hash_findings: bool = True
    #: glob patterns skipped during the walk
    exclude_globs: list[str] = field(
        default_factory=lambda: ["System Volume Information/*", "$RECYCLE.BIN/*", "*.tmp"]
    )
    #: threat names from ClamAV that should never mark the drive infected
    #: (PUA/adware are reported but do not block the upload by default)
    blocking_severity: str = "high"
    clamav: ClamAVConfig = field(default_factory=ClamAVConfig)
    yara: YaraConfig = field(default_factory=YaraConfig)
    intel: IntelConfig = field(default_factory=IntelConfig)


@dataclass
class ArchiveConfig:
    enabled: bool = True
    #: zip (default) or 7z
    format: str = "zip"
    #: 0 = store, 1 = fastest ... 9 = smallest
    compression_level: int = 6
    #: split into volumes of this size in MiB (0 = single file)
    split_size_mb: int = 0
    #: warn in the GUI/CLI above this size in GiB (0 = never warn)
    size_warn_gb: int = 20
    #: when to build the image: clean | always | never
    when: str = "clean"
    #: prepend the device label to the archive name
    use_device_label: bool = True
    #: refuse to archive when the target filesystem has less free space than
    #: this fraction of the estimated archive size
    min_free_space_ratio: float = 1.1


@dataclass
class GofileConfig:
    #: guest token; empty means "create a guest account on the fly"
    token: str = ""
    token_command: str = ""
    #: root folder name created per job ("" = upload to the root)
    folder_prefix: str = "DCS"


@dataclass
class PixeldrainConfig:
    api_key: str = ""
    api_key_command: str = ""
    #: '' keeps the host compiled into the adapter; set it to use a mirror
    base_url: str = ""


@dataclass
class SimpleHostConfig:
    """Configuration for the plain single-POST hosts."""

    base_url: str = ""


@dataclass
class UploadConfig:
    enabled: bool = True
    #: adapter id; see dcs.core.upload.registry
    provider: str = "gofile"
    #: never | clean | always  -- anything but 'clean' may publish malware
    upload_when: str = "clean"
    #: seconds to wait for one upload attempt before retrying
    timeout_s: int = 1800
    retries: int = 2
    #: delete the local archive once the upload succeeded
    delete_archive_after_upload: bool = False
    #: one section per adapter id; base_url is only needed for a mirror
    gofile: GofileConfig = field(default_factory=GofileConfig)
    pixeldrain: PixeldrainConfig = field(default_factory=PixeldrainConfig)
    x0at: SimpleHostConfig = field(default_factory=SimpleHostConfig)
    tempsh: SimpleHostConfig = field(default_factory=SimpleHostConfig)
    catbox: SimpleHostConfig = field(default_factory=SimpleHostConfig)
    tmpfiles: SimpleHostConfig = field(default_factory=SimpleHostConfig)
    litterbox: SimpleHostConfig = field(default_factory=SimpleHostConfig)


@dataclass
class EmailConfig:
    #: Off until a server and a recipient are configured: an installation that
    #: has neither cannot send anything, and a configuration that its own
    #: validator calls broken is a bad starting point (the settings page would
    #: open with a warning and refuse to save).  The pipeline reports the reason
    #: it skipped the stage either way.
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    #: starttls | ssl | none
    security: str = "starttls"
    username: str = ""
    password: str = ""
    password_command: str = ""
    from_addr: str = ""
    from_name: str = "Ducky Can Shoot"
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    bcc_addrs: list[str] = field(default_factory=list)
    reply_to: str = ""
    subject_template: str = "[DCS] {verdict} - {device} ({files} files)"
    #: attach the HTML report and the findings CSV
    attach_report: bool = True
    #: send even when the drive is clean (the whole point of the tool)
    always_send: bool = True
    #: seconds to wait for the server
    timeout_s: int = 60
    #: do not verify the TLS certificate (self signed internal relays)
    insecure_tls: bool = False
    #: file used by --dry-run to dump the message instead of sending it
    dry_run_dir: str = ""


@dataclass
class ReportConfig:
    enabled: bool = True
    directory: str = ""
    format: str = "html"
    #: include the per-file list of every scanned file (big for large sticks)
    include_file_list: bool = False
    #: maximum findings stored in the report
    max_findings: int = 500
    #: keep a JSON copy of the result for automation
    write_json: bool = True


@dataclass
class GuiConfig:
    #: the interface deliberately follows the Plasma colour scheme and icon
    #: theme instead of offering a theme switch; Kirigami 6 exposes colour sets
    #: (Window/View/Button/...), not a light/dark override.
    remember_window_size: bool = True
    window_width: int = 1180
    window_height: int = 780
    show_log_pane: bool = True
    auto_select_new_device: bool = True
    #: open the result page automatically when the job finishes
    switch_to_result: bool = True


#: the four reactions to a newly plugged medium, from silent to fully automatic
WATCH_MODES = ("off", "notify", "open", "open_run")


@dataclass
class WatchConfig:
    """What happens when a human plugs a medium in.

    The watcher is a listener, not a service: it is started by the interface (or
    by `dcs watch`, which the autostart entry runs) and it only ever *reacts* to
    media that appear while it is running.  Media that are already attached when
    it starts are ignored, so a stick that was plugged in yesterday does not
    trigger anything when the session starts.
    """

    #: off      - never watch, never react
    #: notify   - desktop notification with an "Open Ducky Can Shoot" button
    #: open     - open the interface with the new medium selected
    #: open_run - open the interface and start the job straight away
    mode: str = "off"
    #: seconds between two lsblk polls while the watcher runs
    interval_s: float = 3.0
    #: ignore media that are already attached when the watcher starts
    ignore_existing: bool = True
    #: how long the notification (and its button) stays on screen
    notify_timeout_ms: int = 20000
    #: start the watcher with the desktop session (the autostart entry)
    autostart: bool = False


@dataclass
class UpdateConfig:
    """Whether DCS looks for a newer release, and whether it may install one.

    The check asks the GitHub release API for the newest published release and
    compares it with the running version.  It is a read-only request: nothing
    is downloaded and nothing changes until the user presses the button.
    """

    #: check when the interface starts
    check_on_start: bool = True
    #: hours between two automatic checks (0 = every start)
    check_interval_hours: int = 6
    #: the whole feature can be switched off, for air-gapped machines
    enabled: bool = True


@dataclass
class AppConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    #: where this config was loaded from (informational)
    source_path: str = ""

    # -- derived paths ----------------------------------------------------
    @property
    def output_dir(self) -> Path:
        return Path(self.general.output_dir).expanduser() if self.general.output_dir else default_data_dir()

    @property
    def reports_dir(self) -> Path:
        return Path(self.report.directory).expanduser() if self.report.directory else default_reports_dir()

    @property
    def work_dir(self) -> Path:
        return default_work_dir()

    # -- secret resolution ------------------------------------------------
    def resolve_secret(self, value: str, command: str, env_var: str) -> str:
        return resolve_secret(value, command, env_var)

    def smtp_password(self) -> str:
        return self.resolve_secret(
            self.email.password, self.email.password_command, "DCS_SMTP_PASSWORD"
        )

    def smtp_username(self) -> str:
        return os.environ.get("DCS_SMTP_USERNAME") or self.email.username

    def gofile_token(self) -> str:
        return self.resolve_secret(
            self.upload.gofile.token, self.upload.gofile.token_command, "DCS_GOFILE_TOKEN"
        )

    def pixeldrain_api_key(self) -> str:
        return self.resolve_secret(
            self.upload.pixeldrain.api_key,
            self.upload.pixeldrain.api_key_command,
            "DCS_PIXELDRAIN_API_KEY",
        )

    def virustotal_api_key(self) -> str:
        return self.resolve_secret(
            self.scan.intel.virustotal_api_key,
            self.scan.intel.virustotal_api_key_command,
            "DCS_VIRUSTOTAL_API_KEY",
        )

    def metadefender_api_key(self) -> str:
        return self.resolve_secret(
            self.scan.intel.metadefender_api_key,
            self.scan.intel.metadefender_api_key_command,
            "DCS_METADEFENDER_API_KEY",
        )

    # -- serialisation ----------------------------------------------------
    def to_dict(self, *, mask_secrets: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data.pop("source_path", None)
        if mask_secrets:
            data["email"]["password"] = "***" if data["email"]["password"] else ""
            data["upload"]["gofile"]["token"] = "***" if data["upload"]["gofile"]["token"] else ""
            data["upload"]["pixeldrain"]["api_key"] = (
                "***" if data["upload"]["pixeldrain"]["api_key"] else ""
            )
            intel = data["scan"]["intel"]
            for key in ("virustotal_api_key", "metadefender_api_key"):
                intel[key] = "***" if intel[key] else ""
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        return _build(cls, data or {})

    def merge(self, data: dict[str, Any]) -> None:
        """Overlay a (partial) dict, e.g. from a higher priority config file."""
        _overlay(self, data or {})

    def save(self, path: str | os.PathLike[str] | None = None, *, include_secrets: bool = True) -> str:
        target = Path(path) if path else (Path(self.source_path) if self.source_path else default_config_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        text = "# Ducky Can Shoot configuration\n# generated " + str(len(asdict(self))) + " keys\n"
        payload = self.to_dict(mask_secrets=not include_secrets)
        text += dumps_toml(payload)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        self.source_path = str(target)
        return str(target)

    def validation_problems(self) -> list[str]:
        """Human readable list of things that will break a run."""
        problems: list[str] = []
        if self.watch.mode not in WATCH_MODES:
            problems.append(
                f"watch.mode must be one of {', '.join(WATCH_MODES)}, not {self.watch.mode!r}"
            )
        if self.watch.interval_s < 0.5:
            problems.append(f"watch.interval_s must be at least 0.5, not {self.watch.interval_s!r}")
        if self.scan.engines:
            unknown = [e for e in self.scan.engines if e not in ENGINE_IDS]
            if unknown:
                problems.append(f"unknown scan engine(s): {', '.join(unknown)}")
        if not self.scan.engines:
            problems.append(
                "no scan engine enabled: enable 'clamav', 'heuristic', 'yara' and/or 'intel'"
            )
        if self.scan.yara.severity not in SEVERITY_NAMES:
            problems.append(
                f"scan.yara.severity must be one of {', '.join(SEVERITY_NAMES)}, "
                f"not {self.scan.yara.severity!r}"
            )
        if self.scan.intel.lookup not in LOOKUP_MODES:
            problems.append(
                f"scan.intel.lookup must be one of {', '.join(LOOKUP_MODES)}, "
                f"not {self.scan.intel.lookup!r}"
            )
        if self.scan.intel.upload_unknowns and self.scan.intel.lookup != "upload":
            problems.append(
                "scan.intel.upload_unknowns is on but scan.intel.lookup is "
                f"{self.scan.intel.lookup!r}: set lookup = \"upload\" to send files"
            )
        if self.scan.intel.rate_per_minute < 1:
            problems.append("scan.intel.rate_per_minute must be at least 1")
        if self.updates.check_interval_hours < 0:
            problems.append(
                f"updates.check_interval_hours must be 0 or more, "
                f"not {self.updates.check_interval_hours!r}"
            )
        if self.archive.format not in ("zip", "7z"):
            problems.append(f"archive.format must be zip or 7z, not {self.archive.format!r}")
        if self.archive.when not in ("clean", "always", "never"):
            problems.append(f"archive.when must be clean/always/never, not {self.archive.when!r}")
        if self.report.format not in ("html", "text", "both"):
            problems.append(f"report.format must be html, text or both, not {self.report.format!r}")
        if self.upload.upload_when not in ("clean", "always", "never"):
            problems.append(f"upload.upload_when must be clean/always/never, not {self.upload.upload_when!r}")
        if self.email.enabled:
            if not self.email.smtp_host:
                problems.append("email.enabled is true but email.smtp_host is empty")
            if not self.email.from_addr:
                problems.append("email.enabled is true but email.from_addr is empty")
            if not (self.email.to_addrs or self.email.cc_addrs or self.email.bcc_addrs):
                problems.append("email.enabled is true but no recipient is configured")
            if self.email.security not in ("starttls", "ssl", "none", "plain"):
                problems.append(f"email.security must be starttls/ssl/none, not {self.email.security!r}")
        return problems


# ---------------------------------------------------------------------------
# dotted option names ("watch.mode"), used by 'dcs config set'
# ---------------------------------------------------------------------------


def resolve_secret(value: str, command: str, env_var: str) -> str:
    """Environment variable > helper command > literal config value."""
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env
    if command:
        try:
            proc = subprocess.run(
                shlex.split(command),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0:
                return proc.stdout.strip()
            log.warning("secret command %r failed with %s", command, proc.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("secret command %r failed: %s", command, exc)
    return value


def option_paths(config: AppConfig | None = None) -> list[str]:
    """Every settable dotted option name, in configuration order."""
    root = config or AppConfig()
    names: list[str] = []
    for section_field in fields(type(root)):
        if section_field.name == "source_path":
            continue
        section = getattr(root, section_field.name)
        if not is_dataclass(section):
            continue
        for field_info in fields(section):
            if is_dataclass(_resolved_annotation(field_info.type)):
                continue  # a nested table (scan.clamav) is not a single value
            names.append(f"{section_field.name}.{field_info.name}")
    return names


def _resolved_annotation(annotation: Any) -> Any:
    """Turn a (possibly postponed) annotation into the type it names.

    "list[str]" is only a string here because of `from __future__ import
    annotations`, so the subscript is stripped before the name is looked up:
    without that, a list option would be stored as one long string.
    """
    if not isinstance(annotation, str):
        return annotation
    name = annotation.split("[", 1)[0].strip()
    if name in _BUILTIN_TYPES:
        return _BUILTIN_TYPES[name]
    return _RESOLVE.get(name, annotation)


def set_option(config: AppConfig, key: str, value: Any) -> tuple[bool, str]:
    """Set one dotted option from a string, e.g. ("watch.mode", "notify").

    Returns (ok, message).  The value goes through the same coercion the config
    loader uses, so "false" lands as False, "4000" as 4000 and a comma separated
    string as a list, and an unparseable value is refused instead of stored.
    """
    parts = [part for part in str(key).split(".") if part]
    if len(parts) != 2:
        return False, f"{key!r} is not an option name such as 'watch.mode'"
    section_name, field_name = parts
    section = getattr(config, section_name, None)
    if not is_dataclass(section):
        return False, f"there is no [{section_name}] section"
    known = {field_info.name: field_info for field_info in fields(section)}
    field_info = known.get(field_name)
    if field_info is None:
        return False, f"there is no option {key!r}"

    annotation = _resolved_annotation(field_info.type)
    if annotation is list or get_origin(annotation) is list:
        items = value if isinstance(value, list) else [
            part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()
        ]
        setattr(section, field_name, items)
        return True, f"{key} = {items!r}"

    previous = getattr(section, field_name)
    coerced = _coerce(field_info.type, value)
    if coerced is UNCONVERTIBLE:
        return False, f"{value!r} is not a valid value for {key}"
    setattr(section, field_name, coerced)
    # an option that the config itself calls a problem (an unknown mode, an
    # unknown archive format, an interval that is too small) is refused here:
    # 'dcs config show' would otherwise print a broken configuration
    problems = [p for p in config.validation_problems() if key in p]
    if problems:
        setattr(section, field_name, previous)
        return False, problems[0]
    return True, f"{key} = {coerced!r}"


# ---------------------------------------------------------------------------
# generic dataclass <-> dict plumbing
# ---------------------------------------------------------------------------


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Instantiate a (nested) dataclass from a plain dict, ignoring unknowns."""
    if not is_dataclass(cls):
        return data
    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}
    for key, value in data.items():
        spec = known.get(key)
        if spec is None:
            log.debug("ignoring unknown config key %s.%s", cls.__name__, key)
            continue
        coerced = _coerce(spec.type, value)
        if coerced is UNCONVERTIBLE:
            continue
        kwargs[key] = coerced
    try:
        return cls(**kwargs)
    except TypeError as exc:  # pragma: no cover - defensive
        log.error("invalid config for %s: %s", cls.__name__, exc)
        return cls()


#: returned by _coerce when a value cannot be converted; callers keep their
#: default instead of storing something of the wrong type
UNCONVERTIBLE = object()

#: builtin type names as they appear in postponed annotations
_BUILTIN_TYPES: dict[str, Any] = {
    "int": int,
    "float": float,
    "bool": bool,
    "str": str,
    "list": list,
    "dict": dict,
}


def _coerce(annotation: Any, value: Any) -> Any:
    if isinstance(annotation, str):
        # Postponed annotations (from __future__ import annotations) leave the
        # field type as a string.  Resolve the builtins, otherwise every type
        # check below silently falls through and bad values are stored as is.
        annotation = _BUILTIN_TYPES.get(annotation, _RESOLVE.get(annotation, annotation))
    if is_dataclass(annotation) and isinstance(value, dict):
        return _build(annotation, value)
    origin = get_origin(annotation)
    if origin is list and isinstance(value, list):
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        if args and is_dataclass(args[0]):
            return [_build(args[0], item) for item in value if isinstance(item, dict)]
        return list(value)
    if annotation is bool and not isinstance(value, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if annotation is int and not isinstance(value, bool):
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            log.warning("ignoring %r: an integer was expected", value)
            return UNCONVERTIBLE
    if annotation is float and not isinstance(value, bool):
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            log.warning("ignoring %r: a number was expected", value)
            return UNCONVERTIBLE
    return value


def _overlay(target: Any, data: dict[str, Any]) -> None:
    """Recursively apply a partial dict onto an existing dataclass instance."""
    for key, value in data.items():
        if not hasattr(target, key):
            log.debug("ignoring unknown config key %s", key)
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _overlay(current, value)
        else:
            coerced = _coerce(type(current) if current is not None else str, value)
            if coerced is not UNCONVERTIBLE:
                setattr(target, key, coerced)


#: small map so postponed string annotations can be resolved when needed
_RESOLVE: dict[str, Any] = {
    "GeneralConfig": GeneralConfig,
    "ClamAVConfig": ClamAVConfig,
    "YaraConfig": YaraConfig,
    "IntelConfig": IntelConfig,
    "ScanConfig": ScanConfig,
    "ArchiveConfig": ArchiveConfig,
    "GofileConfig": GofileConfig,
    "PixeldrainConfig": PixeldrainConfig,
    "SimpleHostConfig": SimpleHostConfig,
    "UploadConfig": UploadConfig,
    "EmailConfig": EmailConfig,
    "ReportConfig": ReportConfig,
    "GuiConfig": GuiConfig,
    "WatchConfig": WatchConfig,
    "UpdateConfig": UpdateConfig,
}


def escape_toml_string(value: str) -> str:
    """Escape a string for a TOML basic string.

    Control characters have to be escaped: writing a literal newline inside a
    basic string produces a file that tomllib rejects, and since the loader
    treats an unreadable file as "no overrides", the whole configuration would
    silently fall back to the defaults.
    """
    out: list[str] = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return "".join(out)


def dumps_toml(data: dict[str, Any]) -> str:
    """Minimal TOML writer for the config shape (no dependency required)."""
    lines: list[str] = []

    def scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, str):
            return f'"{escape_toml_string(value)}"'
        if value is None:
            return '""'
        return f'"{escape_toml_string(str(value))}"'

    def emit(section: dict[str, Any], prefix: str) -> None:
        simple = {k: v for k, v in section.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in section.items() if isinstance(v, dict)}
        if prefix and (simple or not nested):
            lines.append(f"[{prefix}]")
        for key, value in simple.items():
            if isinstance(value, list):
                if not value:
                    lines.append(f"{key} = []")
                elif all(isinstance(v, (str, int, float, bool)) for v in value):
                    lines.append(f"{key} = [" + ", ".join(scalar(v) for v in value) + "]")
                else:  # pragma: no cover - not used by the current schema
                    lines.append(f"{key} = []")
            else:
                lines.append(f"{key} = {scalar(value)}")
        if simple and nested:
            lines.append("")
        for key, value in nested.items():
            emit(value, f"{prefix}.{key}" if prefix else key)
            lines.append("")

    emit(data, "")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.error("cannot read %s: %s", path, exc)
        return {}


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    system_path: str | os.PathLike[str] | None = SYSTEM_CONFIG,
) -> AppConfig:
    """Load the layered configuration.

    *path* (when given) replaces the per-user file and becomes the save
    target, which is what the CLI --config option and the GUI settings page
    both want.
    """
    config = AppConfig()

    if system_path and Path(system_path).exists():
        config.merge(_read_toml(Path(system_path)))

    candidate = Path(path) if path else default_config_path()
    expanded = candidate.expanduser()
    if expanded.exists():
        config.merge(_read_toml(expanded))
    elif path:
        log.warning("config file %s does not exist, using defaults", expanded)

    config.source_path = str(expanded)

    # environment overrides for a few high value keys
    env_map = {
        "DCS_PROVIDER": ("upload", "provider"),
        "DCS_UPLOAD_WHEN": ("upload", "upload_when"),
        "DCS_SMTP_HOST": ("email", "smtp_host"),
        "DCS_SMTP_PORT": ("email", "smtp_port"),
        "DCS_SMTP_FROM": ("email", "from_addr"),
        "DCS_SMTP_TO": ("email", "to_addrs"),
        "DCS_ENGINES": ("scan", "engines"),
        "DCS_LOOKUP": ("scan.intel", "lookup"),
        "DCS_ARCHIVE_DIR": ("general", "output_dir"),
        "DCS_REPORT_DIR": ("report", "directory"),
    }
    for env_var, (section, key) in env_map.items():
        raw = os.environ.get(env_var)
        if not raw:
            continue
        section_obj: Any = config
        for part in section.split("."):
            section_obj = getattr(section_obj, part)
        if key in ("to_addrs", "engines"):
            values = [part.strip() for part in raw.split(",") if part.strip()]
            setattr(section_obj, key, values)
        elif key == "smtp_port":
            try:
                setattr(section_obj, key, int(raw))
            except ValueError:
                log.warning("ignoring non numeric %s=%r", env_var, raw)
        else:
            setattr(section_obj, key, raw)

    return config


def write_default_config(path: str | os.PathLike[str] | None = None) -> str:
    """Write a commented example config if none exists yet."""
    target = Path(path).expanduser() if path else default_config_path()
    if target.exists():
        return str(target)
    config = AppConfig()
    config.source_path = str(target)
    config.save(target)
    return str(target)
