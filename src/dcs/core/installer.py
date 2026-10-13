"""Install ClamAV for the user, without ever asking DCS to hold a password.

The scan engine is optional, but a machine without it only gets the built-in
heuristics.  Installing a package needs root, and a GUI process has no terminal
to type a password into, so this module does three things:

1. it works out which distribution and package manager the machine runs,
2. it writes a small, inspectable shell script with the exact steps
   (update the index, install, fetch signatures, start the daemon), and
3. it runs that script through whichever privilege helper the desktop offers:
   pkexec (a graphical polkit password dialog, the normal case on Plasma),
   a terminal emulator with sudo inside (when no polkit agent is running), or
   plain sudo when the caller is already on a terminal (the CLI).

DCS itself never sees the password: polkit or sudo collects it.  The generated
script contains nothing but fixed commands, is written with mode 0700 (owner
only), and is printed in full before it runs so the user can read or copy it
instead of executing it.
"""

from __future__ import annotations

import contextlib
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from dcs.core.log import get_logger
from dcs.core.util import atomic_write_text, iso_now

log = get_logger("installer")

#: called with every output line of the installation
OutputFn = Callable[[str], None]

#: markers the generated script prints so DCS can follow the individual steps
STEP_RE = re.compile(r"^=== DCS step (?P<index>\d+): (?P<title>.+?) ===$")
STEP_DONE_RE = re.compile(r"^=== DCS step (?P<index>\d+) finished with exit (?P<code>-?\d+) ===$")
#: exit code of the generated script when a required step fails: 10 * step index
STEP_FAILURE_BASE = 10

#: processes that prove a graphical polkit agent is available
POLKIT_AGENT_PATTERN = "polkit-.*agent|polkit-gnome|polkit-kde"

#: tried in this order when a terminal is needed
TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("konsole", ("--hold", "-e")),
    ("gnome-terminal", ("--",)),
    ("xfce4-terminal", ("--hold", "-e")),
    ("kitty", ("-e",)),
    ("alacritty", ("-e",)),
    ("tilix", ("-e",)),
    ("wezterm", ("start", "--")),
    ("foot", ("-e",)),
    ("xterm", ("-e",)),
)


@dataclass(frozen=True)
class PackageManager:
    """How to install ClamAV on one family of distributions."""

    id: str
    label: str
    install: tuple[tuple[str, ...], ...]
    refresh: tuple[tuple[str, ...], ...] = ()
    #: unit that keeps the signature database fresh
    updater_units: tuple[str, ...] = ()
    #: unit that runs the scanning daemon
    daemon_unit: str = ""
    #: service manager used to start those units
    service_manager: str = "systemd"
    #: run freshclam separately (some packages do it in a service already)
    freshclam: bool = True
    notes: tuple[str, ...] = ()


PACKAGE_MANAGERS: dict[str, PackageManager] = {
    "apt": PackageManager(
        id="apt",
        label="APT (Debian, Ubuntu, Mint, Pop!_OS, elementary)",
        refresh=(("apt-get", "update"),),
        install=(("apt-get", "install", "-y", "clamav", "clamav-daemon", "clamav-freshclam"),),
        updater_units=("clamav-freshclam",),
        daemon_unit="clamav-daemon",
        notes=(
            "The clamav-daemon unit only starts once the signature database exists, "
            "so the signatures are fetched first.",
            "The first signature download is around 300 MB and can take a few minutes.",
        ),
    ),
    "dnf": PackageManager(
        id="dnf",
        label="DNF (Fedora, RHEL, CentOS Stream, Rocky, AlmaLinux)",
        install=(("dnf", "install", "-y", "clamav", "clamd", "clamav-update", "clamav-filesystem"),),
        daemon_unit="clamd@scan",
        notes=(
            "The scanner runs as the clamd@scan unit on Fedora; scanning as an "
            "unprivileged user works through clamdscan --fdpass.",
        ),
    ),
    "zypper": PackageManager(
        id="zypper",
        label="Zypper (openSUSE, SLES)",
        install=(("zypper", "--non-interactive", "install", "clamav"),),
        daemon_unit="clamav-daemon",
    ),
    "pacman": PackageManager(
        id="pacman",
        label="Pacman (Arch, Manjaro, EndeavourOS)",
        refresh=(("pacman", "-Sy", "--noconfirm"),),
        install=(("pacman", "-S", "--noconfirm", "--needed", "clamav"),),
        daemon_unit="clamav-daemon",
    ),
    "apk": PackageManager(
        id="apk",
        label="APK (Alpine)",
        refresh=(("apk", "update"),),
        install=(("apk", "add", "clamav", "clamav-daemon", "clamav-libunrar"),),
        daemon_unit="clamav-daemon",
        service_manager="openrc",
    ),
    "xbps": PackageManager(
        id="xbps",
        label="XBPS (Void Linux)",
        refresh=(("xbps-install", "-S", "-y"),),
        install=(("xbps-install", "-y", "clamav"),),
        notes=(
            "Void runs runit: enable the daemon with "
            "'ln -s /etc/sv/clamd /var/service/' and 'ln -s /etc/sv/freshclam /var/service/'.",
        ),
    ),
    "eopkg": PackageManager(
        id="eopkg",
        label="eopkg (Solus)",
        install=(("eopkg", "install", "-y", "clamav"),),
    ),
    "emerge": PackageManager(
        id="emerge",
        label="Portage (Gentoo)",
        install=(("emerge", "--quiet", "app-antivirus/clamav"),),
        notes=("clamd and freshclam are started through the services in /etc/init.d.",),
    ),
    "brew": PackageManager(
        id="brew",
        label="Homebrew",
        install=(("brew", "install", "clamav"),),
        freshclam=False,
        notes=("Homebrew installs userspace binaries only; DCS will use clamscan.",),
    ),
}

#: checked in order: the first package manager that is installed wins
DETECTION_ORDER = ("apt", "dnf", "zypper", "pacman", "apk", "xbps", "eopkg", "emerge", "brew")

#: binary that has to exist for a manager to be usable
MANAGER_BINARY = {
    "apt": "apt-get",
    "dnf": "dnf",
    "zypper": "zypper",
    "pacman": "pacman",
    "apk": "apk",
    "xbps": "xbps-install",
    "eopkg": "eopkg",
    "emerge": "emerge",
    "brew": "brew",
}


@dataclass
class InstallStep:
    """One command of the plan, with a title for the progress display."""

    title: str
    argv: tuple[str, ...]
    optional: bool = False

    def as_command_line(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "command": self.as_command_line(),
            "optional": self.optional,
        }


@dataclass
class InstallPlan:
    """Everything that will happen, shown to the user before anything runs."""

    manager: PackageManager | None = None
    steps: list[InstallStep] = field(default_factory=list)
    method: str = ""            # pkexec | terminal | sudo | root | manual
    method_label: str = ""
    runner: list[str] = field(default_factory=list)
    script_path: str = ""
    script: str = ""
    distro: str = ""
    warnings: list[str] = field(default_factory=list)
    blocker: str = ""
    terminal: str = ""

    @property
    def possible(self) -> bool:
        return not self.blocker and bool(self.steps)

    def command_lines(self) -> list[str]:
        return [step.as_command_line() for step in self.steps]

    def to_dict(self) -> dict[str, Any]:
        return {
            "possible": self.possible,
            "distro": self.distro,
            "manager": self.manager.id if self.manager else "",
            "managerLabel": self.manager.label if self.manager else "",
            "method": self.method,
            "methodLabel": self.method_label,
            "runner": list(self.runner),
            "scriptPath": self.script_path,
            "script": self.script,
            "steps": [step.to_dict() for step in self.steps],
            "commands": self.command_lines(),
            "warnings": list(self.warnings),
            "blocker": self.blocker,
            "terminal": self.terminal,
            "needsPassword": self.method in ("pkexec", "terminal", "sudo"),
        }


@dataclass
class StepOutcome:
    index: int
    title: str
    code: int | None = None
    ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "title": self.title, "code": self.code, "ok": self.ok}


@dataclass
class InstallOutcome:
    """What actually happened."""

    ok: bool = False
    cancelled: bool = False
    started: bool = False
    exit_code: int | None = None
    failed_step: int = 0
    steps: list[StepOutcome] = field(default_factory=list)
    output: list[str] = field(default_factory=list)
    duration: float = 0.0
    error: str = ""
    verification: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "cancelled": self.cancelled,
            "started": self.started,
            "exitCode": self.exit_code,
            "failedStep": self.failed_step,
            "steps": [step.to_dict() for step in self.steps],
            "output": list(self.output),
            "duration": self.duration,
            "error": self.error,
            "verification": dict(self.verification),
        }


# ---------------------------------------------------------------------------
# environment detection
# ---------------------------------------------------------------------------


def distro_name() -> str:
    """A human readable distribution name from /etc/os-release."""
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def detect_package_manager(*, which: Callable[[str], str | None] | None = None) -> PackageManager | None:
    """Return the package manager to use, or None when there is none."""
    which = which or shutil.which
    for manager_id in DETECTION_ORDER:
        binary = MANAGER_BINARY[manager_id]
        if which(binary):
            return PACKAGE_MANAGERS[manager_id]
    return None


def running_as_root() -> bool:
    return os.geteuid() == 0


def polkit_agent_running() -> bool:
    """True when a graphical polkit agent can show a password dialog."""
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return False
    try:
        proc = subprocess.run(
            [pgrep, "-f", POLKIT_AGENT_PATTERN],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def find_terminal(*, which: Callable[[str], str | None] | None = None) -> tuple[str, tuple[str, ...]]:
    """The first terminal emulator that is installed, with its exec arguments."""
    which = which or shutil.which
    for name, args in TERMINALS:
        path = which(name)
        if path:
            return path, args
    return "", ()


def terminal_available() -> bool:
    return bool(find_terminal()[0])


# ---------------------------------------------------------------------------
# plan construction
# ---------------------------------------------------------------------------


def build_steps(manager: PackageManager, *, want_daemon: bool = True) -> list[InstallStep]:
    """The commands to run, in order, for one package manager."""
    steps: list[InstallStep] = []
    for index, argv in enumerate(manager.refresh):
        title = "Refresh the package index" if index == 0 else f"Refresh the package index ({index + 1})"
        steps.append(InstallStep(title, tuple(argv)))
    for index, argv in enumerate(manager.install):
        title = "Install ClamAV" if index == 0 else f"Install ClamAV ({index + 1})"
        steps.append(InstallStep(title, tuple(argv)))
    if manager.freshclam:
        steps.append(
            InstallStep(
                "Download the virus signatures",
                ("freshclam", "--quiet", "--no-warnings"),
                optional=True,
            )
        )
    if want_daemon and manager.updater_units and manager.service_manager == "systemd":
        steps.append(
            InstallStep(
                "Enable the signature updater",
                ("systemctl", "enable", "--now", *manager.updater_units),
                optional=True,
            )
        )
    if want_daemon and manager.daemon_unit:
        if manager.service_manager == "systemd":
            steps.append(
                InstallStep(
                    "Enable and start the ClamAV daemon",
                    ("systemctl", "enable", "--now", manager.daemon_unit),
                    optional=True,
                )
            )
        elif manager.service_manager == "openrc":
            steps.append(
                InstallStep(
                    "Enable the ClamAV daemon",
                    ("rc-update", "add", manager.daemon_unit, "default"),
                    optional=True,
                )
            )
            steps.append(
                InstallStep(
                    "Start the ClamAV daemon",
                    ("rc-service", manager.daemon_unit, "start"),
                    optional=True,
                )
            )
    return steps


def render_script(steps: Sequence[InstallStep], *, distro: str = "", manager: str = "") -> str:
    """Render the plan as a small shell script the user can read and keep."""
    lines = [
        "#!/bin/bash",
        "# Ducky Can Shoot - ClamAV installation plan",
        f"# generated {iso_now()} for {distro or 'this machine'}"
        + (f" ({manager})" if manager else ""),
        "#",
        "# The script is deliberately linear and contains nothing but the commands",
        "# below.  Read it, or run it yourself with:  sudo bash <this file>",
        "#",
        "# Every step prints a marker line so Ducky Can Shoot can follow the progress",
        "# in its own window:  === DCS step N: title ===",
        "",
        "# keep debconf from asking questions in a window nobody is watching",
        "export DEBIAN_FRONTEND=noninteractive",
        "",
        "step() {",
        '    local index="$1"; shift',
        '    local title="$1"; shift',
        '    printf "\\n=== DCS step %s: %s ===\\n" "$index" "$title"',
        '    "$@"',
        "    local rc=$?",
        '    printf "=== DCS step %s finished with exit %s ===\\n" "$index" "$rc"',
        '    return "$rc"',
        "}",
        "",
    ]
    for index, step in enumerate(steps, start=1):
        command = step.as_command_line()
        lines.append(f"# {step.title}")
        if step.optional:
            # an optional step must not abort the installation
            lines.append(f'step {index} "{step.title}" {command} || true')
        else:
            lines.append(f'step {index} "{step.title}" {command} || exit {STEP_FAILURE_BASE * index}')
        lines.append("")
    lines.append("exit 0")
    lines.append("")
    return "\n".join(lines)


def build_plan(
    *,
    config: Any = None,
    want_daemon: bool = True,
    prefer: str = "auto",
    cache_dir: str | None = None,
    write_script: bool = True,
) -> InstallPlan:
    """Work out how ClamAV would be installed on this machine.

    Nothing is executed here: the GUI shows the plan first.
    """
    plan = InstallPlan(distro=distro_name())
    manager = detect_package_manager()
    if manager is None:
        plan.blocker = (
            "no supported package manager was found (looked for "
            + ", ".join(MANAGER_BINARY[m] for m in DETECTION_ORDER)
            + ")"
        )
        plan.method = "manual"
        return plan

    plan.manager = manager
    plan.steps = build_steps(manager, want_daemon=want_daemon)
    plan.warnings.extend(manager.notes)

    if running_as_root():
        plan.method, plan.method_label = "root", "running as root, no password needed"
    elif prefer == "sudo" and shutil.which("sudo"):
        # the CLI case: sudo asking inside this terminal is what a shell user
        # expects, and it keeps the output in the same window
        plan.method, plan.method_label = "sudo", "sudo will ask for your password here"
        plan.runner = ["sudo"]
    elif prefer == "terminal" or (prefer == "auto" and not polkit_agent_running()):
        terminal, args = find_terminal()
        if terminal:
            plan.method, plan.method_label = "terminal", f"a terminal window will open and ask for your password ({os.path.basename(terminal)})"
            plan.terminal = terminal
            plan.runner = [terminal, *args]
        elif shutil.which("pkexec"):
            plan.method, plan.method_label = "pkexec", "a system password dialog will appear"
            plan.runner = ["pkexec"]
        else:
            plan.blocker = (
                "no graphical password dialog and no terminal emulator were found; "
                "copy the commands below and run them in a terminal yourself"
            )
            plan.method = "manual"
    else:
        if shutil.which("pkexec"):
            plan.method, plan.method_label = "pkexec", "a system password dialog will appear"
            plan.runner = ["pkexec"]
        else:
            terminal, args = find_terminal()
            if terminal:
                plan.method = "terminal", f"a terminal window will open and ask for your password ({os.path.basename(terminal)})"
                plan.terminal = terminal
                plan.runner = [terminal, *args]
            elif shutil.which("sudo"):
                plan.method, plan.method_label = "sudo", "sudo will ask for your password on the terminal"
                plan.runner = ["sudo"]
            else:
                plan.blocker = (
                    "neither pkexec nor sudo is available; run the commands below as root"
                )
                plan.method = "manual"

    plan.script = render_script(plan.steps, distro=plan.distro, manager=manager.id)
    if write_script and plan.steps:
        directory = cache_dir or os.path.join(
            os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"),
            "dcs",
        )
        path = os.path.join(directory, f"install-clamav-{time.strftime('%Y%m%d-%H%M%S')}.sh")
        try:
            os.makedirs(directory, exist_ok=True)
            atomic_write_text(path, plan.script, mode=0o700)
            plan.script_path = path
        except OSError as exc:
            log.warning("could not write the installation script: %s", exc)
            plan.warnings.append(f"the script could not be saved to {directory}: {exc}")
    plan.warnings = list(dict.fromkeys(plan.warnings))
    return plan


def manual_instructions(plan: InstallPlan) -> str:
    """Copy-paste instructions for a machine where DCS cannot help."""
    if not plan.steps:
        return "No installation commands are known for this system."
    lines = [f"# run these as root (on {plan.distro}):"]
    lines.extend(f"sudo {step.as_command_line()}" for step in plan.steps)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def _argv_for(plan: InstallPlan, script_path: str, *, python: str = "bash") -> list[str]:
    """The full command line that runs the generated script."""
    if plan.method == "root":
        return [python, script_path]
    if plan.method == "pkexec":
        return ["pkexec", "/bin/bash", script_path]
    if plan.method == "terminal":
        terminal, args = plan.terminal, tuple(plan.runner[1:])
        inner = f"sudo bash {shlex.quote(script_path)}; "
        inner += "printf '\\nPress Enter to close this window...'; read -r _"
        return [terminal, *args, "bash", "-c", inner]
    if plan.method == "sudo":
        return ["sudo", "bash", script_path]
    return []


class Installer:
    """Runs one installation plan, streaming its output."""

    def __init__(self, plan: InstallPlan, *, timeout: float = 3600.0) -> None:
        self.plan = plan
        self.timeout = timeout
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self, on_output: OutputFn | None = None) -> InstallOutcome:
        """Execute the plan; never raises for a failing command."""
        outcome = InstallOutcome()
        emit = on_output or (lambda line: None)
        started = time.monotonic()

        if not self.plan.possible:
            outcome.error = self.plan.blocker or "there is nothing to install on this system"
            emit(f"error: {outcome.error}")
            outcome.duration = time.monotonic() - started
            return outcome
        if not self.plan.script_path or not os.path.exists(self.plan.script_path):
            outcome.error = "the installation script could not be written"
            emit(f"error: {outcome.error}")
            outcome.duration = time.monotonic() - started
            return outcome

        argv = _argv_for(self.plan, self.plan.script_path)
        if not argv:
            outcome.error = self.plan.blocker or "no way to obtain root privileges was found"
            emit(f"error: {outcome.error}")
            outcome.duration = time.monotonic() - started
            return outcome

        emit(f"# {' '.join(shlex.quote(part) for part in argv)}")
        if self.plan.method in ("pkexec", "terminal"):
            emit(
                "# a password dialog is opening; Ducky Can Shoot never sees what you type"
                if self.plan.method == "pkexec"
                else "# a terminal window is opening; type your password there"
            )

        try:
            # A new session (process group) is used so cancelling stops the whole
            # tree: terminating bash alone would leave a package manager running.
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            outcome.error = f"cannot start {argv[0]}: {exc}"
            emit(f"error: {outcome.error}")
            outcome.duration = time.monotonic() - started
            return outcome

        outcome.started = True
        assert process.stdout is not None
        current: StepOutcome | None = None
        deadline = started + self.timeout

        # Output is drained on a helper thread so this loop stays responsive: a
        # quiet step (freshclam, a slow download) must remain cancellable without
        # waiting for the next line to arrive.
        queue: list[str] = []
        reading_done = threading.Event()

        def reader() -> None:
            try:
                for raw in process.stdout:  # type: ignore[union-attr]
                    queue.append(raw)
            finally:
                reading_done.set()

        threading.Thread(target=reader, daemon=True).start()

        def stop_process(*, force: bool = False) -> None:
            signature = signal.SIGKILL if force else signal.SIGTERM
            try:
                os.killpg(os.getpgid(process.pid), signature)
            except (ProcessLookupError, PermissionError, OSError):
                with contextlib.suppress(OSError):
                    process.kill()

        try:
            while True:
                if queue:
                    line = queue.pop(0).rstrip("\n")
                    emit(line)
                    outcome.output.append(line)
                    if len(outcome.output) > 20000:  # keep the window bounded
                        del outcome.output[:1000]
                    match = STEP_RE.match(line.strip())
                    if match:
                        current = StepOutcome(
                            index=int(match.group("index")), title=match.group("title")
                        )
                        outcome.steps.append(current)
                    done = STEP_DONE_RE.match(line.strip())
                    if done and current is not None and current.index == int(done.group("index")):
                        current.code = int(done.group("code"))
                        current.ok = current.code == 0
                    continue
                if process.poll() is not None and reading_done.is_set():
                    break
                if self._cancel.is_set():
                    stop_process()
                    break
                if time.monotonic() > deadline:
                    outcome.error = "the installation timed out"
                    emit(f"error: {outcome.error}")
                    stop_process()
                    break
                time.sleep(0.05)
        finally:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                stop_process(force=True)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=10)

        outcome.exit_code = process.returncode
        outcome.cancelled = self._cancel.is_set()
        outcome.duration = time.monotonic() - started
        failed = [step for step in outcome.steps if step.code not in (None, 0)]
        if failed:
            outcome.failed_step = failed[0].index
        if outcome.cancelled:
            outcome.error = outcome.error or "cancelled"
        elif outcome.exit_code == 0:
            outcome.ok = True
        elif not outcome.error:
            if outcome.failed_step:
                title = next(
                    (step.title for step in outcome.steps if step.index == outcome.failed_step),
                    "the command failed",
                )
                outcome.error = f"step {outcome.failed_step} failed ({title})"
            elif outcome.exit_code in (126, 127):
                outcome.error = (
                    "the password dialog was dismissed or permission was refused "
                    f"(exit code {outcome.exit_code})"
                )
            else:
                outcome.error = f"the installation script exited with code {outcome.exit_code}"
        return outcome


def verify(config: Any = None) -> dict[str, Any]:
    """Re-probe ClamAV after an installation attempt."""
    from dcs.core.config import ClamAVConfig
    from dcs.core.scanner.clamav import clamav_status_line, database_warning, probe

    clamav_config = ClamAVConfig()
    scan_config = getattr(config, "scan", None)  # AppConfig
    if scan_config is not None:
        clamav_config = getattr(scan_config, "clamav", clamav_config)
    elif config is not None and hasattr(config, "clamav"):
        clamav_config = config.clamav  # ScanConfig
    elif config is not None and hasattr(config, "clamdscan_binary"):
        clamav_config = config  # ClamAVConfig
    status = probe(clamav_config)
    result = {
        "available": status.usable,
        "mode": status.mode,
        "version": status.version,
        "problem": status.problem,
        "database": status.database.path,
        "databaseAgeDays": round(status.database.age_days, 1) if status.database.modified else None,
        "line": clamav_status_line(status),
    }
    warning = database_warning(clamav_config, status.database)
    if warning:
        result["warning"] = warning
    return result


def plan_summary(plan: InstallPlan) -> str:
    """A few lines the CLI and the GUI dialog both show."""
    lines = [f"distribution: {plan.distro}"]
    if plan.manager:
        lines.append(f"package manager: {plan.manager.label}")
    lines.append(f"privileges: {plan.method_label or plan.method or 'unknown'}")
    if plan.blocker:
        lines.append(f"blocked: {plan.blocker}")
    if plan.script_path:
        lines.append(f"script: {plan.script_path}")
    lines.append("steps:")
    for index, step in enumerate(plan.steps, start=1):
        suffix = "  (optional)" if step.optional else ""
        lines.append(f"  {index}. {step.title}: {step.as_command_line()}{suffix}")
    for warning in plan.warnings:
        lines.append(f"note: {warning}")
    return "\n".join(lines)
