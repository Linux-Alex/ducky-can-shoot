"""Provider registry: one place that knows every available cloud host.

The list of simple hosts is derived from SIMPLE_HOSTS itself, so an adapter can
never be advertised without existing (the earlier hard coded list drifted out of
sync with the host table, which made build_provider fall back to gofile for an
id the CLI still printed).
"""

from __future__ import annotations

from typing import Any

from dcs.core.config import UploadConfig
from dcs.core.log import get_logger
from dcs.core.upload.base import UploadProvider
from dcs.core.upload.gofile import GofileProvider
from dcs.core.upload.simple import SIMPLE_HOSTS, SimpleHostProvider

log = get_logger("upload.registry")

#: ids that were renamed when the host behind them changed
LEGACY_IDS: dict[str, str] = {
    "0x0": "x0at",  # 0x0.st disabled uploads; x0.at is its successor
}


def provider_ids() -> list[str]:
    """Every id build_provider understands, default first."""
    return ["gofile", *SIMPLE_HOSTS.keys()]


def resolve_provider_id(provider_id: str) -> str:
    """Map an id (including a legacy spelling) to a provider this build has."""
    candidate = (provider_id or "").strip().lower()
    if candidate in LEGACY_IDS:
        replacement = LEGACY_IDS[candidate]
        log.warning("provider %r was renamed to %r", candidate, replacement)
        return replacement
    return candidate


def provider_catalog() -> list[dict[str, Any]]:
    """Metadata for the GUI: id, label, key requirement, limits, notes."""
    catalog: list[dict[str, Any]] = [
        {
            "id": "gofile",
            "label": "gofile.io",
            "requiresKey": False,
            "maxBytes": 0,
            "notes": (
                "Free JSON API, no account needed. Uploads keep working while the API host is "
                "rate limited, and the store host is cached for the rest of the job."
            ),
            "verified": "live 2026-01",
        }
    ]
    for spec in SIMPLE_HOSTS.values():
        catalog.append(
            {
                "id": spec.provider_id,
                "label": spec.label,
                "requiresKey": spec.requires_key,
                "maxBytes": spec.max_bytes,
                "notes": spec.notes,
                "verified": spec.verified,
            }
        )
    return catalog


def _host_config(config: UploadConfig, provider_id: str) -> str:
    """The configured base URL override for a simple host, if any."""
    section = getattr(config, provider_id, None)
    return getattr(section, "base_url", "") if section is not None else ""


def build_provider(
    config: UploadConfig,
    provider_id: str | None = None,
    *,
    secrets: dict[str, str] | None = None,
) -> UploadProvider:
    """Instantiate the configured (or requested) provider."""
    secrets = secrets or {}
    pid = resolve_provider_id(provider_id or config.provider or "gofile")
    retries = config.retries
    timeout = config.timeout_s

    if pid == "gofile":
        token = secrets.get("gofile_token") or config.gofile.token
        return GofileProvider(config.gofile, retries=retries, timeout=timeout, token=token)

    if pid in SIMPLE_HOSTS:
        api_key = ""
        if pid == "pixeldrain":
            api_key = secrets.get("pixeldrain_api_key") or config.pixeldrain.api_key
        return SimpleHostProvider(
            SIMPLE_HOSTS[pid],
            retries=retries,
            timeout=timeout,
            api_key=api_key,
            base_url=_host_config(config, pid),
        )

    log.warning(
        "unknown upload provider %r (known: %s); falling back to gofile",
        pid,
        ", ".join(provider_ids()),
    )
    return GofileProvider(config.gofile, retries=retries, timeout=timeout)
