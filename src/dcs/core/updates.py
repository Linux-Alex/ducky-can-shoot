"""Find a newer release on GitHub, and install it when the user asks.

The check is one read-only request to the release API.  Nothing is downloaded
and nothing changes until the user presses the button in the settings page (or
runs `dcs update` without `--check`).

Installing means different things depending on how this copy got here, so the
module works out what is running first:

* `deb`    - the distribution package from the release page.  Installing needs
              root, so the plan machinery of `dcs.core.installer` is reused
              (pkexec, a terminal with sudo, or plain sudo on a CLI).
* `venv`   - the launcher virtualenv created by `scripts/install.sh`.  A wheel
              goes into it with pip; no privileges are needed.
* `pip`    - a user site installation of the same wheel.
* `source` - a git checkout.  There is nothing to install: `git pull` is the
              update, and saying so is more useful than pretending otherwise.

Only a newer release is ever offered, and a pre-release never is: the API's
`releases/latest` endpoint already excludes drafts and pre-releases.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dcs.core import installer
from dcs.core.config import UpdateConfig, xdg_cache_home
from dcs.core.log import get_logger
from dcs.core.util import human_bytes, iso_now
from dcs.version import APP_HOMEPAGE, APP_NAME, __version__

log = get_logger("updates")

#: how long one HTTP request may take before it is given up on
DEFAULT_TIMEOUT = 15.0

#: the file names the release workflow attaches, by install kind
ASSET_SUFFIX = {"deb": ".deb", "venv": ".whl", "pip": ".whl"}


class UpdateError(Exception):
    """The release could not be read (network, rate limit, bad answer)."""


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Turn "v0.2.2" or "0.2.2" into (0, 2, 2); None when it is not a version."""
    cleaned = str(text or "").strip().lstrip("vV")
    parts = cleaned.split(".")
    if len(parts) < 2 or len(parts) > 3:
        return None
    numbers: list[int] = []
    for part in parts:
        digits = ""
        for char in part:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return None
        numbers.append(int(digits))
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:3])  # type: ignore[return-value]


def is_newer(candidate: str, current: str) -> bool:
    """True when *candidate* is a strictly newer release than *current*."""
    newer, older = parse_version(candidate), parse_version(current)
    if newer is None or older is None:
        return False
    return newer > older


# ---------------------------------------------------------------------------
# the release API
# ---------------------------------------------------------------------------


def repository() -> str:
    """owner/name, taken from the project homepage so it is written once."""
    path = APP_HOMEPAGE.rstrip("/").rsplit("/", 2)
    if len(path) == 3 and path[-2] and path[-1]:
        return f"{path[-2]}/{path[-1]}"
    return "Linux-Alex/ducky-can-shoot"


def api_url(rest: str = "releases/latest") -> str:
    return f"https://api.github.com/repos/{repository()}/{rest.lstrip('/')}"


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, bytes]:
    """One request, returning (status, body).  Tests replace this."""
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"{url}: {exc}") from exc


def headers() -> dict[str, str]:
    return {
        "accept": "application/vnd.github+json",
        "user-agent": f"{APP_NAME}/{__version__}",
    }


class Asset:
    """One file attached to a release."""

    __slots__ = ("name", "size", "url")

    def __init__(self, name: str, url: str, size: int = 0) -> None:
        self.name = name
        self.url = url
        self.size = size

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url, "size": self.size,
                "sizeText": human_bytes(self.size) if self.size else ""}


class Release:
    """The newest published release, as far as DCS cares."""

    __slots__ = ("assets", "body", "name", "published_at", "tag", "url", "version")

    def __init__(
        self,
        *,
        version: str,
        tag: str = "",
        name: str = "",
        url: str = "",
        published_at: str = "",
        body: str = "",
        assets: list[Asset] | None = None,
    ) -> None:
        self.version = version
        self.tag = tag or version
        self.name = name
        self.url = url
        self.published_at = published_at
        self.body = body
        self.assets = assets or []

    def asset_for(self, kind: str) -> Asset | None:
        suffix = ASSET_SUFFIX.get(kind, "")
        if not suffix:
            return None
        for asset in self.assets:
            if asset.name.endswith(suffix):
                return asset
        return None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Release:
        tag = str(payload.get("tag_name") or "")
        assets = [
            Asset(
                name=str(item.get("name") or ""),
                url=str(item.get("browser_download_url") or ""),
                size=int(item.get("size") or 0),
            )
            for item in payload.get("assets") or []
            if isinstance(item, dict)
        ]
        return cls(
            version=tag.lstrip("vV"),
            tag=tag,
            name=str(payload.get("name") or tag),
            url=str(payload.get("html_url") or ""),
            published_at=str(payload.get("published_at") or ""),
            body=str(payload.get("body") or ""),
            assets=assets,
        )


def fetch_latest(*, timeout: float = DEFAULT_TIMEOUT) -> Release:
    """The newest published release.  Raises UpdateError when it cannot be read."""
    status, body = http_json("GET", api_url(), headers=headers(), timeout=timeout)
    if status == 404:
        raise UpdateError("the repository has no published release yet")
    if status in (403, 429):
        raise UpdateError(
            "GitHub declined the request (rate limit); try again in a few minutes"
        )
    if status >= 400:
        raise UpdateError(f"GitHub answered HTTP {status}")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise UpdateError(f"GitHub sent something that is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("tag_name"):
        raise UpdateError("GitHub sent an unexpected payload")
    if payload.get("draft") or payload.get("prerelease"):
        raise UpdateError("the newest release is a draft or a pre-release")
    release = Release.from_payload(payload)
    if parse_version(release.version) is None:
        raise UpdateError(f"the release tag {release.tag!r} is not a version")
    return release


# ---------------------------------------------------------------------------
# what is installed here
# ---------------------------------------------------------------------------


class InstallTarget:
    """How this copy of DCS is installed, and what an update looks like."""

    __slots__ = ("detail", "kind", "needs_root", "path")

    def __init__(self, kind: str, detail: str = "", *, needs_root: bool = False, path: str = "") -> None:
        self.kind = kind
        self.detail = detail
        self.needs_root = needs_root
        self.path = path

    @property
    def can_install(self) -> bool:
        return self.kind in ASSET_SUFFIX

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "needsRoot": self.needs_root,
            "canInstall": self.can_install,
        }


def _package_file() -> Path:
    import dcs

    return Path(dcs.__file__).resolve()


def _checkout_root(package: Path) -> Path | None:
    """The git checkout this package was imported from, if it was one."""
    for parent in package.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "dcs").is_dir():
            return parent
    return None


def installed_deb_version() -> str:
    """The version of the ducky-can-shoot package, or "" when it is not installed."""
    if shutil.which("dpkg-query") is None:
        return ""
    try:
        proc = subprocess.run(
            ["dpkg-query", "-W", "-f=" + chr(36) + "{Version}", "ducky-can-shoot"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def detect_install(*, package: Path | None = None) -> InstallTarget:
    """Work out how the running copy was installed."""
    package = package or _package_file()
    root = _checkout_root(package)
    if root is not None:
        return InstallTarget(
            "source",
            f"git checkout at {root}",
            path=str(root),
        )
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return InstallTarget(
            "venv",
            f"virtual environment at {sys.prefix}",
            path=sys.prefix,
        )
    deb_version = installed_deb_version()
    if deb_version:
        return InstallTarget(
            "deb",
            f"system package {deb_version}",
            needs_root=True,
            path=deb_version,
        )
    if "site-packages" in str(package) or "dist-packages" in str(package):
        return InstallTarget("pip", f"Python installation at {sysconfig.get_path('purelib')}")
    return InstallTarget("unknown", f"package at {package}")


def other_installs(target: InstallTarget) -> list[str]:
    """Copies of DCS that exist besides the running one, as short sentences."""
    found: list[str] = []
    if target.kind != "deb":
        deb_version = installed_deb_version()
        if deb_version:
            found.append(f"system package {deb_version}")
    launcher = Path.home() / ".local" / "share" / "dcs" / "venv"
    if target.kind != "venv" and (launcher / "bin" / "dcs").exists():
        version_file = launcher / "lib"
        versions = sorted(version_file.glob("python*/site-packages/dcs/version.py")) if version_file.exists() else []
        found.append(
            f"launcher virtualenv at {launcher}"
            + (f" ({_read_version(versions[0])})" if versions else "")
        )
    return found


def _read_version(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


class UpdateCheck:
    """Everything the interface needs to describe one check."""

    __slots__ = (
        "asset",
        "checked_at",
        "current",
        "error",
        "latest",
        "newer",
        "other",
        "release",
        "target",
    )

    def __init__(
        self,
        *,
        current: str = __version__,
        release: Release | None = None,
        target: InstallTarget | None = None,
        other: list[str] | None = None,
        error: str = "",
        checked_at: str = "",
    ) -> None:
        self.current = current
        self.release = release
        self.latest = release.version if release else ""
        self.newer = bool(release and is_newer(release.version, current))
        self.target = target or detect_install()
        self.asset = self.release.asset_for(self.target.kind) if self.release else None
        self.other = other or []
        self.error = error
        self.checked_at = checked_at or iso_now()

    @property
    def can_install(self) -> bool:
        return bool(self.newer and self.release and self.asset and self.target.can_install)

    @property
    def message(self) -> str:
        if self.error:
            return self.error
        if not self.release:
            return "No release information was found."
        if not self.newer:
            return f"You are running the newest release ({self.current})."
        if not self.target.can_install:
            if self.target.kind == "source":
                return (
                    f"{self.latest} is available. This is a git checkout: run "
                    f"git pull in {self.target.path or 'the checkout'} to update it."
                )
            return (
                f"{self.latest} is available, but DCS does not know how to update "
                f"a {self.target.kind} installation by itself."
            )
        if not self.asset:
            return (
                f"{self.latest} is available, but this release has no package for "
                f"a {self.target.kind} installation."
            )
        return f"{self.latest} is available (you have {self.current})."

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "newer": self.newer,
            "canInstall": self.can_install,
            "releaseUrl": self.release.url if self.release else "",
            "releaseName": self.release.name if self.release else "",
            "publishedAt": self.release.published_at if self.release else "",
            "asset": self.asset.to_dict() if self.asset else {},
            "install": self.target.to_dict(),
            "otherInstalls": list(self.other),
            "error": self.error,
            "message": self.message,
            "checkedAt": self.checked_at,
        }


def check_for_update(
    *,
    current: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    release: Release | None = None,
) -> UpdateCheck:
    """Ask GitHub for the newest release and compare it with this copy."""
    target = detect_install()
    try:
        latest = release if release is not None else fetch_latest(timeout=timeout)
    except UpdateError as exc:
        return UpdateCheck(current=current or __version__, target=target, error=str(exc))
    return UpdateCheck(
        current=current or __version__,
        release=latest,
        target=target,
        other=other_installs(target),
    )


# ---------------------------------------------------------------------------
# remembering the last check, so a restart does not ask GitHub again
# ---------------------------------------------------------------------------


def state_path() -> Path:
    return xdg_cache_home() / "dcs" / "update-check.json"


def read_state() -> dict[str, Any]:
    try:
        payload = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_state(check: UpdateCheck) -> None:
    state = {
        "checked": time.time(),
        "checkedAt": check.checked_at,
        "current": check.current,
        "latest": check.latest,
        "newer": check.newer,
        "releaseUrl": check.release.url if check.release else "",
        "error": check.error,
    }
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:  # a cache that cannot be written is not a failure
        log.debug("cannot write the update state: %s", exc)


def auto_check_due(config: UpdateConfig, *, now: float | None = None) -> bool:
    """True when the interface should check on start."""
    if not config.enabled or not config.check_on_start:
        return False
    state = read_state()
    last = float(state.get("checked") or 0)
    if not last:
        return True
    interval = max(0, int(config.check_interval_hours)) * 3600
    if interval == 0:
        return True
    return (now if now is not None else time.time()) - last >= interval


# ---------------------------------------------------------------------------
# download and install
# ---------------------------------------------------------------------------


def http_stream(url: str, *, timeout: float = 300.0) -> Any:
    """Open a download stream.  Tests replace this."""
    request = urllib.request.Request(url, headers={"user-agent": headers()["user-agent"]})
    return urllib.request.urlopen(request, timeout=timeout)


def download_dir() -> Path:
    """Where release packages are downloaded to (removed again by hand)."""
    return xdg_cache_home() / "dcs" / "downloads"


def download(
    asset: Asset,
    directory: Path,
    *,
    on_progress: Any = None,
    timeout: float = 300.0,
) -> Path:
    """Fetch one asset into *directory* and return the file."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (asset.name or "dcs-release-artifact")
    try:
        with http_stream(asset.url, timeout=timeout) as response, open(target, "wb") as handle:
            total = int(response.headers.get("content-length") or asset.size or 0)
            received = 0
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                handle.write(block)
                received += len(block)
                if on_progress is not None:
                    on_progress(received, total)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise UpdateError(f"could not download {asset.name}: {exc}") from exc
    return target


class InstallResult:
    """What an installation attempt did."""

    __slots__ = ("cancelled", "message", "needs_restart", "ok")

    def __init__(self, ok: bool, message: str = "", *, cancelled: bool = False, needs_restart: bool = False) -> None:
        self.ok = ok
        self.message = message
        self.cancelled = cancelled
        self.needs_restart = needs_restart

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "cancelled": self.cancelled,
            "needsRestart": self.needs_restart,
        }


def build_deb_plan(package: Path, *, prefer: str = "auto") -> installer.InstallPlan:
    """The plan that installs a release .deb, shown before anything runs."""
    plan = installer.InstallPlan(distro=installer.distro_name())
    manager = installer.detect_package_manager()
    if manager is None:
        plan.blocker = "no supported package manager was found; install the .deb by hand"
        plan.method = "manual"
        return plan
    if manager.id != "apt":
        plan.blocker = (
            f"the release ships a .deb and this machine uses {manager.id}; "
            "install the package by hand"
        )
        plan.method = "manual"
        return plan
    plan.manager = manager
    plan.steps = [
        installer.InstallStep(
            title=f"install {package.name}",
            argv=("apt", "install", "-y", str(package)),
        )
    ]
    installer.choose_method(plan, prefer)
    plan.script = installer.render_script(
        plan.steps, distro=plan.distro, manager=manager.id, title="update installation plan"
    )
    return plan


def write_plan_script(plan: installer.InstallPlan, directory: Path | None = None) -> installer.InstallPlan:
    """Save the plan as a script the user could read (mode 0700)."""
    if not plan.steps or plan.script_path:
        return plan
    folder = directory or (xdg_cache_home() / "dcs")
    path = folder / f"update-{time.strftime('%Y%m%d-%H%M%S')}.sh"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.script, encoding="utf-8")
        path.chmod(0o700)
        plan.script_path = str(path)
    except OSError as exc:
        plan.warnings.append(f"the script could not be saved to {folder}: {exc}")
    return plan


def install_deb(package: Path, *, on_output: Any = None, prefer: str = "auto") -> InstallResult:
    """Install a .deb through the shared privilege machinery."""
    plan = write_plan_script(build_deb_plan(package, prefer=prefer))
    if not plan.possible:
        return InstallResult(False, plan.blocker or "the package cannot be installed here")
    outcome = installer.Installer(plan).run(on_output)
    if outcome.cancelled:
        return InstallResult(False, "the installation was cancelled", cancelled=True)
    if not outcome.ok:
        return InstallResult(False, outcome.error or "the package manager reported a failure")
    return InstallResult(True, f"installed {package.name}", needs_restart=True)


def install_wheel(
    package: Path,
    *,
    python: str | None = None,
    on_output: Any = None,
    timeout: float = 900.0,
) -> InstallResult:
    """Install a wheel into the interpreter that is running now."""
    interpreter = python or sys.executable
    argv = [interpreter, "-m", "pip", "install", "--quiet", "--upgrade", str(package)]
    emit = on_output or (lambda line: None)
    emit("# " + " ".join(argv))
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        return InstallResult(False, f"cannot run pip: {exc}")
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if line:
            emit(line)
        if time.monotonic() > deadline:
            proc.kill()
            return InstallResult(False, "pip did not finish in time")
    proc.wait(timeout=30)
    if proc.returncode != 0:
        return InstallResult(False, f"pip exited with code {proc.returncode}")
    return InstallResult(True, f"installed {package.name}", needs_restart=True)


def install_release(
    check: UpdateCheck,
    package: Path,
    *,
    on_output: Any = None,
    prefer: str = "auto",
) -> InstallResult:
    """Install the downloaded package for this kind of installation."""
    if check.target.kind == "deb":
        return install_deb(package, on_output=on_output, prefer=prefer)
    if check.target.kind in ("venv", "pip"):
        return install_wheel(package, on_output=on_output)
    return InstallResult(
        False,
        f"this is a {check.target.kind} installation; DCS cannot update it in place",
    )


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


def restart_argv() -> list[str]:
    """The command line that starts this application again."""
    argv = list(getattr(sys, "argv", []))
    first = argv[0] if argv else ""
    if first and os.path.isfile(first) and os.access(first, os.X_OK):
        return [first, *argv[1:]]
    return [sys.executable, "-m", "dcs", *(argv[1:] or ["gui"])]


def restart_watcher() -> str:
    """Restart the background USB watcher, so it runs the new code too."""
    from dcs.core import service

    try:
        service.stop()
    except Exception as exc:  # pragma: no cover - a watcher that would not stop
        log.warning("cannot stop the watcher: %s", exc)
    started, message = service.start()
    return message or ("started" if started else "not started")
