"""Known-hash intelligence: local block lists, and optional reputation lookups.

The shipped hash list is already applied by the heuristic engine, so this
engine adds two things on top of it: extra "sha256 <name>" lists the user keeps
(an exported MalwareBazaar dump, an internal block list) and, when they opt in,
a "has anybody seen this file?" lookup against VirusTotal and MetaDefender
Cloud.

Privacy is the whole point of the mode switch:

* `off`    - nothing leaves the machine.
* `hash`   - the SHA-256 of each unknown file is looked up.  No file content
              is sent anywhere.
* `upload` - unknown files are also sent to the provider.  That needs a second
              switch (`upload_unknowns = true`) and every uploaded path ends up
              in the scan warnings, so the report says what left the machine.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dcs.core.config import IntelConfig, resolve_secret, xdg_cache_home
from dcs.core.events import Finding, Severity
from dcs.core.log import get_logger
from dcs.core.scanner.base import EngineStatus, ScanContext
from dcs.core.scanner.heuristic import load_hash_db
from dcs.core.util import iso_now, sha256_file, walk_files

log = get_logger("scan.intel")

VIRUSTOTAL_API = "https://www.virustotal.com/api/v3"
METADEFENDER_API = "https://api.metadefender.com/v4"

#: provider ids this module can talk to
PROVIDERS = ("virustotal", "metadefender")


class ProviderError(Exception):
    """A provider could not be reached, or refused to answer."""


class ProviderBusyError(ProviderError):
    """Rate limited (HTTP 429): stop asking for a while."""


class ProviderAuthError(ProviderError):
    """The API key is missing, wrong or out of quota."""


# ---------------------------------------------------------------------------
# answers and their cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Answer:
    """What one provider knows about one hash."""

    provider: str
    found: bool = False
    detections: int = 0
    total: int = 0
    names: list[str] = field(default_factory=list)
    link: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Answer:
        known = set(cls.__slots__)
        return cls(**{key: value for key, value in data.items() if key in known})


class AnswerCache:
    """Answers kept between runs, so a stick can be re-scanned for free.

    The providers rate limit aggressively and the same files show up again and
    again on a stick that is scanned twice, so an answer that is a week old is
    worth far more than no answer at all.
    """

    VERSION = 1
    #: keep the cache from growing without a bound on a media library
    MAX_ENTRIES = 20000

    def __init__(self, directory: str | Path, ttl_hours: int) -> None:
        self.directory = Path(directory).expanduser()
        self.path = self.directory / "answers.json"
        self.ttl = max(1, int(ttl_hours)) * 3600
        self.entries: dict[str, dict[str, Any]] = {}
        self.dirty = False
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if payload.get("version") != self.VERSION:
            return
        entries = payload.get("entries")
        if isinstance(entries, dict):
            self.entries = {
                key: value for key, value in entries.items() if isinstance(value, dict)
            }

    def get(self, digest: str) -> list[Answer] | None:
        entry = self.entries.get(digest)
        if not entry:
            return None
        if time.time() - float(entry.get("checked") or 0) > self.ttl:
            return None
        answers = entry.get("answers")
        if not isinstance(answers, list):
            return None
        return [Answer.from_dict(item) for item in answers if isinstance(item, dict)]

    def put(self, digest: str, answers: Sequence[Answer]) -> None:
        self.entries[digest] = {
            "checked": time.time(),
            "checkedAt": iso_now(),
            "answers": [answer.to_dict() for answer in answers],
        }
        self.dirty = True
        if len(self.entries) > self.MAX_ENTRIES:
            oldest = sorted(self.entries, key=lambda key: self.entries[key].get("checked", 0))
            for key in oldest[: len(self.entries) - self.MAX_ENTRIES]:
                self.entries.pop(key, None)

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            payload = {"version": self.VERSION, "entries": self.entries}
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self.path)
            self.dirty = False
        except OSError as exc:  # a cache that cannot be written is not a failure
            log.warning("cannot write the intelligence cache: %s", exc)


# ---------------------------------------------------------------------------
# the providers
# ---------------------------------------------------------------------------


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 60.0,
) -> tuple[int, bytes]:
    """One HTTP request, returning (status, body).  Tests replace this."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:  # a 404 and a 429 are answers too
        return int(exc.code), exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"{url}: {exc}") from exc


def _multipart(field_name: str, path: str) -> tuple[bytes, str]:
    """Build a one-file multipart body (urllib has no helper for this)."""
    boundary = "----dcs" + os.urandom(12).hex()
    filename = os.path.basename(path)
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    with open(path, "rb") as handle:
        return head + handle.read() + tail, f"multipart/form-data; boundary={boundary}"


class VirusTotal:
    """VirusTotal API v3: hash lookups, and file uploads when asked to."""

    name = "virustotal"
    label = "VirusTotal"

    def __init__(self, api_key: str, *, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"x-apikey": self.api_key, "accept": "application/json"}

    def lookup(self, digest: str) -> Answer | None:
        status, body = http_json(
            "GET", f"{VIRUSTOTAL_API}/files/{digest}", headers=self._headers(), timeout=self.timeout
        )
        if status == 404:
            return None
        return self._answer(status, body)

    def upload(self, path: str) -> str:
        """Upload a file and return the analysis id that follows it."""
        body, content_type = _multipart("file", path)
        headers = self._headers()
        headers["content-type"] = content_type
        status, response = http_json(
            "POST", f"{VIRUSTOTAL_API}/files", headers=headers, body=body, timeout=self.timeout
        )
        payload = self._payload(status, response)
        analysis = payload.get("data") if isinstance(payload, dict) else None
        analysis_id = analysis.get("id") if isinstance(analysis, dict) else None
        if not analysis_id:
            raise ProviderError("VirusTotal did not return an analysis id")
        return str(analysis_id)

    def analysis(self, analysis_id: str, *, deadline: float, poll_s: float = 15.0) -> Answer:
        """Poll an analysis until it is finished, then report it."""
        while True:
            status, body = http_json(
                "GET",
                f"{VIRUSTOTAL_API}/analyses/{analysis_id}",
                headers=self._headers(),
                timeout=self.timeout,
            )
            payload = self._payload(status, body)
            attributes = (payload.get("data") or {}).get("attributes") or {}
            if str(attributes.get("status", "")).lower() == "completed":
                return self._stats(attributes)
            if time.monotonic() > deadline:
                raise ProviderError("VirusTotal did not finish the analysis in time")
            time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))

    def _payload(self, status: int, body: bytes) -> dict[str, Any]:
        if status == 401 or status == 403:
            raise ProviderAuthError(f"VirusTotal refused the API key (HTTP {status})")
        if status == 429:
            raise ProviderBusyError("VirusTotal rate limit reached (HTTP 429)")
        if status >= 400:
            raise ProviderError(f"VirusTotal answered HTTP {status}")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ProviderError(f"VirusTotal sent something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProviderError("VirusTotal sent an unexpected payload")
        return payload

    def _answer(self, status: int, body: bytes) -> Answer:
        payload = self._payload(status, body)
        attributes = (payload.get("data") or {}).get("attributes") or {}
        return self._stats(attributes)

    def _stats(self, attributes: dict[str, Any]) -> Answer:
        stats = attributes.get("last_analysis_stats") or {}
        detections = 0
        total = 0
        if isinstance(stats, dict):
            for key, value in stats.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    total += value
                    if key in ("malicious", "suspicious"):
                        detections += value
        names: list[str] = []
        results = attributes.get("last_analysis_results") or {}
        if isinstance(results, dict):
            for result in results.values():
                if not isinstance(result, dict):
                    continue
                if result.get("category") in ("malicious", "suspicious") and result.get("result"):
                    name = str(result["result"]).strip()
                    if name and name not in names:
                        names.append(name)
        return Answer(
            provider=self.name,
            found=True,
            detections=detections,
            total=total,
            names=names[:5],
            link=f"https://www.virustotal.com/gui/file/{attributes.get('sha256', '')}",
        )


class MetaDefender:
    """MetaDefender Cloud API v4: hash lookups across dozens of engines."""

    name = "metadefender"
    label = "MetaDefender Cloud"

    def __init__(self, api_key: str, *, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def lookup(self, digest: str) -> Answer | None:
        status, body = http_json(
            "GET",
            f"{METADEFENDER_API}/hash/{digest}",
            headers={"apikey": self.api_key, "accept": "application/json"},
            timeout=self.timeout,
        )
        if status == 404:
            return None
        if status in (401, 403):
            raise ProviderAuthError(f"MetaDefender refused the API key (HTTP {status})")
        if status == 429:
            raise ProviderBusyError("MetaDefender rate limit reached (HTTP 429)")
        if status >= 400:
            raise ProviderError(f"MetaDefender answered HTTP {status}")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ProviderError(f"MetaDefender sent something that is not JSON: {exc}") from exc
        results = (payload.get("scan_results") or {}) if isinstance(payload, dict) else {}
        detected = int(results.get("total_detected_avs") or 0)
        total = int(results.get("total_avs") or 0)
        names: list[str] = []
        details = results.get("scan_details") or {}
        if isinstance(details, dict):
            for engine_result in details.values():
                if not isinstance(engine_result, dict):
                    continue
                threat = str(engine_result.get("threat_found") or "").strip()
                if threat and threat not in names:
                    names.append(threat)
        return Answer(
            provider=self.name,
            found=True,
            detections=detected,
            total=total,
            names=names[:5],
            link=f"https://metadefender.opswat.com/results/file/{digest}/hash/overview",
        )


def build_providers(config: IntelConfig) -> tuple[list[Any], list[str]]:
    """The providers that have a key, plus what stopped the others."""
    warnings: list[str] = []
    if config.lookup == "off":
        return [], warnings
    providers: list[Any] = []
    virustotal = resolve_secret(
        config.virustotal_api_key,
        config.virustotal_api_key_command,
        "DCS_VIRUSTOTAL_API_KEY",
    )
    metadefender = resolve_secret(
        config.metadefender_api_key,
        config.metadefender_api_key_command,
        "DCS_METADEFENDER_API_KEY",
    )
    if virustotal:
        providers.append(VirusTotal(virustotal, timeout=config.timeout_s))
    else:
        warnings.append(
            "no VirusTotal API key: set scan.intel.virustotal_api_key, "
            "scan.intel.virustotal_api_key_command or DCS_VIRUSTOTAL_API_KEY"
        )
    if metadefender:
        providers.append(MetaDefender(metadefender, timeout=config.timeout_s))
    else:
        warnings.append(
            "no MetaDefender API key: set scan.intel.metadefender_api_key, "
            "scan.intel.metadefender_api_key_command or DCS_METADEFENDER_API_KEY"
        )
    return providers, warnings


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------


class _Budget:
    """How many lookups are left, and how fast they may be asked for."""

    def __init__(self, max_lookups: int, per_minute: int) -> None:
        #: None = no limit
        self.remaining: int | None = max_lookups if max_lookups > 0 else None
        self.interval = 60.0 / max(1, per_minute)
        self._next_at = 0.0

    def available(self) -> bool:
        return self.remaining is None or self.remaining > 0

    def take(self, cancel: Any) -> bool:
        """Wait for the next slot.  False when cancelled or out of budget."""
        if not self.available():
            return False
        while True:
            now = time.monotonic()
            if now >= self._next_at:
                break
            if getattr(cancel, "is_set", bool)():
                return False
            time.sleep(min(0.2, self._next_at - now))
        if self.remaining is not None:
            self.remaining -= 1
        self._next_at = time.monotonic() + self.interval
        return True


class IntelEngine:
    """Hash block lists plus reputation lookups; see the module docstring."""

    engine_id = "intel"
    label = "Hash intelligence"

    def __init__(
        self,
        config: IntelConfig,
        *,
        follow_symlinks: bool = False,
        include_hidden: bool = True,
        exclude_globs: Sequence[str] = (),
        max_file_size_mb: int = 0,
    ) -> None:
        self.config = config
        self.follow_symlinks = follow_symlinks
        self.include_hidden = include_hidden
        self.exclude_globs = tuple(exclude_globs)
        self.max_file_size_mb = max_file_size_mb
        self.errors: list[str] = []
        #: things the report has to say, e.g. which files were uploaded
        self.notes: list[str] = []
        self.local: dict[str, str] = {}
        self.sources: list[str] = []
        self.lookups = 0
        self.hashed = 0
        self.skipped_large = 0
        self._uploaded: list[dict[str, str]] = []
        self._load_local()
        self.providers, self.provider_warnings = build_providers(config)
        self.cache = AnswerCache(self._cache_dir(), config.cache_ttl_hours)
        self.budget = _Budget(config.max_lookups, config.rate_per_minute)
        self._dead_providers: set[str] = set()

    # -- setup ------------------------------------------------------------
    def _cache_dir(self) -> Path:
        if self.config.cache_dir:
            return Path(self.config.cache_dir).expanduser()
        return xdg_cache_home() / "dcs" / "intel"

    def _load_local(self) -> None:
        # the shipped list is shared with the heuristic engine on purpose: its
        # finding text is the same, so the scanner's de-duplication collapses
        # the two into one finding when both engines are on
        self.local = load_hash_db()
        if self.local:
            self.sources.append("dcs/data/hashes.txt")
        for extra in self.config.hash_files:
            path = Path(extra).expanduser()
            try:
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for raw in handle:
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        digest, _, name = line.partition(" ")
                        digest = digest.strip().lower()
                        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                            self.local[digest] = name.strip() or "known malware"
            except OSError as exc:
                message = f"cannot read the hash list {path}: {exc}"
                self.errors.append(message)
            else:
                self.sources.append(str(path))

    # -- status -----------------------------------------------------------
    def status(self) -> EngineStatus:
        warnings: list[str] = []
        if self.config.lookup != "off":
            warnings.extend(self.provider_warnings)
            if self.providers and not self.config.upload_unknowns:
                warnings.append(
                    "lookups are hash-only: no file content leaves the machine "
                    f"({', '.join(provider.label for provider in self.providers)})"
                )
        if self.config.upload_unknowns and self.config.lookup == "upload":
            warnings.append(
                "upload mode is on: unknown files are sent to the provider, "
                "which publishes its analysis to other users of that service"
            )
        if not self.local:
            warnings.append("no known-hash list could be read")
        detail = (
            f"{len(self.local)} known hashes from {len(self.sources)} list(s); "
            f"lookups: {self.config.lookup}"
        )
        if self.config.lookup != "off" and not self.providers:
            detail += " (no provider has an API key)"
        return EngineStatus(
            engine_id=self.engine_id,
            label=self.label,
            available=bool(self.local) or bool(self.providers),
            version="",
            detail=detail,
            warnings=warnings,
        )

    # -- the pass ---------------------------------------------------------
    def scan_tree(self, root: str, ctx: ScanContext) -> Iterator[Finding]:
        max_bytes = self.config.max_hash_size_mb * 1024 * 1024 if self.config.max_hash_size_mb else 0
        try:
            for entry in walk_files(
                root,
                follow_symlinks=self.follow_symlinks,
                include_hidden=self.include_hidden,
                exclude_globs=self.exclude_globs,
            ):
                if ctx.cancelled:
                    return
                if entry.is_special:
                    continue
                if max_bytes and entry.size > max_bytes:
                    self.skipped_large += 1
                    continue
                yield from self._analyse(entry.path, entry.size, ctx)
                if self.hashed and self.hashed % 500 == 0:
                    ctx.log(
                        f"hash intelligence: {self.hashed} files hashed, "
                        f"{self.lookups} lookups",
                        "debug",
                    )
        finally:
            self.cache.save()
            if self.uploaded_count():
                self.notes.append(
                    f"uploaded {self.uploaded_count()} file(s) for analysis: "
                    + ", ".join(self.uploaded_paths()[:5])
                    + (" …" if self.uploaded_count() > 5 else "")
                )

    def uploaded_paths(self) -> list[str]:
        return [item.get("path", "") for item in self._uploaded]

    def uploaded_count(self) -> int:
        return len(self._uploaded)

    # -- one file ---------------------------------------------------------
    def _analyse(self, path: str, size: int, ctx: ScanContext) -> Iterator[Finding]:
        try:
            digest = sha256_file(path)
        except OSError as exc:
            ctx.log(f"hash intelligence could not read {path}: {exc}", "warning")
            return
        self.hashed += 1

        known = self.local.get(digest)
        if known:
            yield self._local_finding(path, size, digest, known)
            return

        if self.config.lookup == "off" or not self.providers:
            return

        answers = self.cache.get(digest)
        if answers is None:
            answers = self._lookup(digest, ctx)
            if answers is None:  # provider trouble: try the next file
                return
            self.cache.put(digest, answers)

        detections = sum(answer.detections for answer in answers)
        if detections:
            yield self._reputation_finding(path, size, digest, answers)
            return

        # a cache entry (even an empty one) means somebody already answered, so
        # the file is not uploaded twice - and a clean upload is remembered as a
        # clean answer, since re-publishing it would be pointless and rude
        if self.config.lookup == "upload" and self.config.upload_unknowns and not answers:
            uploaded = self._upload(path, size, digest, ctx)
            if uploaded is not None:
                yield uploaded

    def _local_finding(self, path: str, size: int, digest: str, name: str) -> Finding:
        return Finding(
            path=path,
            threat=f"known malware hash: {name}",
            engine=self.engine_id,
            severity=Severity.CRITICAL,
            category="blocklist",
            size=size,
            sha256=digest,
            detail="the file's SHA-256 is on a known-malware list ("
            + ", ".join(self.sources or ["dcs/data/hashes.txt"])
            + ")",
        )

    def _reputation_finding(
        self, path: str, size: int, digest: str, answers: Sequence[Answer]
    ) -> Finding:
        flagged = [answer for answer in answers if answer.detections]
        best = max(flagged, key=lambda answer: answer.detections)
        names = [name for answer in flagged for name in answer.names]
        threat = names[0] if names else f"{best.detections} engine(s) flagged this file"
        detail_parts = [
            f"{answer.provider}: {answer.detections}/{answer.total or '?'} engine(s)"
            for answer in flagged
        ]
        link = next((answer.link for answer in flagged if answer.link), "")
        severity = Severity.CRITICAL if best.detections >= 5 else Severity.HIGH
        return Finding(
            path=path,
            threat=f"reputation: {threat}",
            engine=self.engine_id,
            severity=severity,
            category="reputation",
            size=size,
            sha256=digest,
            detail=", ".join(detail_parts) + (f" · {link}" if link else ""),
        )

    def _lookup(self, digest: str, ctx: ScanContext) -> list[Answer] | None:
        """Ask every live provider about one hash; None when nobody answered."""
        answers: list[Answer] = []
        answered = False
        for provider in self.providers:
            if provider.name in self._dead_providers:
                continue
            if not self.budget.take(ctx.cancel):
                return None
            try:
                answer = provider.lookup(digest)
            except ProviderAuthError as exc:
                self._dead_providers.add(provider.name)
                self.errors.append(str(exc))
                self.notes.append(f"{provider.label} was not used: {exc}")
                ctx.log(f"{provider.label}: {exc}; it will not be asked again", "warning")
                continue
            except ProviderBusyError as exc:
                self._dead_providers.add(provider.name)
                self.errors.append(str(exc))
                self.notes.append(f"{provider.label} stopped answering: {exc}")
                ctx.log(f"{provider.label}: {exc}; stopping early", "warning")
                continue
            except ProviderError as exc:
                self.errors.append(str(exc))
                ctx.log(f"{provider.label} lookup failed: {exc}", "warning")
                continue
            self.lookups += 1
            answered = True
            if answer is not None:
                answers.append(answer)
        return answers if answered else None

    def _upload(self, path: str, size: int, digest: str, ctx: ScanContext) -> Finding | None:
        uploader = next((p for p in self.providers if hasattr(p, "upload")), None)
        if uploader is None:
            return None
        if size > self.config.upload_max_size_mb * 1024 * 1024:
            return None
        if not self.budget.take(ctx.cancel):
            return None
        ctx.log(f"uploading {path} to {uploader.label} for analysis", "warning")
        try:
            analysis_id = uploader.upload(path)
            answer = uploader.analysis(
                analysis_id,
                deadline=time.monotonic() + max(30.0, float(self.config.timeout_s)),
            )
        except ProviderError as exc:
            self.errors.append(str(exc))
            ctx.log(f"{uploader.label} upload failed: {exc}", "warning")
            return None
        self._uploaded.append({"path": path, "provider": uploader.label, "link": answer.link})
        self.cache.put(digest, [answer])
        self.lookups += 1
        if not answer.detections:
            return None
        return self._reputation_finding(path, size, digest, [answer])
