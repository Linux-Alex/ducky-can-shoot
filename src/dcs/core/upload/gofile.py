"""gofile.io adapter - DCS's default host.

gofile is free, keyless and reachable, which is why it is the default:

    GET  https://api.gofile.io/servers          -> pick an upload server
    POST https://{server}.gofile.io/contents/uploadfile   multipart, field "file"
    POST https://api.gofile.io/contents/createFolder      group a multi part image

Two behaviours found by live testing (see docs/research/cloud-upload-apis.md)
shaped this implementation:

1. An upload does **not** need a token.  Anonymous POSTs to a store host succeed
   and the response carries a guest token inline.  Creating guest accounts up
   front only burns the rate limit.
2. The API host rate limits (and then blocks) aggressively after roughly ten
   calls in five minutes, while the store hosts keep answering.  So the server
   name is cached, a list of known store hosts is used as a fallback, and the
   folder step is treated as strictly optional.

The public link is always the downloadPage from the response; it is never
synthesised from the file id.
"""

from __future__ import annotations

import time
from typing import Any

from dcs.core.log import get_logger
from dcs.core.upload.base import (
    BaseProvider,
    MultipartStream,
    ProgressFn,
    UploadCancelled,
    UploadResult,
    file_md5,
)

log = get_logger("upload.gofile")

API_ROOT = "https://api.gofile.io"
USER_AGENT = "DCS/0.1 (+https://github.com/ducky-can-shoot/ducky-can-shoot)"

#: used when the API host is rate limited or unreachable
FALLBACK_STORE_HOSTS = (
    "store1.gofile.io",
    "store2.gofile.io",
    "store3.gofile.io",
    "store4.gofile.io",
)


class GofileProvider(BaseProvider):
    provider_id = "gofile"
    label = "gofile.io"
    requires_key = False
    #: gofile publishes no size limit for free accounts
    max_bytes = 0

    def __init__(
        self,
        config: Any = None,
        *,
        retries: int = 2,
        timeout: int = 1800,
        token: str = "",
        use_folders: bool = True,
    ) -> None:
        super().__init__(config, retries=retries, timeout=timeout)
        self.token = token
        self.use_folders = use_folders
        self._server: str = ""
        self._folder_id: str = ""
        self._folder_code: str = ""
        self.api_host_ok = True

    # -- availability -----------------------------------------------------
    def availability(self) -> tuple[bool, str]:
        """True when some upload host answers; the API host is optional."""
        try:
            server = self._pick_server()
            return True, f"upload server {server}"
        except Exception as exc:  # network problems are reported, not raised
            return False, f"gofile.io is not reachable: {exc}"

    # -- http helpers -----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        import requests

        kwargs.setdefault("timeout", (15, 60))
        kwargs.setdefault("headers", self._headers())
        response = requests.request(method, url, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> HTTP {response.status_code}: {response.text[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"{method} {url} returned no JSON: {response.text[:200]}") from exc

    def _probe(self, url: str) -> bool:
        import requests

        try:
            response = requests.head(url, timeout=(5, 10), allow_redirects=True)
            return response.status_code < 500
        except Exception:
            return False

    def _pick_server(self) -> str:
        """Return an upload host URL, preferring the API answer."""
        if self._server:
            return self._server

        if self.api_host_ok:
            try:
                payload = self._request("GET", f"{API_ROOT}/servers")
                servers = ((payload or {}).get("data") or {}).get("servers") or []
                for entry in servers:
                    name = str(entry.get("name") or entry.get("url") or "")
                    if not name:
                        continue
                    if name.startswith("http"):
                        self._server = name.rstrip("/")
                    else:
                        self._server = f"https://{name if '.' in name else name + '.gofile.io'}"
                    log.info("gofile upload server from API: %s", self._server)
                    return self._server
            except Exception as exc:
                self.api_host_ok = False
                log.warning("gofile servers endpoint unavailable (%s); using known hosts", exc)

        for host in FALLBACK_STORE_HOSTS:
            candidate = f"https://{host}"
            if self._probe(candidate):
                self._server = candidate
                log.info("gofile upload server from fallback list: %s", candidate)
                return self._server
        raise RuntimeError("no gofile upload server answered")

    # -- optional folder grouping ----------------------------------------
    def create_folder(self, name: str) -> str:
        """Try to create one folder so a split image shares a single link."""
        if self._folder_id or not self.use_folders or not self.api_host_ok:
            return self._folder_id
        try:
            payload = self._request(
                "POST",
                f"{API_ROOT}/contents/createFolder",
                json={"parentFolderId": None, "folderName": name},
            )
            data = (payload or {}).get("data") or {}
            self._folder_id = str(data.get("id") or "")
            self._folder_code = str(data.get("code") or "")
            if self._folder_id:
                log.info("gofile folder %s created for this job", self._folder_id)
        except Exception as exc:
            log.warning("could not create a gofile folder (%s); uploading loose files", exc)
            self.use_folders = False
        return self._folder_id

    def folder_link(self) -> str:
        return f"https://gofile.io/d/{self._folder_code}" if self._folder_code else ""

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

        if cancel is not None and cancel.is_set():
            # nothing is sent when the user cancelled before the first attempt
            return self._result(path, started, ok=False, error="cancelled")

        last_error = ""
        for attempt in range(1, self.retries + 2):
            try:
                server = self._pick_server()
                fields: dict[str, str] = {}
                if self._folder_id:
                    fields["folderId"] = self._folder_id
                # The body is streamed from disk: requests would otherwise read
                # the whole image into memory (and report no progress at all).
                stream = MultipartStream(
                    path,
                    file_field="file",
                    fields=fields,
                    progress=progress,
                    cancel=cancel,
                )
                response = requests.post(
                    f"{server}/contents/uploadfile",
                    data=stream,
                    headers={**self._headers(), **stream.headers},
                    timeout=(30, self.timeout),
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
                payload = response.json()
                status = str((payload or {}).get("status") or "")
                data = (payload or {}).get("data") or {}
                link = data.get("downloadPage") or data.get("downloadLink") or ""
                if status not in ("ok", "success") or not link:
                    raise RuntimeError(f"unexpected response: {str(payload)[:200]}")
                # gofile returns the md5 of what it stored: a mismatch means the
                # transfer was corrupted or truncated, so the link must not be
                # handed out as if the image were intact
                remote_md5 = str(data.get("md5") or "").lower()
                if remote_md5:
                    local_md5 = file_md5(path)
                    if local_md5 != remote_md5:
                        raise RuntimeError(
                            f"the host stored a different file (md5 {remote_md5[:12]} "
                            f"instead of {local_md5[:12]}); retrying"
                        )
                # an anonymous upload hands back a guest token: reuse it so the
                # next part of a split image lands in the same account
                guest_token = str(data.get("guestToken") or "")
                if guest_token and not self.token:
                    self.token = guest_token
                return self._result(
                    path,
                    started,
                    ok=True,
                    link=link,
                    detail=f"uploaded to {server.split('//')[-1]}"
                    + (f" ({part_label})" if part_label else ""),
                    raw={
                        "fileId": data.get("id") or "",
                        "code": data.get("code") or "",
                        "parentFolder": data.get("parentFolderCode") or "",
                        "server": server,
                        # the host checksum, already compared against the local
                        # file above; kept so the report can show it
                        "md5": remote_md5,
                        "verified": bool(remote_md5),
                    },
                )
            except UploadCancelled:
                return self._result(path, started, ok=False, error="cancelled")
            except Exception as exc:
                last_error = str(exc)
                log.warning("gofile upload attempt %d failed: %s", attempt, exc)
                self._server = ""  # let the next attempt pick another host
                if attempt <= self.retries:
                    time.sleep(min(30, 2**attempt))
        return self._result(path, started, ok=False, error=last_error or "upload failed")
