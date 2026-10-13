"""Cloud upload adapters.

The default is gofile.io (free, no account, JSON API).  The other adapters
exist so that a user can switch provider in one line of configuration when the
default is down or the archive does not fit: pixeldrain, 0x0.st, tmpfiles.org
and litterbox all implement the same tiny interface.
"""

from __future__ import annotations

from dcs.core.upload.base import (
    BaseProvider,
    ProgressFn,
    ProgressReader,
    UploadCancelled,
    UploadError,
    UploadResult,
)
from dcs.core.upload.registry import build_provider, provider_catalog, provider_ids

__all__ = [
    "BaseProvider",
    "ProgressFn",
    "ProgressReader",
    "UploadCancelled",
    "UploadError",
    "UploadResult",
    "build_provider",
    "provider_catalog",
    "provider_ids",
]
