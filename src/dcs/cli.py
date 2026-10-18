"""Command line interface.

The CLI is the headless twin of the GUI: same pipeline, same configuration,
same result object, but a text progress line instead of a window.  It is meant
to be usable from a script, so the exit code carries the verdict:

    0  clean
    1  suspicious or infected
    2  the run failed, was cancelled, or could not be evaluated
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from dcs.core.config import (
    WATCH_MODES,
    default_config_path,
    load_config,
    option_paths,
    set_option,
    write_default_config,
)
from dcs.core.device import list_removable_devices
from dcs.core.events import (
    Finding,
    Stage,
    StageProgress,
    StageState,
    severity_rank,
)
from dcs.core.installer import (
    Installer,
    build_plan,
    manual_instructions,
    plan_summary,
)
from dcs.core.installer import verify as verify_clamav
from dcs.core.log import configure_logging, get_logger
from dcs.core.pipeline import JobRequest, Pipeline
from dcs.core.scanner import ENGINE_LABELS, Scanner
from dcs.core.upload import build_provider, provider_catalog
from dcs.core.util import human_bytes, human_duration, human_rate
from dcs.version import APP_HOMEPAGE, APP_LICENSE, APP_NAME, __version__

log = get_logger("cli")

EXIT_CLEAN = 0
EXIT_THREAT = 1
EXIT_ERROR = 2

USE_COLOUR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

COLOURS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
}


def colour(text: str, name: str) -> str:
    if not USE_COLOUR or name not in COLOURS:
        return text
    return f"{COLOURS[name]}{text}{COLOURS['reset']}"


# ---------------------------------------------------------------------------
# output sinks
# ---------------------------------------------------------------------------


class CliSink:
    """Prints pipeline events as a single updating line plus stage summaries."""

    #: how often a non interactive run reports progress (seconds)
    PLAIN_INTERVAL = 5.0

    def __init__(self, *, quiet: bool = False, stream: Any = None) -> None:
        self.quiet = quiet
        self.stream = stream or sys.stderr
        self.interactive = self.stream.isatty() and not quiet
        self.findings: list[Finding] = []
        self._last_line = ""
        self._last_plain = 0.0
        self._started: dict[Stage, float] = {}
        self._stages: list[tuple[str, str, str, float]] = []

    # -- helpers ---------------------------------------------------------
    def _write(self, text: str, *, newline: bool = True) -> None:
        if self.quiet:
            return
        if newline:
            self.stream.write(text + "\n")
        else:
            self.stream.write(text)

    def _progress_line(self, text: str) -> None:
        if self.quiet or not self.interactive:
            return
        padding = max(0, len(self._last_line) - len(text))
        self.stream.write("\r" + text + " " * padding)
        self.stream.flush()
        self._last_line = text

    # -- EventSink -------------------------------------------------------
    def on_stage(self, stage: Stage, state: StageState, message: str = "") -> None:
        if self.interactive and self._last_line:
            self.stream.write("\r" + " " * len(self._last_line) + "\r")
            self._last_line = ""
        if state is StageState.RUNNING:
            self._started[stage] = time.monotonic()
            self._write(colour(f"==> {stage.value}", "bold") + (f": {message}" if message else ""))
        elif state in (StageState.DONE, StageState.FAILED, StageState.SKIPPED, StageState.CANCELLED):
            elapsed = time.monotonic() - self._started.get(stage, time.monotonic())
            self._stages.append((stage.value, state.value, message, elapsed))
            marker = {
                StageState.DONE: colour("ok", "green"),
                StageState.FAILED: colour("failed", "red"),
                StageState.SKIPPED: colour("skipped", "yellow"),
                StageState.CANCELLED: colour("cancelled", "yellow"),
            }.get(state, state.value)
            suffix = f" - {message}" if message else ""
            self._write(f"    {stage.value:<16} {marker:<18} {human_duration(elapsed):>8}{suffix}")

    def on_progress(self, progress: StageProgress) -> None:
        if self.quiet:
            return
        parts: list[str] = []
        if progress.total:
            parts.append(f"{progress.processed:,}/{progress.total:,} files")
        elif progress.processed:
            parts.append(f"{progress.processed:,} files")
        if progress.bytes_total:
            parts.append(f"{human_bytes(progress.bytes_processed)}/{human_bytes(progress.bytes_total)}")
        parts.append(f"{progress.percent * 100:5.1f}%")
        if progress.rate:
            parts.append(human_rate(progress.rate))
        if progress.eta is not None:
            parts.append(f"ETA {human_duration(progress.eta)}")
        line = f"    [{progress.stage.value}] " + " · ".join(parts)
        if progress.current:
            line += f" · {os.path.basename(progress.current)[:48]}"

        if self.interactive:
            self._progress_line(line)
            return
        # not a terminal: one line every few seconds keeps logs readable
        now = time.monotonic()
        if now - self._last_plain >= self.PLAIN_INTERVAL:
            self._last_plain = now
            self._write(line)

    def on_finding(self, finding: Finding) -> None:
        self.findings.append(finding)
        if self.quiet:
            return
        if self.interactive and self._last_line:
            self.stream.write("\r" + " " * len(self._last_line) + "\r")
            self._last_line = ""
        severity = str(finding.severity).upper()
        palette = "red" if severity_rank(finding.severity) >= 3 else "yellow"
        self._write(
            f"    {colour(f'{severity:<8}', palette)} {finding.threat} · {finding.path}"
        )

    def on_log(self, level: str, message: str) -> None:
        if level == "error":
            self._write(colour(f"    error: {message}", "red"))

    def on_summary(self, summary: dict[str, Any]) -> None:
        self.summary = summary


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def format_result(result: dict[str, Any]) -> str:
    """Human readable block for the end of a CLI run."""
    verdict = str(result.get("verdict", "unknown"))
    palette = {
        "clean": "green",
        "suspicious": "yellow",
        "infected": "red",
    }.get(verdict, "dim")
    lines = [
        "",
        colour(f"VERDICT: {verdict.upper()}", palette) + f"{'':<4}{APP_NAME} {__version__}",
        "-" * 72,
        f"  medium        : {result.get('sourceLabel', '')} ({result.get('sourcePath', '')})",
        f"  files scanned : {result.get('filesScanned', 0):,} ({result.get('bytesScannedText', '-')})",
        f"  findings      : {result.get('findingCount', 0):,} ({result.get('threatCount', 0)} threatening)",
        f"  duration      : {result.get('durationText', '-')}",
    ]
    covered: set[str] = set()
    archive = result.get("archive") or {}
    if archive.get("ok"):
        lines.append(f"  archive       : {archive.get('archiveBytesText')} in {archive.get('partCount')} file(s)")
        lines.append(f"                  {archive.get('primaryPath')}")
        covered.add("archive")
    if result.get("shareLink"):
        lines.append(f"  share link    : {result['shareLink']}")
        covered.add("upload")
    elif result.get("uploadSkippedReason"):
        lines.append(f"  upload        : {result['uploadSkippedReason']}")
        covered.add("upload")
    email = result.get("email") or {}
    if email:
        covered.add("email")
        if email.get("sent"):
            lines.append(f"  email         : sent to {', '.join(email.get('recipients', []))}")
        elif email.get("dryRun"):
            lines.append(f"  email         : dry run written to {email.get('messagePath')}")
        elif email.get("error"):
            lines.append(colour(f"  email         : {email['error']}", "red"))
    reports = result.get("reports") or {}
    if reports.get("html"):
        lines.append(f"  report        : {reports['html']}")
        covered.add("report")

    # stages that were skipped or failed still owe the user an explanation
    for timing in result.get("timings") or []:
        stage = str(timing.get("stage", ""))
        if stage in covered or timing.get("state") not in ("skipped", "failed"):
            continue
        palette = "red" if timing.get("state") == "failed" else None
        text = f"  {stage:<13} : {timing.get('message', '')}"
        lines.append(colour(text, palette) if palette else text)
    warnings = result.get("warnings") or []
    if warnings:
        lines.append("  notes:")
        for warning in warnings[:6]:
            lines.append(f"    - {warning}")
    lines.append("-" * 72)
    return "\n".join(lines)


def exit_code_for(verdict: str) -> int:
    if verdict == "clean":
        return EXIT_CLEAN
    if verdict in ("suspicious", "infected"):
        return EXIT_THREAT
    return EXIT_ERROR


def parse_paths(values: list[str] | None) -> list[str]:
    if not values:
        return []
    parts: list[str] = []
    for value in values:
        parts.extend(part.strip() for part in value.replace(";", ",").split(",") if part.strip())
    return parts


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from dcs.gui.app import run_gui
    except ImportError as exc:
        print(f"{APP_NAME} needs PyQt6 for the graphical interface: {exc}", file=sys.stderr)
        print("Install it (for example: sudo apt install python3-pyqt6 python3-pyqt6.qtqml) "
              "or use the headless commands such as 'dcs scan'.", file=sys.stderr)
        return EXIT_ERROR
    return run_gui(
        ["dcs"],
        config=load_config(args.config),
        device=getattr(args, "device", "") or "",
        autorun=bool(getattr(args, "autorun", False)),
    )


# ---------------------------------------------------------------------------
# watching for newly plugged media
# ---------------------------------------------------------------------------


def gui_command(device: str, *, autorun: bool = False) -> list[str]:
    """How to start the interface on *device*, installed or from a checkout."""
    launcher = shutil.which("dcs")
    argv = [launcher] if launcher else [sys.executable, "-m", "dcs"]
    argv += ["gui", "--device", device]
    if autorun:
        argv.append("--autorun")
    return argv


def launch_gui(device: str, *, autorun: bool = False) -> bool:
    """Start the interface on a medium, detached from this process."""
    argv = gui_command(device, autorun=autorun)
    try:
        subprocess.Popen(
            argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"could not start the interface: {exc}", file=sys.stderr)
        return False
    print(f"starting {APP_NAME} on {device}" + (" and running the job" if autorun else ""))
    return True


#: one line per mode, used by 'dcs watch --status' and by the help text
MODE_SUMMARY: dict[str, str] = {
    "off": "nothing; the watcher exits immediately",
    "notify": "desktop notification with an Open button, nothing is scanned",
    "open": "start the interface on the new medium",
    "open_run": "start the interface and run the whole job",
}


def _watch_status(config: Any, mode: str) -> int:
    from dcs.core import autostart, service, session

    running = session.gui_pid()
    watcher = service.watcher_pid()
    interface_state = f"running (pid {running})" if running else "not running"
    watcher_state = f"running (pid {watcher})" if watcher else "not running"
    print(f"mode            : {mode}")
    print(f"watch interval  : {config.watch.interval_s:g}s")
    print(f"ignore existing : {config.watch.ignore_existing}")
    print(f"autostart file  : {autostart.autostart_path()}"
          f" ({'installed' if autostart.is_enabled() else 'not installed'})")
    print(f"autostart runs  : {autostart.default_exec()} watch")
    print(f"interface       : {interface_state}")
    print(f"watcher         : {watcher_state}")
    print(f"watcher log     : {service.log_path()}")
    print(f"would do        : {MODE_SUMMARY.get(mode, mode)}")
    return EXIT_CLEAN


def _watch_test_notification(notifier: Any) -> int:
    """Send one notification through the real code path and wait for the answer.

    Troubleshooting a watcher nobody can see is hopeless without this: it says
    which mechanism was used and whether the button came back.
    """
    kind = type(notifier).__name__
    print(f"sending a test notification through {kind}")
    print("press the button in the notification (or wait for it to expire)")
    choice = notifier.notify(
        "Ducky Can Shoot: notification test",
        "This is the notification a new medium produces. Nothing is scanned and no "
        "medium was read; pressing the button would open the interface.",
        actions=(("open", f"Open {APP_NAME}"),),
    )
    if choice == "open":
        print("answer: the button works")
        return EXIT_CLEAN
    print("answer: no button press was received")
    if kind == "DesktopNotifier":
        print(
            "note: notify-send is showing this notification without its buttons. "
            "Install gdbus (glib2) so the button appears, or use watch.mode open.",
            file=sys.stderr,
        )
    return EXIT_THREAT


def cmd_watch(args: argparse.Namespace) -> int:
    """Listen for media that are plugged in and react the way the config says."""
    from dcs.core import service
    from dcs.core.notify.desktop import ACTION_OPEN, build_notifier
    from dcs.core.session import gui_running
    from dcs.core.watch import DeviceWatcher

    if getattr(args, "stop", False):
        stopped, message = service.stop()
        print(message)
        return EXIT_CLEAN if stopped or "no watcher" in message else EXIT_ERROR

    if getattr(args, "start", False):
        # the same thing the settings page does when the mode is enabled: make
        # sure something is listening from now on, without waiting for a login
        started, message = service.start()
        print(("started: " if started else "nothing to do: ") + message)
        return EXIT_CLEAN if started or "already" in message else EXIT_ERROR

    config = load_config(args.config)
    mode = (getattr(args, "mode", "") or config.watch.mode or "off").strip().lower()
    if mode != "off" and not getattr(args, "once", False):
        # a watcher nobody watches: keep a log of what it did
        configure_logging(
            level=os.environ.get("DCS_LOG_LEVEL", "INFO"),
            logfile=str(service.log_path()),
            quiet=True,
        )
    if mode not in WATCH_MODES:
        print(
            f"unknown watch mode {mode!r}: use one of {', '.join(WATCH_MODES)}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if getattr(args, "status", False):
        return _watch_status(config, mode)

    watcher = DeviceWatcher(config, mode=mode, interval_s=getattr(args, "interval", None))

    if getattr(args, "once", False):
        # a snapshot: everything attached right now, as if it had just appeared
        watcher.ignore_existing = False
        events = watcher.poll()
        if args.json:
            print(json.dumps([event.to_dict() for event in events], indent=2, ensure_ascii=False))
            return EXIT_CLEAN
        if not events:
            print("No removable medium is attached.")
            return EXIT_CLEAN
        print(f"{'NODE':<14}{'LABEL':<20}{'SIZE':>10}  {'FS':<8}MOUNT")
        for event in events:
            print(
                f"{event.node:<14}{(event.label or '-')[:19]:<20}"
                f"{event.device.size_text:>10}  {event.device.fstype:<8}"
                f"{event.mount_path or '(not mounted)'}"
            )
        return EXIT_CLEAN

    if mode == "off":
        print("The USB watcher is off, so nothing was started.")
        print("Turn it on with:  dcs config set watch.mode notify")
        print(f"Modes: {', '.join(WATCH_MODES)}  (see 'dcs watch --help' for what each one does)")
        return EXIT_CLEAN

    notifier = build_notifier(timeout_ms=config.watch.notify_timeout_ms)

    if getattr(args, "test", False):
        return _watch_test_notification(notifier)

    if mode == "notify" and not notifier.available():
        print(
            "note: neither gdbus nor notify-send is installed, so no notification "
            "can be shown; new media are printed here instead.",
            file=sys.stderr,
        )

    stop = threading.Event()

    def on_signal(signum: int, frame: Any) -> None:  # pragma: no cover - signal handler
        print("stopping...", file=sys.stderr)
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None:
            # not the main thread (an embedding application or a test): there is
            # nothing to install a handler on, and stop still ends the loop
            with contextlib.suppress(OSError, ValueError):
                signal.signal(number, on_signal)

    def react(event: Any) -> None:
        """What the configured mode does with one new medium."""
        if gui_running():
            # the interface is open and watches for itself: two reactions would
            # mean two notifications and possibly two jobs
            log.debug("ignoring %s: the interface is running", event.node)
            return
        if args.json:
            print(json.dumps(event.to_dict(), ensure_ascii=False), flush=True)
        else:
            print(f"new medium: {event.summary()}", flush=True)
        if mode == "notify":
            def ask(node: str = event.node, summary: str = event.summary(), label: str = event.label) -> None:
                # the notification waits for the user, so it must not hold up
                # the polling loop: a second medium plugged in meanwhile is
                # still noticed while this one is on screen
                choice = notifier.notify(
                    f"USB medium plugged in: {label}",
                    f"{summary}. Open {APP_NAME} to scan it, archive it and email "
                    "the verdict; nothing has been read from the medium yet.",
                    actions=((ACTION_OPEN, f"Open {APP_NAME}"),),
                )
                if choice == ACTION_OPEN:
                    launch_gui(node)

            threading.Thread(target=ask, daemon=True, name="dcs-notify").start()
        elif mode == "open":
            launch_gui(event.node)
        elif mode == "open_run":
            launch_gui(event.node, autorun=True)

    # Claim the watcher role before the loop: a second copy (the autostart entry
    # racing a 'Start now' from the settings page) has to leave quietly instead
    # of notifying the user twice.
    if not service.claim():
        print(f"another watcher is already running (pid {service.watcher_pid()})")
        return EXIT_CLEAN

    print(
        f"Watching for new media (mode: {mode}, every {watcher.interval_s:g}s). "
        f"Media that are already attached are ignored. log: {service.log_path()} "
        "Ctrl+C stops.",
        flush=True,
    )
    try:
        with contextlib.suppress(KeyboardInterrupt):  # pragma: no cover - interactive only
            watcher.run(react, stop=stop)
    finally:
        service.release()
    print("Stopped watching.", flush=True)
    return EXIT_CLEAN


def cmd_devices(args: argparse.Namespace) -> int:
    devices = list_removable_devices()
    if args.json:
        print(json.dumps([device.to_dict() for device in devices], indent=2))
        return EXIT_CLEAN
    if not devices:
        print("No removable medium detected. Plug a device in and try again.")
        return EXIT_CLEAN
    print(f"{'NODE':<14}{'LABEL':<20}{'SIZE':>10}  {'FS':<8}{'BUS':<6}MOUNT")
    for device in devices:
        print(
            f"{device.path:<14}{(device.display_name or '-')[:19]:<20}"
            f"{device.size_text:>10}  {device.fstype:<8}{device.transport.upper():<6}"
            f"{device.mount_path or '(not mounted)'}"
        )
    return EXIT_CLEAN


def apply_engine_flags(config: Any, args: argparse.Namespace) -> None:
    """Apply --engines/--yara-rules/--lookup/--upload-unknowns to a config."""
    if getattr(args, "engines", None):
        config.scan.engines = parse_paths([args.engines])
    if getattr(args, "yara_rules", None):
        config.scan.yara.rules = list(args.yara_rules)
        config.scan.yara.enabled = True
        if "yara" not in config.scan.engines:
            config.scan.engines.append("yara")
    lookup = getattr(args, "lookup", None)
    if lookup:
        config.scan.intel.lookup = lookup
    if getattr(args, "upload_unknowns", False):
        # asking twice on purpose: the flag speaks for this run, and the engine
        # still needs scan.intel.lookup = "upload" to consider uploading at all
        config.scan.intel.lookup = "upload"
        config.scan.intel.upload_unknowns = True
        print(
            colour("Note: unknown files will be sent to the provider for analysis.", "yellow"),
            file=sys.stderr,
        )


def cmd_scan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    apply_engine_flags(config, args)
    if getattr(args, "max_file_size_mb", None) is not None:
        config.scan.max_file_size_mb = args.max_file_size_mb

    sink = CliSink(quiet=args.quiet)
    scanner = Scanner(config.scan)
    result = scanner.run(args.path, sink=sink)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        if sink.findings and not args.quiet:
            print()
        print(colour(f"VERDICT: {result.verdict.value.upper()}", {
            "clean": "green", "suspicious": "yellow", "infected": "red",
        }.get(result.verdict.value, "dim")))
        print(
            f"  {len(result.findings)} finding(s) in {result.stats.files_scanned:,} files "
            f"({human_bytes(result.stats.bytes_scanned)}) in {human_duration(result.stats.duration)}"
        )
        for warning in result.warnings[:5]:
            print(f"  note: {warning}")
    return exit_code_for(result.verdict.value)


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    apply_engine_flags(config, args)
    if args.provider:
        config.upload.provider = args.provider
    if args.no_upload:
        config.upload.enabled = False
    if args.no_email:
        config.email.enabled = False
    recipients = parse_paths(args.to)
    if recipients:
        config.email.to_addrs = recipients

    request = JobRequest(
        source_path=args.path or args.device or "",
        device_path=args.device or "",
        label=args.label or "",
        do_archive=None if not args.no_archive else False,
        do_upload=None if not args.no_upload else False,
        do_email=None if not args.no_email else False,
        recipients=recipients,
        email_dry_run=args.dry_run_email,
        assume_yes=args.yes,
        output_dir=args.output_dir or "",
    )
    if not request.source_path and not request.device_path:
        print("Select a device with --device or a folder with --path.", file=sys.stderr)
        return EXIT_ERROR

    sink = CliSink(quiet=args.quiet)
    cancel = threading.Event()
    if not args.yes and config.upload.enabled and config.general.confirm_before_upload:
        print(
            colour(
                "Note: a clean image is uploaded to a public host and the link is emailed. "
                "Use --yes to skip this notice, or --no-upload to keep everything local.",
                "yellow",
            ),
            file=sys.stderr,
        )
    try:
        result = Pipeline(config).run(request, sink=sink, cancel=cancel)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        cancel.set()
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_ERROR

    summary = result.to_dict()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(format_result(summary))
    return exit_code_for(summary["verdict"])


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, hint: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "hint": hint})

    add(
        "python",
        sys.version_info >= (3, 11),
        f"{sys.version.split()[0]} at {sys.executable}",
        "DCS needs Python 3.11 or newer (tomllib).",
    )
    add("config file", True, config.source_path or "(defaults)")
    problems = config.validation_problems()
    add(
        "configuration",
        not problems,
        "; ".join(problems) if problems else "no problems found",
        "Fix the values in the settings page or in the config file.",
    )

    for status in Scanner(config.scan).statuses():
        add(
            f"engine {status.engine_id}",
            status.available,
            (status.version or "") + (" " if status.version else "") + status.detail,
            "Install it from the interface (Settings, Install ClamAV) or run "
            "'dcs install-clamav' for signature based detection.",
        )

    for tool, hint in (
        ("lsblk", "Install util-linux: it is what detects removable media."),
        ("udisksctl", "Install udisks2 so a stick can be mounted without root."),
        ("zip", "Install zip, or DCS falls back to its slower built-in archiver."),
        ("7z", "Optional: only needed for 7z archives."),
    ):
        path = shutil.which(tool)
        add(f"tool {tool}", bool(path), path or "not found", hint)

    for label, directory in (("output", str(config.output_dir)), ("reports", str(config.reports_dir))):
        try:
            os.makedirs(directory, exist_ok=True)
            probe = os.path.join(directory, ".dcs-write-test")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.unlink(probe)
            add(f"{label} directory", True, directory)
        except OSError as exc:
            add(f"{label} directory", False, f"{directory}: {exc}", "Make the directory writable.")

    if not args.offline:
        provider = build_provider(
            config.upload,
            config.upload.provider,
            secrets={
                "gofile_token": config.gofile_token(),
                "pixeldrain_api_key": config.pixeldrain_api_key(),
            },
        )
        ok, detail = provider.availability()
        add(
            f"upload provider {provider.provider_id}",
            ok,
            detail or "reachable",
            "Check your network or pick another provider with --provider or the settings page.",
        )
    if args.smtp:
        from dcs.core.notify import Notifier

        notifier = Notifier(
            config.email,
            password=config.smtp_password(),
            username=config.smtp_username(),
        )
        ok, detail = notifier.test_connection()
        add("smtp", ok, detail, "Check host, port, security mode and credentials.")

    if args.json:
        print(json.dumps(checks, indent=2))
    else:
        print(f"{APP_NAME} {__version__} doctor\n")
        for check in checks:
            marker = colour(" ok ", "green") if check["ok"] else colour("FAIL", "red")
            print(f"[{marker}] {check['check']:<26} {check['detail']}")
            if not check["ok"] and check["hint"]:
                print(f"       {colour('hint:', 'dim')} {check['hint']}")
    return EXIT_CLEAN if all(check["ok"] for check in checks) else EXIT_THREAT


def cmd_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if getattr(args, "options", False):
        print("\n".join(option_paths(config)))
        return EXIT_CLEAN
    if args.action == "set":
        if not args.key or args.value is None:
            print("usage: dcs config set KEY VALUE   (see 'dcs config --options')", file=sys.stderr)
            return EXIT_ERROR
        ok, message = set_option(config, args.key, args.value)
        if not ok:
            print(f"error: {message}", file=sys.stderr)
            return EXIT_ERROR
        path = config.save()
        print(f"{message}  (saved to {path})")
        return EXIT_CLEAN
    if args.action == "path":
        print(config.source_path or str(default_config_path()))
        return EXIT_CLEAN
    if args.action == "init":
        path = write_default_config(args.config)
        print(f"Configuration ready at {path}")
        return EXIT_CLEAN
    if args.action == "show":
        from dcs.core.config import dumps_toml

        print(dumps_toml(config.to_dict(mask_secrets=not args.show_secrets)))
        return EXIT_CLEAN
    print("usage: dcs config {init|show|path|set}", file=sys.stderr)
    return EXIT_ERROR


def cmd_engines(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    statuses = [status.to_dict() for status in Scanner(config.scan).statuses()]
    # an engine that is installed but not switched on is the most common reason
    # for "why did it not find anything", so it is listed here too
    enabled = set(config.scan.engines)
    for engine_id, label in ENGINE_LABELS.items():
        if engine_id in enabled:
            continue
        statuses.append(
            {
                "id": engine_id,
                "label": label,
                "available": False,
                "version": "",
                "detail": f"not enabled: add {engine_id!r} to scan.engines",
                "warnings": [],
            }
        )
    if args.json:
        print(json.dumps(statuses, indent=2))
        return EXIT_CLEAN
    for status in statuses:
        state = "available" if status["available"] else "unavailable"
        print(f"{status['label']}: {state} ({status['detail']})")
        for warning in status["warnings"]:
            print(f"  warning: {warning}")
    return EXIT_CLEAN


def cmd_providers(args: argparse.Namespace) -> int:
    catalog = provider_catalog()
    if args.json:
        print(json.dumps(catalog, indent=2))
        return EXIT_CLEAN
    for entry in catalog:
        limit = human_bytes(entry["maxBytes"]) if entry["maxBytes"] else "no published limit"
        key = "API key required" if entry["requiresKey"] else "no account needed"
        print(f"{entry['id']:<10} {entry['label']:<22} {key:<20} {limit}")
        if entry.get("notes"):
            print(f"           {entry['notes']}")
    return EXIT_CLEAN


def cmd_install_clamav(args: argparse.Namespace) -> int:
    """Install the optional ClamAV engine through the system package manager.

    The password is collected by sudo/pkexec, never by DCS: this command only
    writes the plan as a shell script and hands it to the privilege helper.
    """
    config = load_config(args.config)
    prefer = "terminal" if args.terminal else ("sudo" if sys.stdin.isatty() else "auto")
    plan = build_plan(config=config.scan.clamav, prefer=prefer)

    if args.json and args.dry_run:
        payload = plan.to_dict()
        payload["manual"] = manual_instructions(plan)
        payload["status"] = verify_clamav(config)
        print(json.dumps(payload, indent=2))
        return EXIT_CLEAN

    if not plan.steps:
        print(plan.blocker or "no installation commands are known for this system.", file=sys.stderr)
        return EXIT_ERROR

    print(plan_summary(plan))
    print()

    if args.dry_run:
        print(colour("Nothing was executed (--dry-run).", "yellow"))
        print()
        print(plan.script)
        return EXIT_CLEAN

    if not plan.possible:
        print(colour(f"Cannot run it automatically: {plan.blocker}", "red"))
        print()
        print(manual_instructions(plan))
        return EXIT_ERROR

    if not args.yes:
        try:
            answer = input("Run these commands now? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Nothing was executed. The commands above are ready to copy.")
            return EXIT_CLEAN

    outcome = Installer(plan).run(on_output=lambda line: print(line, flush=True))
    print()
    status = verify_clamav(config)
    print(status.get("line", ""))
    if status.get("warning"):
        print(colour(f"note: {status['warning']}", "yellow"))
    if outcome.ok:
        print(colour("ClamAV is installed.", "green"))
        return EXIT_CLEAN
    if outcome.cancelled:
        print(colour("Installation cancelled.", "yellow"))
        return EXIT_ERROR
    print(colour(f"Installation failed: {outcome.error}", "red"))
    return EXIT_ERROR


def cmd_version(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps({"name": APP_NAME, "version": __version__, "license": APP_LICENSE}))
    else:
        print(f"{APP_NAME} {__version__}")
        print(f"{APP_LICENSE} - {APP_HOMEPAGE}")
    return EXIT_CLEAN


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dcs",
        description=f"{APP_NAME}: scan a USB medium, archive it, upload it and email the verdict.",
        epilog="Exit codes: 0 clean, 1 suspicious or infected, 2 error or cancelled.",
    )
    parser.add_argument("--config", metavar="PATH", help="configuration file to use")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("DCS_LOG_LEVEL", "WARNING"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log verbosity (default: %(default)s)",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    gui = subparsers.add_parser("gui", help="start the graphical interface (default)")
    gui.add_argument("--device", help="open the interface on this medium (mounted if needed)")
    gui.add_argument(
        "--autorun",
        action="store_true",
        help="start the job on that medium as soon as the window is up",
    )
    gui.set_defaults(func=cmd_gui)

    watch = subparsers.add_parser(
        "watch",
        help="watch for media plugged in while this runs and react (see watch.mode)",
        description="Listen for newly plugged media and do what watch.mode says: "
        + "; ".join(f"{name} - {MODE_SUMMARY[name]}" for name in WATCH_MODES)
        + ". Media that are already attached when the watcher starts are ignored.",
    )
    watch.add_argument("--mode", choices=list(WATCH_MODES), help="override watch.mode for this run")
    watch.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="seconds between two polls (default: watch.interval_s)",
    )
    watch.add_argument("--once", action="store_true", help="report the media attached now and exit")
    watch.add_argument(
        "--start",
        action="store_true",
        help="start a watcher in the background (unless one is already running) and exit",
    )
    watch.add_argument("--stop", action="store_true", help="stop the background watcher and exit")
    watch.add_argument(
        "--test",
        action="store_true",
        help="send one test notification and report whether its button works",
    )
    watch.add_argument(
        "--status",
        action="store_true",
        help="show the mode, the autostart entry and the state of the interface",
    )
    watch.add_argument("--json", action="store_true", help="one JSON object per medium")
    watch.set_defaults(func=cmd_watch)

    devices = subparsers.add_parser("devices", help="list removable media")
    devices.add_argument("--json", action="store_true")
    devices.set_defaults(func=cmd_devices)

    scan = subparsers.add_parser("scan", help="scan a path and report findings")
    scan.add_argument("path", help="mount point or directory to scan")
    scan.add_argument(
        "--engines",
        help="comma separated engine ids (clamav, heuristic, yara, intel)",
    )
    scan.add_argument(
        "--yara-rules",
        action="append",
        metavar="PATH",
        help="YARA rule file or directory (repeatable); switches the yara engine on",
    )
    scan.add_argument(
        "--lookup",
        choices=("off", "hash", "upload"),
        help="reputation lookups for unknown files (scan.intel.lookup)",
    )
    scan.add_argument(
        "--upload-unknowns",
        action="store_true",
        help="send unknown files to the provider for analysis (needs an API key)",
    )
    scan.add_argument("--max-file-size-mb", type=int, default=None)
    scan.add_argument("--json", action="store_true", help="print the full result as JSON")
    scan.add_argument("--quiet", action="store_true")
    scan.set_defaults(func=cmd_scan)

    run = subparsers.add_parser("run", help="run the whole job: scan, archive, upload, email")
    run.add_argument("--device", help="device node such as /dev/sdb1 (mounted if needed)")
    run.add_argument("--path", help="directory to triage instead of a device")
    run.add_argument("--label", help="name used in reports, archives and emails")
    run.add_argument("--to", action="append", help="recipient address (repeatable, comma separated)")
    run.add_argument("--provider", help="upload provider id (see 'dcs providers')")
    run.add_argument(
        "--engines",
        help="comma separated engine ids (clamav, heuristic, yara, intel)",
    )
    run.add_argument(
        "--yara-rules",
        action="append",
        metavar="PATH",
        help="YARA rule file or directory (repeatable); switches the yara engine on",
    )
    run.add_argument(
        "--lookup",
        choices=("off", "hash", "upload"),
        help="reputation lookups for unknown files (scan.intel.lookup)",
    )
    run.add_argument(
        "--upload-unknowns",
        action="store_true",
        help="send unknown files to the provider for analysis (needs an API key)",
    )
    run.add_argument("--output-dir", help="where the archive is written")
    run.add_argument("--no-archive", action="store_true", help="skip the archive stage")
    run.add_argument("--no-upload", action="store_true", help="never upload anything")
    run.add_argument("--no-email", action="store_true", help="do not send the email")
    run.add_argument("--dry-run-email", action="store_true", help="write the message to disk instead")
    run.add_argument("--yes", action="store_true", help="do not ask anything, upload even so")
    run.add_argument("--json", action="store_true")
    run.add_argument("--quiet", action="store_true")
    run.set_defaults(func=cmd_run)

    doctor = subparsers.add_parser("doctor", help="check the environment and the configuration")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--offline", action="store_true", help="skip network reachability checks")
    doctor.add_argument("--smtp", action="store_true", help="also connect to the SMTP server")
    doctor.set_defaults(func=cmd_doctor)

    config = subparsers.add_parser("config", help="inspect, change or create the configuration")
    config.add_argument("action", choices=["init", "show", "path", "set"], nargs="?", default="show")
    config.add_argument("key", nargs="?", help="option name for 'set', such as watch.mode")
    config.add_argument("value", nargs="?", help="value for 'set'")
    config.add_argument("--show-secrets", action="store_true", help="do not mask passwords")
    config.add_argument("--options", action="store_true", help="print every settable option name")
    config.set_defaults(func=cmd_config)

    engines = subparsers.add_parser("engines", help="show the scan engines and their state")
    engines.add_argument("--json", action="store_true")
    engines.set_defaults(func=cmd_engines)

    providers = subparsers.add_parser("providers", help="list the available upload providers")
    providers.add_argument("--json", action="store_true")
    providers.set_defaults(func=cmd_providers)

    install = subparsers.add_parser(
        "install-clamav",
        help="install the optional ClamAV engine with the system package manager",
    )
    install.add_argument("--dry-run", action="store_true", help="print the plan and the script only")
    install.add_argument(
        "--terminal",
        action="store_true",
        help="open a terminal emulator instead of using the password dialog",
    )
    install.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    install.add_argument("--json", action="store_true", help="machine readable plan with --dry-run")
    install.set_defaults(func=cmd_install_clamav)

    version = subparsers.add_parser("version", help="print the version")
    version.add_argument("--json", action="store_true")
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "func", None) is None:
        args = parser.parse_args(["gui", *argv])

    configure_logging(args.log_level, logfile=None, quiet=getattr(args, "quiet", False))
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        log.debug("command failed", exc_info=True)
        print(colour(f"error: {exc}", "red"), file=sys.stderr)
        return EXIT_ERROR


def gui_main() -> int:
    """Entry point for the dcs-gui / ducky-can-shoot scripts."""
    return main(["gui"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
