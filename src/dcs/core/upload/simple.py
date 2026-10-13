"""Adapters for the plain "send one file, get one URL" hosts.

These are the fallbacks for when gofile is unavailable or the archive does not
fit.  They differ only in a handful of details (URL, multipart field names,
authentication, how the link is spelled), so they share one implementation
driven by the table below.

Every entry records how it was verified, because these APIs move: the research
in docs/research/cloud-upload-apis.md checked each of them live in January 2026
and found, for example, that 0x0.st had disabled uploads and that pixeldrain
had dropped anonymous uploads entirely.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from dcs.core.log import get_logger
from dcs.core.upload.base import (
    BaseProvider,
    MultipartStream,
    ProgressFn,
    ProgressReader,
    UploadCancelled,
    UploadResult,
)

log = get_logger("upload.simple")

USER_AGENT = "DCS/0.1 (+https://github.com/linux-alex/ducky-can-shoot)"

MiB = 1024**2
GiB = 1024**3


@dataclass(frozen=True)
class SimpleHostSpec:
    """Everything that differs between the simple hosts."""

    provider_id: str
    label: str
    upload_url: str
    method: str = "POST"
    #: name of the multipart file field
    file_field: str = "file"
    #: extra multipart fields
    fields: tuple[tuple[str, str], ...] = ()
    #: response body is JSON rather than a bare URL
    json_response: bool = True
    #: dotted path inside the JSON answer that holds the link
    link_path: tuple[str, ...] = ()
    link_template: str = ""
    max_bytes: int = 0
    requires_key: bool = False
    #: HTTP Basic with the API key in the password (pixeldrain style)
    basic_key_auth: bool = False
    notes: str = ""
    verified: str = ""


def _rebase(url: str, base_url: str) -> str:
    """Replace the scheme and host of *url* with *base_url*.

    The path of the original URL is kept, so a mirror only has to serve the same
    layout: https://pixeldrain.com/api/file/x with base_url https://mirror.test
    becomes https://mirror.test/api/file/x.
    """
    from urllib.parse import urlsplit

    base = base_url.strip().rstrip("/")
    if not base:
        return url
    if "://" not in base:
        base = "https://" + base
    base_parts = urlsplit(base)
    base_path = base_parts.path.rstrip("/")
    spec_path = urlsplit(url).path
    # a mirror may already include the API prefix (https://mirror/api), in
    # which case the overlapping part of the original path is dropped
    if base_path and spec_path.startswith(base_path):
        spec_path = spec_path[len(base_path) :]
    return f"{base_parts.scheme}://{base_parts.netloc}{base_path}{spec_path}"


SIMPLE_HOSTS: dict[str, SimpleHostSpec] = {
    "litterbox": SimpleHostSpec(
        provider_id="litterbox",
        label="litterbox.catbox.moe",
        upload_url="https://litterbox.catbox.moe/resources/internals/api.php",
        file_field="fileToUpload",
        fields=(("reqtype", "fileupload"), ("time", "72h")),
        json_response=False,
        max_bytes=1 * GiB,
        notes="No account, 1 GB cap, uploads are deleted after 72 hours.",
        verified="live 2026-01",
    ),
    "tempsh": SimpleHostSpec(
        provider_id="tempsh",
        label="temp.sh",
        upload_url="https://temp.sh/upload",
        file_field="file",
        json_response=False,
        max_bytes=4 * GiB,
        notes="Largest anonymous cap found (4 GB, 3 day expiry); single operator service.",
        verified="live 2026-01",
    ),
    "x0at": SimpleHostSpec(
        provider_id="x0at",
        label="x0.at",
        upload_url="https://x0.at/",
        file_field="file",
        json_response=False,
        max_bytes=1 * GiB,
        notes="Successor of 0x0.st (whose uploads were disabled); retention depends on size.",
        verified="live 2026-01",
    ),
    "catbox": SimpleHostSpec(
        provider_id="catbox",
        label="catbox.moe",
        upload_url="https://catbox.moe/user/api.php",
        file_field="fileToUpload",
        fields=(("reqtype", "fileupload"),),
        json_response=False,
        max_bytes=200 * MiB,
        notes="Permanent storage, 200 MB cap.",
        verified="live 2026-01",
    ),
    "tmpfiles": SimpleHostSpec(
        provider_id="tmpfiles",
        label="tmpfiles.org",
        upload_url="https://tmpfiles.org/api/v1/upload",
        file_field="file",
        json_response=True,
        link_path=("data", "url"),
        max_bytes=100 * MiB,
        notes=(
            "Temporary (1 h .. 48 h), 100 MB cap. The /dl/ rewrite no longer serves "
            "bytes, so the page link is handed out instead."
        ),
        verified="live 2026-01",
    ),
    "pixeldrain": SimpleHostSpec(
        provider_id="pixeldrain",
        label="pixeldrain.com",
        upload_url="https://pixeldrain.com/api/file/{name}",
        method="PUT",
        json_response=True,
        link_path=("id",),
        link_template="https://pixeldrain.com/u/{value}",
        max_bytes=20 * GiB,
        requires_key=True,
        basic_key_auth=True,
        notes="Requires an API key; anonymous uploads are no longer accepted.",
        verified="live 2026-01 (401 authentication_required without a key)",
    ),
}


class SimpleHostProvider(BaseProvider):
    """Generic implementation for the hosts described by SimpleHostSpec."""

    def __init__(
        self,
        spec: SimpleHostSpec,
        *,
        retries: int = 2,
        timeout: int = 1800,
        api_key: str = "",
        base_url: str = "",
    ) -> None:
        super().__init__(None, retries=retries, timeout=timeout)
        self.spec = spec
        self.provider_id = spec.provider_id
        self.label = spec.label
        self.requires_key = spec.requires_key
        self.max_bytes = spec.max_bytes
        self.api_key = api_key
        #: upload.pixeldrain.base_url and friends: a mirror or a self hosted
        #: instance of the same API replaces the scheme and host of the spec
        self.upload_url = _rebase(spec.upload_url, base_url) if base_url else spec.upload_url

    # -- status -----------------------------------------------------------
    def availability(self) -> tuple[bool, str]:
        if self.requires_key and not self.api_key:
            return False, f"{self.spec.label} needs an API key ({self.spec.notes})"
        return True, self.spec.notes

    # -- helpers ----------------------------------------------------------
    def _url_for(self, path: str) -> str:
        if "{name}" in self.upload_url:
            return self.upload_url.replace("{name}", quote(os.path.basename(path)))
        return self.upload_url

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        if self.spec.basic_key_auth and self.api_key:
            token = base64.b64encode(f":{self.api_key}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        return headers

    def _extract_link(self, text: str) -> str:
        if not self.spec.json_response:
            return text.strip().splitlines()[-1].strip()
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise RuntimeError(f"non JSON answer: {text[:160]}") from exc
        node: Any = payload
        for key in self.spec.link_path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                raise RuntimeError(f"missing key {key!r} in answer: {str(payload)[:160]}")
        link = str(node)
        if not link:
            raise RuntimeError(f"empty link in answer: {str(payload)[:160]}")
        if self.spec.link_template:
            link = self.spec.link_template.format(value=link)
        return link

    # -- upload -----------------------------------------------------------
    def upload(
        self,
        path: str,
        *,
        progress: ProgressFn | None = None,
        cancel: Any | None = None,
        part_label: str = "",
    ) -> UploadResult:
        import requests

        started = time.monotonic()
        problem = self._check_size(path, self.max_bytes)
        if problem:
            return self._result(path, started, ok=False, error=problem)
        if self.requires_key and not self.api_key:
            return self._result(
                path, started, ok=False, error=f"{self.spec.label} requires an API key"
            )

        if cancel is not None and cancel.is_set():
            # nothing is sent when the user cancelled before the first attempt
            return self._result(path, started, ok=False, error="cancelled")

        last_error = ""
        for attempt in range(1, self.retries + 2):
            try:
                url = self._url_for(path)
                if self.spec.method == "PUT":
                    # pixeldrain reads the raw body, not a multipart form: the
                    # reader streams it and the length is stated explicitly so
                    # the server does not have to guess
                    with ProgressReader(path, progress=progress, cancel=cancel) as reader:
                        response = requests.put(
                            url,
                            data=reader,
                            headers={
                                **self._headers(),
                                "Content-Type": "application/octet-stream",
                                "Content-Length": str(reader.size),
                            },
                            timeout=(30, self.timeout),
                        )
                else:
                    # multipart, streamed from disk with an exact Content-Length
                    stream = MultipartStream(
                        path,
                        file_field=self.spec.file_field,
                        fields=dict(self.spec.fields),
                        progress=progress,
                        cancel=cancel,
                    )
                    response = requests.post(
                        url,
                        data=stream,
                        headers={**self._headers(), **stream.headers},
                        timeout=(30, self.timeout),
                    )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
                link = self._extract_link(response.text)
                return self._result(
                    path,
                    started,
                    ok=True,
                    link=link,
                    detail=f"uploaded to {self.spec.label}" + (f" ({part_label})" if part_label else ""),
                    raw={"notes": self.spec.notes, "verified": self.spec.verified},
                )
            except UploadCancelled:
                return self._result(path, started, ok=False, error="cancelled")
            except Exception as exc:
                last_error = str(exc)
                log.warning("%s attempt %d failed: %s", self.spec.provider_id, attempt, exc)
                if attempt <= self.retries:
                    time.sleep(min(20, 2**attempt))
        return self._result(path, started, ok=False, error=last_error or "upload failed")
