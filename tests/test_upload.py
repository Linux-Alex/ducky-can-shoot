"""Tests for the cloud upload adapters in dcs.core.upload.

Everything here is offline: requests.post/put and the gofile internals are
replaced by recording stand ins, so no test can reach a real host, a real
credential or the network.
"""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from dcs.core.config import AppConfig, GofileConfig, UploadConfig
from dcs.core.upload import gofile as gofile_module
from dcs.core.upload import registry
from dcs.core.upload import simple as simple_module
from dcs.core.upload.base import (
    BaseProvider,
    ProgressReader,
    UploadCancelled,
    UploadResult,
)
from dcs.core.upload.gofile import FALLBACK_STORE_HOSTS, GofileProvider
from dcs.core.upload.simple import SIMPLE_HOSTS, SimpleHostProvider

STORE_URL = "https://store1.gofile.io"
SERVERS_URL = "https://api.gofile.io/servers"

#: every id provider_ids() advertises, mapped to the (provider_id, label) pair
#: the registry is expected to build for it
EXPECTED_PROVIDERS: dict[str, tuple[str, str]] = {
    "gofile": ("gofile", "gofile.io"),
    "pixeldrain": ("pixeldrain", "pixeldrain.com"),
    "x0at": ("x0at", "x0.at"),
    "tmpfiles": ("tmpfiles", "tmpfiles.org"),
    "litterbox": ("litterbox", "litterbox.catbox.moe"),
    "catbox": ("catbox", "catbox.moe"),
    "tempsh": ("tempsh", "temp.sh"),
}

#: the plain text hosts and the exact request each of them must produce
PLAIN_HOSTS: dict[str, tuple[str, str, dict[str, str] | None]] = {
    "litterbox": (
        "https://litterbox.catbox.moe/resources/internals/api.php",
        "fileToUpload",
        {"reqtype": "fileupload", "time": "72h"},
    ),
    "tempsh": ("https://temp.sh/upload", "file", None),
    "x0at": ("https://x0.at/", "file", None),
    "catbox": ("https://catbox.moe/user/api.php", "fileToUpload", {"reqtype": "fileupload"}),
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand in for requests.Response."""

    def __init__(self, status_code: int = 200, text: str = "", payload: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self.payload = payload

    def json(self) -> Any:
        if self.payload is None:
            raise ValueError("no JSON body")
        return self.payload


class Recorder:
    """Callable stand in for requests.post/put that records every argument.

    The answers are used in order; the last one repeats, so a single answer
    models a host that answers the same way forever.
    """

    def __init__(self, *answers: Any, read_body: bool = False) -> None:
        if not answers:
            raise ValueError("at least one answer is required")
        self.answers = list(answers)
        self.read_body = read_body
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        call: dict[str, Any] = {"url": url, **kwargs}
        if self.read_body:
            bodies = {
                field: value[1].read()
                for field, value in (kwargs.get("files") or {}).items()
                if isinstance(value, tuple) and hasattr(value[1], "read")
            }
            if bodies:
                call["bodies"] = bodies
            body = kwargs.get("data")
            if hasattr(body, "read"):
                call["body"] = body.read()
        self.calls.append(call)
        index = len(self.calls) - 1
        answer = self.answers[index] if index < len(self.answers) else self.answers[-1]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def forbidden(*args: Any, **kwargs: Any) -> Any:
    """A request stand in that fails the test when it is called."""
    raise AssertionError("no HTTP request was expected")


def make_sample(root: Path, name: str = "payload.bin", data: bytes = b"payload bytes") -> Path:
    """Write one real sample file and return its path."""
    path = root / name
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def block_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any escaped HTTP call fail the test instead of reaching a real host."""
    monkeypatch.setattr("requests.request", forbidden)
    monkeypatch.setattr("requests.head", forbidden)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Neutralise the retry backoff so that no test in this file can sleep.

    Both adapters call time.sleep on the very same module object, so patching it
    through dcs.core.upload.gofile also covers dcs.core.upload.simple.  The
    returned list records the delays a test would otherwise have waited for.
    """
    assert simple_module.time is gofile_module.time
    delays: list[float] = []
    monkeypatch.setattr(gofile_module.time, "sleep", delays.append)
    return delays


@pytest.fixture
def upload_config(tmp_config: AppConfig) -> UploadConfig:
    """The upload section of the temporary configuration."""
    return tmp_config.upload


@pytest.fixture
def gofile(tmp_config: AppConfig) -> GofileProvider:
    """A gofile provider pinned to one store host so no API call is needed."""
    provider = GofileProvider(tmp_config.upload.gofile, retries=1, timeout=30)
    provider._server = STORE_URL
    return provider


def make_simple(provider_id: str, *, retries: int = 0, **kwargs: Any) -> SimpleHostProvider:
    """Build one of the plain hosts with retries disabled."""
    return SimpleHostProvider(SIMPLE_HOSTS[provider_id], retries=retries, **kwargs)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_provider_ids_exact() -> None:
    """The advertised provider list is stable, ordered and starts with the default."""
    assert registry.provider_ids() == [
        "gofile",
        *SIMPLE_HOSTS,
    ]
    assert set(registry.provider_ids()) == {
        "gofile",
        "pixeldrain",
        "x0at",
        "tmpfiles",
        "litterbox",
        "catbox",
        "tempsh",
    }
    assert registry.provider_ids()[0] == UploadConfig().provider


def test_provider_catalog_shape() -> None:
    """Every catalog entry has exactly the keys the GUI reads."""
    catalog = registry.provider_catalog()
    keys = {"id", "label", "requiresKey", "maxBytes", "notes", "verified"}
    assert catalog, "the catalog must not be empty"
    for entry in catalog:
        assert set(entry) == keys
        assert entry["id"] and entry["label"] and entry["notes"]
        assert isinstance(entry["requiresKey"], bool)
        assert isinstance(entry["maxBytes"], int)

    by_id = {entry["id"]: entry for entry in catalog}
    assert len(by_id) == len(catalog), "catalog ids must be unique"
    # one entry per simple host, plus gofile
    assert set(by_id) == {"gofile", *SIMPLE_HOSTS}
    assert by_id["gofile"]["requiresKey"] is False
    assert by_id["pixeldrain"]["requiresKey"] is True
    assert by_id["catbox"]["maxBytes"] == 200 * 1024**2
    assert by_id["pixeldrain"]["maxBytes"] == 20 * 1024**3


def test_provider_ids_are_all_buildable_and_complete(upload_config: UploadConfig) -> None:
    """Every advertised id must build itself, and the id set must match the catalog."""
    advertised = registry.provider_ids()
    for provider_id in advertised:
        provider = registry.build_provider(upload_config, provider_id)
        assert provider.provider_id == provider_id, f"{provider_id} built {provider.provider_id}"
    assert set(advertised) == {entry["id"] for entry in registry.provider_catalog()}


@pytest.mark.parametrize("provider_id", sorted(EXPECTED_PROVIDERS))
def test_build_provider_identity(
    upload_config: UploadConfig,
    provider_id: str,
) -> None:
    """build_provider returns the adapter the id names, with its label."""
    expected_id, expected_label = EXPECTED_PROVIDERS[provider_id]
    provider = registry.build_provider(upload_config, provider_id)
    assert provider.provider_id == expected_id
    assert provider.label == expected_label


def test_legacy_provider_id_is_resolved(upload_config: UploadConfig) -> None:
    """A renamed host id (0x0 -> x0at) still builds the successor adapter."""
    assert "0x0" not in registry.provider_ids()
    provider = registry.build_provider(upload_config, "0x0")
    assert provider.provider_id == "x0at"
    assert provider.label == "x0.at"


def test_build_provider_carries_config_and_secrets(upload_config: UploadConfig) -> None:
    """Retries, timeout and both secret sources reach the adapter."""
    upload_config.retries = 3
    upload_config.timeout_s = 99
    upload_config.gofile = GofileConfig(token="cfg-token")
    upload_config.pixeldrain.api_key = "cfg-key"

    gofile = registry.build_provider(upload_config, "gofile")
    assert (gofile.retries, gofile.timeout) == (3, 99)
    assert gofile.token == "cfg-token"
    assert gofile.requires_key is False

    gofile_secret = registry.build_provider(
        upload_config, "gofile", secrets={"gofile_token": "secret-token"}
    )
    assert gofile_secret.token == "secret-token"

    pixel = registry.build_provider(upload_config, "pixeldrain")
    assert (pixel.retries, pixel.timeout) == (3, 99)
    assert pixel.api_key == "cfg-key"
    assert pixel.requires_key is True

    pixel_secret = registry.build_provider(
        upload_config, "pixeldrain", secrets={"pixeldrain_api_key": "secret-key"}
    )
    assert pixel_secret.api_key == "secret-key"


def test_build_provider_unknown_id_falls_back_to_gofile(upload_config: UploadConfig) -> None:
    """An unknown id degrades to the default host instead of raising."""
    provider = registry.build_provider(upload_config, "not-a-host")
    assert isinstance(provider, GofileProvider)
    assert provider.provider_id == "gofile"


def test_build_provider_uses_configured_default(upload_config: UploadConfig) -> None:
    """Without a requested id the configured provider is used."""
    upload_config.provider = "litterbox"
    assert registry.build_provider(upload_config).provider_id == "litterbox"


# ---------------------------------------------------------------------------
# gofile: server selection
# ---------------------------------------------------------------------------


def test_gofile_server_from_api_is_cached(
    upload_config: UploadConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The API answer picks the store host and is asked only once."""
    calls: list[tuple[str, str]] = []

    def fake_request(self: GofileProvider, method: str, url: str, **kwargs: Any) -> Any:
        calls.append((method, url))
        return {"data": {"servers": [{"name": "store9.gofile.io"}]}}

    monkeypatch.setattr(GofileProvider, "_request", fake_request)
    provider = GofileProvider(upload_config.gofile, retries=0)
    assert provider._pick_server() == "https://store9.gofile.io"
    assert provider._pick_server() == "https://store9.gofile.io"
    assert calls == [("GET", SERVERS_URL)]

    sample = make_sample(tmp_path, "medium.zip")
    post = Recorder(FakeResponse(200, payload={"status": "ok", "data": {"downloadPage": "https://gofile.io/d/x"}}))
    monkeypatch.setattr("requests.post", post)
    result = provider.upload(str(sample))
    assert result.ok is True
    assert post.calls[0]["url"] == "https://store9.gofile.io/contents/uploadfile"
    assert calls == [("GET", SERVERS_URL)]


def test_gofile_server_name_with_scheme_is_kept_as_is(
    upload_config: UploadConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully qualified server name is used verbatim, without a trailing slash."""
    monkeypatch.setattr(
        GofileProvider,
        "_request",
        lambda self, method, url, **kwargs: {"data": {"servers": [{"name": "https://store7.gofile.io/"}]}},
    )
    provider = GofileProvider(upload_config.gofile, retries=0)
    assert provider._pick_server() == "https://store7.gofile.io"


def test_gofile_falls_back_to_known_hosts(
    upload_config: UploadConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead API host flips api_host_ok and the first answering fallback wins."""

    def dead_api(self: GofileProvider, method: str, url: str, **kwargs: Any) -> Any:
        raise RuntimeError("API host rate limited")

    probed: list[str] = []

    def fake_probe(self: GofileProvider, url: str) -> bool:
        probed.append(url)
        return len(probed) > 2

    monkeypatch.setattr(GofileProvider, "_request", dead_api)
    monkeypatch.setattr(GofileProvider, "_probe", fake_probe)

    provider = GofileProvider(upload_config.gofile, retries=0)
    assert provider.api_host_ok is True
    assert provider._pick_server() == "https://store3.gofile.io"
    assert provider.api_host_ok is False
    assert probed == [f"https://{host}" for host in FALLBACK_STORE_HOSTS[:3]]


def test_gofile_availability_when_nothing_answers(
    upload_config: UploadConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable gofile is reported, never raised."""

    def dead_api(self: GofileProvider, method: str, url: str, **kwargs: Any) -> Any:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(GofileProvider, "_request", dead_api)
    monkeypatch.setattr(GofileProvider, "_probe", lambda self, url: False)

    provider = GofileProvider(upload_config.gofile, retries=0)
    usable, message = provider.availability()
    assert usable is False
    assert "not reachable" in message
    assert "no gofile upload server answered" in message


def test_gofile_availability_true_when_api_answers(
    upload_config: UploadConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working API host makes the provider available and names the server."""
    monkeypatch.setattr(
        GofileProvider,
        "_request",
        lambda self, method, url, **kwargs: {"data": {"servers": [{"name": "store5.gofile.io"}]}},
    )
    provider = GofileProvider(upload_config.gofile, retries=0)
    assert provider.availability() == (True, "upload server https://store5.gofile.io")


# ---------------------------------------------------------------------------
# gofile: upload parsing
# ---------------------------------------------------------------------------


def test_gofile_upload_uses_the_download_page(
    gofile: GofileProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public link is the downloadPage, not a link built from the file id."""
    sample = make_sample(tmp_path, "medium.zip", b"x" * 10)
    payload = {
        "status": "ok",
        "data": {
            "id": "file-id-1234",
            "code": "AbCdEf",
            "downloadPage": "https://gofile.io/d/AbCdEf",
            "guestToken": "guest-token",
        },
    }
    post = Recorder(FakeResponse(200, payload=payload), read_body=True)
    monkeypatch.setattr("requests.post", post)

    result = gofile.upload(str(sample), part_label="part 1")

    assert result.ok is True
    assert result.link == "https://gofile.io/d/AbCdEf"
    assert result.provider == "gofile"
    assert result.path == str(sample)
    assert result.size == 10
    assert result.error == ""
    assert result.raw["fileId"] == "file-id-1234"
    assert result.raw["server"] == STORE_URL
    assert result.detail == "uploaded to store1.gofile.io (part 1)"

    call = post.calls[0]
    assert call["url"] == f"{STORE_URL}/contents/uploadfile"
    assert set(call["files"]) == {"file"}
    assert call["files"]["file"][0] == "medium.zip"
    assert call["bodies"]["file"] == b"x" * 10
    assert call["data"] is None


def test_gofile_guest_token_is_captured(
    gofile: GofileProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guest token of an anonymous upload is reused for the next part."""
    sample = make_sample(tmp_path)
    assert gofile.token == ""
    monkeypatch.setattr(
        "requests.post",
        Recorder(FakeResponse(200, payload={"status": "ok", "data": {"downloadPage": "https://gofile.io/d/a", "guestToken": "guest-1"}})),
    )
    assert gofile.upload(str(sample)).ok is True
    assert gofile.token == "guest-1"

    # a configured token is never overwritten by the guest token
    gofile.token = "configured"
    monkeypatch.setattr(
        "requests.post",
        Recorder(FakeResponse(200, payload={"status": "ok", "data": {"downloadPage": "https://gofile.io/d/b", "guestToken": "guest-2"}})),
    )
    assert gofile.upload(str(sample)).ok is True
    assert gofile.token == "configured"


def test_gofile_authorization_header_only_with_a_token(
    tmp_config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bearer header appears exactly when a token is configured."""
    sample = make_sample(tmp_path)
    answer = FakeResponse(200, payload={"status": "ok", "data": {"downloadPage": "https://gofile.io/d/a"}})

    anonymous = GofileProvider(tmp_config.upload.gofile, retries=0)
    anonymous._server = STORE_URL
    post = Recorder(answer)
    monkeypatch.setattr("requests.post", post)
    assert anonymous.upload(str(sample)).ok is True
    assert "Authorization" not in post.calls[0]["headers"]
    assert post.calls[0]["headers"]["User-Agent"].startswith("DCS/")

    member = GofileProvider(tmp_config.upload.gofile, retries=0, token="tok-123")
    member._server = STORE_URL
    post = Recorder(answer)
    monkeypatch.setattr("requests.post", post)
    assert member.upload(str(sample)).ok is True
    assert post.calls[0]["headers"]["Authorization"] == "Bearer tok-123"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok", "data": {}},
        {"status": "ok", "data": {"id": "only-an-id"}},
        {"status": "error", "data": {"downloadPage": "https://gofile.io/d/a"}},
    ],
)
def test_gofile_upload_without_a_usable_link_fails(
    gofile: GofileProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_backoff: list[float],
    payload: dict[str, Any],
) -> None:
    """A response without a downloadPage, or with an error status, is a failure."""
    # the retry clears the cached server, so the API host must answer from a stub
    monkeypatch.setattr(
        GofileProvider,
        "_request",
        lambda self, method, url, **kwargs: {"data": {"servers": [{"name": "store9.gofile.io"}]}},
    )
    monkeypatch.setattr(GofileProvider, "_probe", lambda self, url: False)
    sample = make_sample(tmp_path)
    post = Recorder(FakeResponse(200, text=json.dumps(payload), payload=payload))
    monkeypatch.setattr("requests.post", post)

    result = gofile.upload(str(sample))

    assert result.ok is False
    assert result.link == ""
    assert result.error == f"unexpected response: {str(payload)[:200]}"
    assert gofile.token == ""
    assert len(post.calls) == gofile.retries + 1 == 2
    assert no_backoff == [2.0]


# ---------------------------------------------------------------------------
# gofile: retries
# ---------------------------------------------------------------------------


def test_gofile_retries_once_then_succeeds(
    upload_config: UploadConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_backoff: list[float],
) -> None:
    """One HTTP 500 is retried and the second attempt wins."""
    monkeypatch.setattr(
        GofileProvider,
        "_request",
        lambda self, method, url, **kwargs: {"data": {"servers": [{"name": "store9.gofile.io"}]}},
    )
    sample = make_sample(tmp_path)
    post = Recorder(
        FakeResponse(500, text="server exploded"),
        FakeResponse(200, payload={"status": "ok", "data": {"downloadPage": "https://gofile.io/d/ok"}}),
    )
    monkeypatch.setattr("requests.post", post)

    provider = GofileProvider(upload_config.gofile, retries=1, timeout=30)
    provider._server = STORE_URL  # the first attempt uses the pinned host
    result = provider.upload(str(sample))

    assert result.ok is True
    assert result.link == "https://gofile.io/d/ok"
    assert len(post.calls) == 2
    assert post.calls[0]["url"] == f"{STORE_URL}/contents/uploadfile"
    assert post.calls[1]["url"] == "https://store9.gofile.io/contents/uploadfile"
    assert result.raw["server"] == "https://store9.gofile.io"
    assert len(post.calls) == provider.retries + 1 == 2
    assert no_backoff == [2.0]


def test_gofile_reports_the_last_error_when_every_attempt_fails(
    upload_config: UploadConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_backoff: list[float],
) -> None:
    """With every attempt failing the last error text is returned."""
    monkeypatch.setattr(
        GofileProvider,
        "_request",
        lambda self, method, url, **kwargs: {"data": {"servers": [{"name": "store9.gofile.io"}]}},
    )
    sample = make_sample(tmp_path)
    post = Recorder(FakeResponse(503, text="service unavailable"))
    monkeypatch.setattr("requests.post", post)

    provider = GofileProvider(upload_config.gofile, retries=2, timeout=30)
    result = provider.upload(str(sample))

    assert result.ok is False
    assert result.link == ""
    assert result.error == "HTTP 503: service unavailable"
    assert len(post.calls) == provider.retries + 1 == 3
    assert no_backoff == [2.0, 4.0]


# ---------------------------------------------------------------------------
# simple hosts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_id", sorted(PLAIN_HOSTS))
def test_simple_plain_text_hosts(
    provider_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each plain host is POSTed to its own URL with its own field names."""
    expected_url, field, expected_data = PLAIN_HOSTS[provider_id]
    sample = make_sample(tmp_path, "medium.zip", b"PK\x03\x04 archive")
    post = Recorder(FakeResponse(200, text="\nignored first line\n  https://files.example/abc.zip  \n"))
    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.put", forbidden)

    provider = make_simple(provider_id)
    result = provider.upload(str(sample))

    assert result.ok is True
    assert result.link == "https://files.example/abc.zip"
    assert result.provider == provider_id
    assert result.size == len(b"PK\x03\x04 archive")
    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["url"] == expected_url
    assert call["data"] == expected_data
    assert set(call["files"]) == {field}
    assert call["files"][field][0] == "medium.zip"
    assert "Authorization" not in call["headers"]


def test_tmpfiles_uses_the_json_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tmpfiles answers JSON and the link is data.url."""
    sample = make_sample(tmp_path, "medium.zip")
    body = '{"status": "success", "data": {"url": "https://tmpfiles.org/12345/medium.zip"}}'
    post = Recorder(FakeResponse(200, text=body, payload=json.loads(body)))
    monkeypatch.setattr("requests.post", post)

    result = make_simple("tmpfiles").upload(str(sample))

    assert result.ok is True
    assert result.link == "https://tmpfiles.org/12345/medium.zip"
    assert post.calls[0]["url"] == "https://tmpfiles.org/api/v1/upload"
    assert set(post.calls[0]["files"]) == {"file"}


def test_tmpfiles_non_json_answer_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTML error page is reported as a failure, not parsed."""
    sample = make_sample(tmp_path)
    monkeypatch.setattr("requests.post", Recorder(FakeResponse(200, text="<html>error</html>")))
    result = make_simple("tmpfiles").upload(str(sample))
    assert result.ok is False
    assert result.error.startswith("non JSON answer:")


def test_pixeldrain_uses_put_with_basic_key_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pixeldrain receives the raw body at the quoted name with basic auth."""
    sample = make_sample(tmp_path, "report image.zip", b"PK\x03\x04 pixeldrain")
    put = Recorder(FakeResponse(200, text='{"id": "abc"}', payload={"id": "abc"}), read_body=True)
    monkeypatch.setattr("requests.put", put)
    monkeypatch.setattr("requests.post", forbidden)

    result = make_simple("pixeldrain", api_key="key-42").upload(str(sample))

    assert result.ok is True
    assert result.link == "https://pixeldrain.com/u/abc"
    assert len(put.calls) == 1
    call = put.calls[0]
    assert call["url"] == "https://pixeldrain.com/api/file/report%20image.zip"
    assert call["body"] == b"PK\x03\x04 pixeldrain"
    expected = base64.b64encode(b":key-42").decode()
    assert call["headers"]["Authorization"] == f"Basic {expected}"
    assert "files" not in call


def test_pixeldrain_requires_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a key the provider is unusable and never touches the network."""
    provider = make_simple("pixeldrain")
    usable, message = provider.availability()
    assert usable is False
    assert message.startswith("pixeldrain.com needs an API key")
    assert "pixeldrain.com" in message

    monkeypatch.setattr("requests.put", forbidden)
    monkeypatch.setattr("requests.post", forbidden)
    sample = make_sample(tmp_path)
    result = provider.upload(str(sample))
    assert result.ok is False
    assert result.error == "pixeldrain.com requires an API key"


def test_availability_of_keyless_hosts() -> None:
    """The keyless hosts are available and explain themselves."""
    for provider_id in ("litterbox", "tempsh", "x0at", "catbox", "tmpfiles"):
        spec = SIMPLE_HOSTS[provider_id]
        assert make_simple(provider_id).availability() == (True, spec.notes)


def test_http_error_from_a_simple_host_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_backoff: list[float]
) -> None:
    """A 4xx body is surfaced in result.error after retries + 1 attempts."""
    sample = make_sample(tmp_path)
    post = Recorder(FakeResponse(429, text="slow down"))
    monkeypatch.setattr("requests.post", post)

    provider = make_simple("catbox", retries=1)
    result = provider.upload(str(sample))

    assert result.ok is False
    assert result.error == "HTTP 429: slow down"
    assert len(post.calls) == provider.retries + 1 == 2
    assert no_backoff == [2.0]


# ---------------------------------------------------------------------------
# size limits
# ---------------------------------------------------------------------------


def test_provider_rejects_a_file_over_its_limit(
    tmp_config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file over the host limit fails before any request is made."""
    sample = make_sample(tmp_path, "medium.zip", b"A" * 64)
    monkeypatch.setattr("requests.post", forbidden)
    monkeypatch.setattr("requests.put", forbidden)

    gfile = GofileProvider(tmp_config.upload.gofile, retries=0)
    gfile._server = STORE_URL
    monkeypatch.setattr(gfile, "max_bytes", 4)
    result = gfile.upload(str(sample))
    assert result.ok is False
    assert result.link == ""
    assert result.error == "medium.zip is 64 B, over the 4 B limit of this host"
    assert result.size == 64

    catbox = make_simple("catbox")
    monkeypatch.setattr(catbox, "max_bytes", 4)
    simple_result = catbox.upload(str(sample))
    assert simple_result.ok is False
    assert simple_result.error == "medium.zip is 64 B, over the 4 B limit of this host"
    assert simple_result.provider == "catbox"


def test_check_size_direct(tmp_path: Path) -> None:
    """A zero maximum means no limit; anything over it names both sizes."""
    sample = make_sample(tmp_path, "medium.zip", b"A" * 2048)
    assert BaseProvider._check_size(str(sample), 0) == ""
    assert BaseProvider._check_size(str(sample), -1) == ""
    assert BaseProvider._check_size(str(sample), 2048) == ""
    assert BaseProvider._check_size(str(sample), 1024) == (
        "medium.zip is 2.0 KiB, over the 1.0 KiB limit of this host"
    )


def test_real_catbox_limit_is_two_hundred_mebibytes() -> None:
    """The documented catbox cap survives the registry wiring."""
    assert SIMPLE_HOSTS["catbox"].max_bytes == 200 * 1024**2
    assert make_simple("catbox").max_bytes == 200 * 1024**2


# ---------------------------------------------------------------------------
# ProgressReader
# ---------------------------------------------------------------------------


def test_progress_reader_reports_every_chunk(tmp_path: Path) -> None:
    """Progress is reported per read and ends at (size, size)."""
    data = bytes(range(10))
    sample = make_sample(tmp_path, "ten.bin", data)
    seen: list[tuple[int, int]] = []

    with ProgressReader(str(sample), progress=lambda sent, total: seen.append((sent, total))) as reader:
        assert len(reader) == 10
        assert reader.read(4) == data[:4]
        assert reader.read(4) == data[4:8]
        assert reader.read(4) == data[8:]

    assert seen == [(4, 10), (8, 10), (10, 10)]
    assert seen[-1] == (10, 10)


def test_progress_reader_reads_a_chunk_by_default(tmp_path: Path) -> None:
    """A bare read() returns one chunk and reports the whole file."""
    data = b"abcdef"
    sample = make_sample(tmp_path, "six.bin", data)
    seen: list[tuple[int, int]] = []
    reader = ProgressReader(str(sample), progress=lambda sent, total: seen.append((sent, total)))
    try:
        assert reader.read() == data
    finally:
        reader.close()
    assert seen == [(6, 6)]


def test_progress_reader_reports_the_uploaded_size_on_a_large_file(tmp_path: Path) -> None:
    """A multi chunk file ends on the exact file size."""
    size = 3 * (1 << 20) + 7
    sample = make_sample(tmp_path, "big.bin", b"z" * size)
    seen: list[tuple[int, int]] = []
    read_total = 0
    with ProgressReader(str(sample), progress=lambda sent, total: seen.append((sent, total))) as reader:
        assert len(reader) == size
        while chunk := reader.read(1 << 20):
            read_total += len(chunk)
    assert read_total == size
    assert seen[-1] == (size, size)
    assert [sent for sent, _ in seen] == [1 << 20, 2 << 20, 3 << 20, size, size]


def test_progress_reader_raises_when_cancelled(tmp_path: Path) -> None:
    """A cancelled reader refuses to hand out more data."""
    sample = make_sample(tmp_path, "ten.bin", b"0123456789")
    cancel = threading.Event()
    with ProgressReader(str(sample), cancel=cancel) as reader:
        assert reader.read(4) == b"0123"
        cancel.set()
        with pytest.raises(UploadCancelled) as excinfo:
            reader.read(4)
    assert "cancelled" in str(excinfo.value)


def test_progress_reader_survives_a_broken_callback(tmp_path: Path) -> None:
    """A progress callback that raises never breaks the read."""
    sample = make_sample(tmp_path, "ten.bin", b"0123456789")

    def broken(sent: int, total: int) -> None:
        raise RuntimeError("the progress bar exploded")

    with ProgressReader(str(sample), progress=broken) as reader:
        assert reader.read(10) == b"0123456789"


def test_progress_reader_close_is_idempotent(tmp_path: Path) -> None:
    """Closing twice, and after a with block, is safe."""
    sample = make_sample(tmp_path, "ten.bin", b"0123456789")
    reader = ProgressReader(str(sample))
    assert reader.read(4) == b"0123"
    reader.close()
    reader.close()
    assert len(reader) == 10


def test_progress_reader_deleted_file_is_reported(tmp_path: Path) -> None:
    """A missing file fails loudly at construction time."""
    with pytest.raises(FileNotFoundError):
        ProgressReader(str(tmp_path / "ghost.bin"))


# ---------------------------------------------------------------------------
# cancellation and result shape
# ---------------------------------------------------------------------------


def test_gofile_upload_is_cancelled_through_the_reader(
    gofile: GofileProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel flag seen while reading turns into ok False and error cancelled."""
    sample = make_sample(tmp_path)
    cancel = threading.Event()

    def fake_post(url: str, **kwargs: Any) -> Any:
        cancel.set()
        kwargs["files"]["file"][1].read()
        raise AssertionError("the reader should have raised UploadCancelled")

    monkeypatch.setattr("requests.post", fake_post)
    result = gofile.upload(str(sample), cancel=cancel)

    assert result.ok is False
    assert result.error == "cancelled"
    assert result.link == ""
    assert result.size == len(b"payload bytes")


def test_gofile_upload_is_cancelled_when_the_reader_raises(
    gofile: GofileProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An UploadCancelled from the transport is not treated as a retryable error."""
    sample = make_sample(tmp_path)
    post = Recorder(UploadCancelled("upload cancelled by the user"))
    monkeypatch.setattr("requests.post", post)

    result = gofile.upload(str(sample))

    assert result.ok is False
    assert result.error == "cancelled"
    assert len(post.calls) == 1


def test_simple_upload_is_cancelled_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel flag already set makes the upload fail without sending bytes."""
    sample = make_sample(tmp_path)
    cancel = threading.Event()
    cancel.set()
    attempted: list[str] = []

    def reading_post(url: str, **kwargs: Any) -> Any:
        attempted.append(url)
        assert set(kwargs["files"]) == {"fileToUpload"}
        kwargs["files"]["fileToUpload"][1].read()  # must raise UploadCancelled
        raise AssertionError("a cancelled reader must not return data")

    monkeypatch.setattr("requests.post", reading_post)
    result = make_simple("litterbox", retries=0).upload(str(sample), cancel=cancel)

    assert result.ok is False
    assert result.error == "cancelled"
    assert attempted == ["https://litterbox.catbox.moe/resources/internals/api.php"]


def test_upload_result_to_dict_keys() -> None:
    """to_dict exposes the documented keys and copies the warnings list."""
    result = UploadResult(ok=True, link="https://x/y", provider="gofile", path="/tmp/a", size=3)
    data = result.to_dict()
    assert set(data) == {
        "ok",
        "link",
        "provider",
        "path",
        "size",
        "duration",
        "detail",
        "error",
        "warnings",
    }
    assert data["ok"] is True
    assert data["link"] == "https://x/y"
    assert data["warnings"] == []
    data["warnings"].append("mutated")
    assert result.warnings == []
