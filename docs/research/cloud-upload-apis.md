# Cloud upload APIs for Ducky Can Shoot (DCS)

**Status:** research snapshot, 2026-10-13
**Target:** DCS upload stage — push one ZIP archive, get a public link, email it.
**Method:** every endpoint below was called live with `curl` / `requests` from this machine unless explicitly marked otherwise.

## Verification legend

| Tag | Meaning |
| --- | --- |
| **[LIVE]** | I made the call from this machine and saw the response quoted here. |
| **[DOC]** | Read from the provider's own documentation / FAQ / production JS bundle. |
| **[SRC]** | Read from third-party client source code or community docs. |
| **[INFERRED]** | Deduced, not directly observed. Treat as a hypothesis. |

> The most important result of this research: **three of the seven requested providers are no longer usable as assumed.** 0x0.st has uploads switched off, pixeldrain requires an API key for *all* writes, and transfer.sh is unreachable. SwissTransfer has silently replaced the API that every third-party client targets. Plan accordingly.

---

## 0. Executive summary

| # | Provider | Anonymous upload works? | Verdict | Retention | Max size |
| --- | --- | --- | --- | --- | --- |
| 1 | **gofile.io** | **Yes** [LIVE] | **Implement first** | Temporary; cold storage after expiry | Not published |
| 2 | **SwissTransfer** | **No** — verified email + ALTCHA now required [LIVE] | Defer (unofficial, gated) | 1/3/7/15/30 days [DOC] | ~50 GB (site claim, unverified) |
| 3 | **pixeldrain.com** | **No** — API key required [LIVE] | Optional, key-only | Per plan | Not published |
| 4 | **0x0.st** | **No** — uploads disabled [LIVE] | **Do not implement** | n/a | n/a |
| 4b | **x0.at** (0x0 fork) | **Yes** [LIVE] | Good fallback | 3–100 days, size-dependent | 1024 MiB [DOC] |
| 5 | **tmpfiles.org** | **Yes** [LIVE] | Small-file fallback | 60 min / 6 h / 24 h / 48 h | 100 MB [DOC] |
| 6 | **litterbox.catbox.moe** | **Yes** [LIVE] | **Implement second** | 1 h / 12 h / 24 h / 72 h | 1 GB [DOC] |
| 6b | **catbox.moe** | **Yes** [LIVE] | Permanent option | Permanent | 200 MB [DOC] |
| 7 | **transfer.sh** | **No** — unreachable [LIVE] | **Do not implement** | n/a | n/a |
| 7b | **temp.sh** | **Yes** [LIVE] | transfer.sh replacement | 3 days [DOC] | 4 GB [DOC] |

**Recommended implementation order:** `gofile` → `litterbox` → `tmpfiles` → `catbox` → `x0.at` → `temp.sh` → `pixeldrain` (key) → SwissTransfer (last, gated).

---

## 1. gofile.io — RECOMMENDED PRIMARY

Gofile is the best fit: a real, documented JSON API, a genuine anonymous guest tier, and uploads that work with **no credentials at all**.

### 1.1 Endpoints

**Base API:** `https://api.gofile.io` — but **uploads go to a per-server host**, not the API host.

| Step | Method + URL | Auth |
| --- | --- | --- |
| Pick server | `GET https://api.gofile.io/servers` | none |
| Create guest account | `POST https://api.gofile.io/accounts` | none |
| Upload file | `POST https://{server}.gofile.io/contents/uploadfile` | `Authorization: Bearer <token>` (optional) |
| Create folder | `POST https://api.gofile.io/contents/createfolder` | `Authorization: Bearer <token>` |
| List/read contents | `GET https://api.gofile.io/contents/{id}?token=...` | **Premium only** |

#### Server list — [LIVE]

```bash
curl -s https://api.gofile.io/servers
```

HTTP 200. Real response (truncated):

```json
{"status":"ok","data":{"servers":[
  {"name":"store-na-phx-4","zone":"na"},
  {"name":"store-eu-par-4","zone":"eu"},
  {"name":"store8","zone":"na"}],
 "serversAllZone":[...]}}
```

The upload host is `https://<name>.gofile.io`, e.g. `https://store-na-phx-4.gofile.io`. Zone `eu` gives Paris; `na` gives Phoenix. Gofile docs say **do not call this more than once every 10 seconds** [SRC].

#### Guest account — [LIVE]

```bash
curl -s -X POST https://api.gofile.io/accounts
```

HTTP 200. Real response:

```json
{"status":"ok","data":{
  "id":"8df67b59-f0f3-4167-874a-fd132ee3cc54",
  "rootFolder":"e25fddc8-a9bd-4a00-b826-b5c16a4e92a9",
  "tier":"guest",
  "token":"rqZrtvYGTno7wCO87Y6XM8MpQcmfghzW"}}
```

#### Upload — [LIVE]

```bash
curl -H "Authorization: Bearer $TOKEN" \
     -F "file=@archive.zip" \
     "https://store-eu-par-4.gofile.io/contents/uploadfile"
```

Multipart field name is exactly `file`. Optional extra field `folderId` targets a folder.

HTTP 200. **Real response from a live upload of a 56-byte text file:**

```json
{"data":{
  "code":"GSyMiLAf",
  "createTime":1791911422,
  "downloadPage":"https://gofile.io/d/Ve5YurEl",
  "guestToken":"gISkDjDKYd7XvSZtBHHhGJguybsNcCD7",
  "id":"6c333c2f-f7a9-49b6-a6f7-e4f7d338fccd",
  "md5":"d6c5ca1fc2b9c1392458e31161518817",
  "mimetype":"text/plain; charset=utf-8",
  "name":"probe.txt",
  "parentFolder":"a9e43b03-db7f-4c9e-8dc0-df7ccebd7730",
  "parentFolderCode":"Ve5YurEl",
  "servers":["store3"],
  "size":56,
  "type":"file"},
 "status":"ok"}
```

#### Upload with NO token at all — [LIVE]

Passing **no** `Authorization` header works. Gofile creates a guest account implicitly and returns `guestToken` in the same response. Verified HTTP 200:

```json
{"data":{"code":"145dtYFf","downloadPage":"https://gofile.io/d/vnXu12fl",
 "guestToken":"igXfzqZpysZq88RdxYsXWATiTEcbJ8bY", ...},"status":"ok"}
```

This is the simplest integration path and the one DCS should use as a bootstrap: **one POST, no prior state.** Persist `guestToken` and reuse it so you never create accounts repeatedly (see rate limits below).

#### Folder creation — [LIVE]

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"parentFolderId":"<rootFolder>","folderName":"DCS"}' \
  https://api.gofile.io/contents/createfolder
```

HTTP 200:

```json
{"status":"ok","data":{"id":"dce10a8d-0110-4b7f-b7eb-7a8a4e2509c2",
 "owner":"a29c89c9-6024-4e59-bcba-7c4e672f2942","type":"folder",
 "name":"DCS Test","parentFolder":"b8524d01-...","code":"SGcqW8hp",
 "createTime":1791911435,"modTime":1791911435}}
```

Then upload with `-F folderId=dce10a8d-...`; the response's `parentFolderCode` becomes the shared folder code and `downloadPage` is the folder link — i.e. **all files in one folder share one link**. For DCS (one ZIP) this is optional, but it is the right way to group a multi-part archive.

Note: if the token and the `parentFolderId` belong to different accounts you get HTTP 400 `{"status":"error-notOwner"}` — verified.

#### Reading contents is now Premium-only — [LIVE]

`GET https://api.gofile.io/contents/{id}` returns **HTTP 401** for guest accounts:

```json
{"status":"error-notPremium","data":{}}
```

I tried every token-passing style: `Authorization: Bearer` header, `?token=` query, and legacy `?wt=` — **all returned `error-notPremium`**. Gofile's own FAQ states it plainly [DOC]:

> "Most endpoints require a Premium account. Basic operations — uploading and basic content management — are available to free accounts."

A community write-up claims `error-notPremium` is really an obfuscated `X-Website-Token` (`wt`) problem, computed as `sha256(UA::lang::accountToken::window::salt)` [SRC]. I **implemented that formula and tested it live — it did NOT work**; the request still returned `error-notPremium`. I did confirm `https://gofile.io/js/wt.obf.js` exists (the obfuscated generator is real) and that the old `https://gofile.io/dist/js/config.js` is now **404** [LIVE]. So the token story is plausible but the published algorithm is stale. **Do not build on it.**

**Consequence for DCS: never depend on listing contents.** Everything you need — the shareable link — is already in the upload response as `downloadPage`.

#### Direct-download URLs are gone — [LIVE]

The legacy direct link is dead:

```bash
curl -L "https://store3.gofile.io/download/<fileId>/probe.txt"
# -> HTTP 200 but returns the 3358-byte SPA shell HTML, NOT the file
curl -L "https://store3.gofile.io/download/direct/<fileId>/probe.txt"
# -> HTTP 404 "docContent not found in DB"
```

So **email the `downloadPage` URL** (`https://gofile.io/d/<code>`). I fetched such a page and it returns HTTP 200; the recipient's browser renders the download UI. There is a real direct-link feature, but it is created via `POST /contents/{contentId}/directlinks` and is a Premium-adjacent feature [SRC].

#### The API host can be blocked while uploads still work — [LIVE]

While testing the snippet below, `api.gofile.io` stopped answering entirely:

```bash
curl https://api.gofile.io/servers
# -> HTTP 000, connect timeout after 20 s (no TCP connection established)

curl https://gofile.io/
# -> HTTP 000, timeout as well

curl https://store-eu-par-4.gofile.io/    # storage host
# -> HTTP 200
```

So the **API host and the main site became unreachable while the storage hosts stayed up**, and uploads kept succeeding — HTTP 200, both anonymously and with a cached token:

```json
{"data":{"code":"DUk6gBqP","downloadPage":"https://gofile.io/d/Duw9xCJo",
 "guestToken":"7blqNO9I7Sur85AvwgVFDofHDsaez6Mr", ...},"status":"ok"}
```

This is almost certainly the IP-level block Gofile's FAQ warns about, triggered by my ~10 rapid API calls [DOC]: *"If limits are repeatedly exceeded, the originating IP address may be temporarily blocked."* I could not re-test from a different IP, so I cannot fully distinguish a targeted block from a transient outage of `api.gofile.io` — but either way the operational conclusion is the same and it is important:

> **Uploads do not require `api.gofile.io`. Only server discovery and account creation do.** A DCS adapter should therefore cache the chosen store hostname (not just the token) and fall back to a hard-coded list of known store hosts, so a rate-limit block on the API host does not break uploads.

### 1.2 Limits, expiry and rate limits

- **Rate limiting is aggressive and IP-wide — [LIVE].** After roughly ten `/accounts` + `/servers` calls in ~5 minutes, **both** endpoints returned HTTP **429**:
  ```json
  {"status":"error-rateLimit","data":{}}
  ```
  It persisted across repeated retries over several minutes. Gofile's FAQ [DOC]: *"Yes, per endpoint — exceeding them returns HTTP 429 responses"* and *"If limits are repeatedly exceeded, the originating IP address may be temporarily blocked."*
  **Design requirement:** create the guest account **at most once**, persist `token` + `rootFolder` under `~/.config/dcs/`, cache the server name, and implement exponential backoff on 429.
- **Retention:** free storage is explicitly *temporary*; the FAQ describes "ephemeral storage" where expired content is moved to **cold storage** and is not downloadable until re-imported, and *re-importing requires Premium* [DOC]. Exact free retention days are **not published** [DOC]. `/contents/{id}/update` accepts an `expiry` attribute [SRC].
- **Traffic:** monthly allowance; **free traffic is counted by IP address**, so on a shared/NAT/VPN IP other users' downloads count against you; exceeding it temporarily restricts *downloads* [DOC].
- **Max file size:** not published in the FAQ or API docs [DOC]. No limit was hit at small sizes. Treat as "large, but unpublished and changeable".
- The `zone` parameter selects the upload proxy region [SRC].

### 1.3 Python 3.14 (`requests` only)

```python
import pathlib
import requests

API = "https://api.gofile.io"

class GofileUploader:
    """Upload a file to gofile.io and return a public download page URL."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self._server: str | None = None

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers["User-Agent"] = "DCS/0.1 (+https://github.com/ducky-can-shoot)"
        return s

    #: used when api.gofile.io is unreachable or rate-limiting (see §1.2).
    #: Uploads work on a store host even while the API host is blocked.
    FALLBACK_SERVERS = ("store-eu-par-4", "store-na-phx-4", "store-eu-par-8")

    def _pick_server(self, s: requests.Session) -> str:
        if self._server:
            return self._server
        try:
            r = s.get(f"{API}/servers", timeout=15)
            r.raise_for_status()
            servers = r.json()["data"]["servers"]
            # prefer a European store for a European DCS user, else first
            eu = [x["name"] for x in servers if x.get("zone") == "eu"]
            self._server = (eu or [x["name"] for x in servers])[0]
        except (requests.RequestException, KeyError, ValueError):
            # API host down / rate-limited: fall back to a known store.
            self._server = self.FALLBACK_SERVERS[0]
        return self._server

    def upload(self, path: str | pathlib.Path, progress=None) -> str:
        path = pathlib.Path(path)
        s = self._session()
        server = self._pick_server(s)

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        size = path.stat().st_size
        sent = 0

        with path.open("rb") as fh:
            if progress is None:
                files = {"file": (path.name, fh, "application/zip")}
                r = s.post(f"https://{server}.gofile.io/contents/uploadfile",
                           headers=headers, files=files, timeout=(30, 1800))
            else:
                # stream manually so we can report progress
                def reader():
                    nonlocal sent
                    while chunk := fh.read(262144):
                        sent += len(chunk)
                        progress(sent, size)
                        yield chunk
                files = {"file": (path.name, reader(), "application/zip")}
                r = s.post(f"https://{server}.gofile.io/contents/uploadfile",
                           headers=headers, files=files, timeout=(30, 1800))

        if r.status_code == 429:
            raise RuntimeError("gofile rate limit hit (429); retry later")
        r.raise_for_status()
        body = r.json()

        if body.get("status") != "ok":
            raise RuntimeError(f"gofile upload failed: {body}")

        data = body["data"]
        # Persist this: it lets later uploads reuse the same guest account.
        self.token = data.get("guestToken") or self.token
        return data["downloadPage"]        # e.g. https://gofile.io/d/Ve5YurEl
```

**Parsing the link:** `response["data"]["downloadPage"]`. Do **not** synthesise a URL from `id` — the working link uses the folder `code`.

### 1.4 Risk note

Unofficial-but-real API (gofile publishes an API page; the endpoints are stable and widely used). The **premium-isation of read endpoints is the live risk**: it happened without notice, so treat `downloadPage` as the only contract you rely on. Files are **public to anyone with the link** — DCS uploads forensic archives of a USB drive, so the link is the only secret. Recommend a password-protected ZIP or explicit user confirmation (DCS already has `confirm_before_upload` in `GeneralConfig`). Rate limits are per-IP and can lock out a whole NAT.

---

## 2. SwissTransfer (Infomaniak) — unofficial, now gated

**Headline: the API that every third-party client targets no longer exists.** SwissTransfer rebuilt its frontend; the live API is a new, versioned `/api/1/...` surface.

### 2.1 What the third-party sources say — and why it is stale [SRC]

The Go CLI (`Blutsh/swissfer`), the Android client (`Infomaniak/android-SwissTransfer`) and the pyload-ng `SwisstransferComFolder` plugin all describe the older flow: `POST /api/container`, `POST /api/container/{uuid}/uploadChunk`, `GET /api/anonymous/user`, 5 MiB chunks. **I tested those paths live and they are gone — [LIVE]:**

```bash
curl -X POST https://www.swisstransfer.com/api/container       # -> HTTP 404
curl -X POST https://www.swisstransfer.com/api/anonymous/user  # -> HTTP 404
# {"message":"Object not found","errorCode":404}
```

Every one of `/api/container`, `/api/anonymous/user`, `/api/user/anonymous`, `/api/authorization`, `/api/config`, `/api/user` returned **404 `Object not found`**. Do not implement from those sources.

### 2.2 Bot filter on User-Agent — [LIVE]

Without a browser `User-Agent` every request is rejected:

```json
{"message":"Access denied","errorCode":"403"}    # HTTP 403
```

With a normal Chrome UA the same paths return a proper `404 Object not found`. **DCS must send a realistic browser User-Agent.**

### 2.3 The real current API — [DOC: extracted from the live production bundle]

I downloaded SwissTransfer's live frontend bundles (`/build/assets/app-*.js`, `store-*.js`, `container-*.js` — all HTTP 200) and extracted the generated route table. The client is auto-generated from an OpenAPI spec, so the `.definition={methods:[...],url:...}` objects are the **authoritative endpoint list**:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/1/transfers` | create the transfer (container) |
| `POST` | `/api/1/transfers/{transfer}/files/{file}` | register a file inside the transfer |
| `POST` | `/api/1/transfers/{transfer}/files/{file}/chunks/{chunk}` | upload one chunk |
| `PATCH` | `/api/1/transfers/{transfer}/files/{file}` | finalise a file |
| `PATCH` | `/api/1/transfers/{transfer}` | finalise the transfer |
| `DELETE` | `/api/1/transfers/{transfer}` | delete |
| `GET` | `/api/1/altcha-challenge` | **anti-bot proof-of-work challenge** |
| `POST` | `/api/1/email-validations` | start email verification |
| `POST` | `/api/1/email-validations/{uuid}/confirm` | confirm with emailed code |
| `GET` | `/api/1/auth` | OAuth redirect to `login.infomaniak.com` |

All are relative to `https://www.swisstransfer.com`.

**Important:** `{transfer}` and `{file}` are **client-generated UUIDs** — the request builder reads `e.transfer.uuid` / `e.file.uuid`, and the code path creates the file UUID locally. You choose the UUID; you are not given one.

### 2.4 The transfer body — [LIVE validation probing + DOC]

From the bundle, the client's default form state is:

```javascript
{method:"link", title:"", recipients:[], email:"", message:"",
 password:"", language:t, max_download:250, files:[],
 altcha:"", email_validation_id:""}
```

I drove this live and used the server's own validation errors to recover the required schema:

```bash
curl -X POST https://www.swisstransfer.com/api/1/transfers \
  -H "User-Agent: Mozilla/5.0 ... Chrome/131.0.0.0 Safari/537.36" \
  -H "Content-Type: application/json" \
  -H "Origin: https://www.swisstransfer.com" \
  -d '{"method":"link","title":"","recipients":[],"email":"a@b.c","message":"",
       "password":"","language":"en","max_download":250,"expires_in_days":30,
       "files":[{"path":"probe.txt","size":56}],"altcha":"","email_validation_id":""}'
```

First attempt (empty body) → HTTP 422:

```json
{"result":"error","error":{"code":"validation_failed","description":"Validation failed",
 "errors":[
  {"code":"validation_rule_required","description":"The email field is required.","context":{"attribute":"email"}},
  {"code":"validation_rule_required","description":"The expires in days field is required.","context":{"attribute":"expires_in_days"}},
  {"code":"validation_rule_required","description":"The files field is required.","context":{"attribute":"files"}}]}}
```

**`files` is an array of objects with `path` and `size` — [LIVE, confirmed by elimination].** Passing `"files":[{"path":"probe.txt","size":56}]` clears every `files.*` error. Passing a list-of-lists such as `[["probe.txt",56]]` produces `"The files.0 field must be an array"` plus `path`/`size` required errors. The final remaining error was:

```json
{"code":"validation_rule_verified_email","description":"validation.verified_email_check_failed",
 "context":{"attribute":"email"}}
```

### 2.5 Two hard gates

**1. Verified sender email — [LIVE].** `email` is required and must be a *verified* address. The browser flow is [DOC]:

```javascript
const e = await Te(Yt(), {body: {email: I.email}});   // POST /api/1/email-validations
if (e.result === "success") { L("email_validation_id", e.data.id); /* show email_confirmation */ }
```

so: `POST /api/1/email-validations {"email": ...}` → returns `{result:"success",data:{id}}` → the user must complete the emailed confirmation, which yields the `email_validation_id`. The UI caches this (it calls a "remembered email" helper on mount), so **it is a one-time-per-user round trip, not per upload.** Rate-limit codes `email_validation_rate_limit_error` and `email_validation_request_locked` exist [DOC].

Note: the endpoint's own field is `email` (the bundle's call site sends `{email: I.email}`), but its validator reports the attribute as `target_email` — sending both, or an address it considers undeliverable, yields HTTP 422 `validation_rule_email "The target email must be a valid email address"`. Expected, since I deliberately used a non-deliverable address and will not mail a stranger.

**2. ALTCHA proof-of-work — [LIVE].**

```bash
curl https://www.swisstransfer.com/api/1/altcha-challenge
```

HTTP 200:

```json
{"algorithm":"SHA-256",
 "challenge":"a81bd8f4ff6e5c70ea6bfb94fef83eb9df9fed1f80086792b28315f700b39409",
 "maxNumber":1000000,
 "salt":"35ae0a0ee5a1a2e35fe7ce1c?expires=1791911594&",
 "signature":"6b995f6b80cca9fdc4e70a12bac8d5b7c7ea7dbc7b6033abec410b01468a70b6"}
```

ALTCHA is client-side hashcash: find `n` in `[0, maxNumber]` such that `sha256(salt + n) == challenge`, then submit the solution as the `altcha` field. Whether it is **mandatory** for anonymous transfers is **unverified** — I never reached that stage, because the verified-email gate fires first. Solving it in Python is easy (`hashlib` loop); the real blocker is the email.

### 2.6 Retention, link format and sizes

- **Allowed `expires_in_days` for a free/anonymous user: `[1, 3, 7, 15, 30]`** — [DOC]. The bundle defines `Je('expiry_days',[1,3,7,15,30])` as the allowed set and `[7,15,30,60]` as a separate tier set (60 days is paid). So **30 days is the anonymous maximum**, matching the brief.
- **Public link:** `https://www.swisstransfer.com/d/<linkUUID>`. The bundle consistently names the share identifier `link` (field `method:"link"`, `downloadUrl`). **[INFERRED]** — the exact response field carrying it could not be observed because the flow is gated. Confirm against a real response before shipping.
- **`max_download`** defaults to 250.
- **~50 GB max / 30-day anonymous retention:** 30 days is confirmed above; **the 50 GB figure is unverified from any live source** [INFERRED].
- **Chunk size:** the bundle proves chunking exists (`.../chunks/{chunk}`) but **no chunk size constant was recoverable** [INFERRED]. The old 5 MiB value came from the dead API and must not be assumed.

### 2.7 Risk note

**Highest risk of the set.** No official API, no ToS permission for third-party clients, endpoints reverse-engineered from an obfuscated bundle, and the gates (verified email + ALTCHA) exist specifically to stop automation — working around ALTCHA may violate their terms. The API changed once already and broke every client. Files are public to anyone with the `/d/` link. **Implement last, and only if the user accepts the email-verification UX.**

---

## 3. pixeldrain.com — no longer anonymous

**The premise in the brief is out of date: anonymous upload is gone.**

### 3.1 Live result — [LIVE]

```bash
curl -X PUT -T probe.txt https://pixeldrain.com/api/file/probe.txt
```

HTTP **401**:

```json
{"success":false,"value":"authentication_required",
 "message":"This request requires API authentication. Please provide an API key in the password field of HTTP Basic Access Authentication"}
```

The official docs confirm it verbatim [DOC]:

> "All methods which create, modify or delete a resource require an API key."

and the documented error for this endpoint is literally *"authentication_required 401 — No API key was provided, **uploading requires an account**"*. There is no anonymous path.

### 3.2 Correct usage (with a key)

```bash
curl -T "archive.zip" -u :5f45f184-64bb-4eaa-be19-4a5f0459db49 \
     https://pixeldrain.com/api/file/archive.zip
```

- **Basic auth**, API key in the **password** field; username is ignored.
- `PUT /api/file/{name}` uploads raw bytes (the documented "curl example" form). A `POST` multipart variant exists: sending multipart to the `PUT` endpoint yields `invalid_content_type 400 — You sent multipart form data, use the POST endpoint for form uploads` [DOC].
- **Download URLs:** `https://pixeldrain.com/u/{id}` (page) and `https://pixeldrain.com/api/file/{id}?download` (direct bytes).
- **Errors:** `file_too_large 413` (limits are per plan and not published numerically), `ip_rate_limit_reached 429`, `read_only_mode_enabled 503`, `user_out_of_space 400`, `account_banned 403` [DOC]. Free users also face `max_concurrent_downloads`, `transfer_limit_exceeded`, `download_limit_exceeded` and captcha prompts on download [DOC].
- **Expiry:** not exposed at upload; files persist per account settings [DOC].

### 3.3 Python

```python
import pathlib
import requests

def pixeldrain_upload(path: str | pathlib.Path, api_key: str) -> str:
    path = pathlib.Path(path)
    url = f"https://pixeldrain.com/api/file/{path.name}"
    with path.open("rb") as fh:
        r = requests.put(url, data=fh, auth=("", api_key), timeout=(30, 3600))
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"pixeldrain: {body.get('value')}: {body.get('message')}")
    return f"https://pixeldrain.com/u/{body['id']}"
```

**Risk note:** clean, well-documented JSON API and an honest error schema — technically the nicest of the set. But it is **not free/anonymous**: it needs a per-user API key, so DCS can only offer it as an opt-in "bring your own key" provider with `needs_key=True`. Files are public with the link.

---

## 4. 0x0.st — uploads are DISABLED

### 4.1 Live result — [LIVE]

```bash
curl -F 'file=@probe.txt' https://0x0.st
```

HTTP **503**, body:

```
uploads disabled because it's been almost nothing but AI botnet spam for the past few months.
will be back with a few changes at some point. no ETA.
```

The site itself (GET `https://0x0.st`) still returns HTTP 200 with its usual ASCII banner, so it *looks* alive — but **every upload is rejected**. It has been closed for months with no ETA. **Do not implement 0x0.st.** Even if it returns, it historically required a non-empty `User-Agent` and enforced size-dependent retention.

### 4.2 Working replacement: x0.at — [LIVE]

`x0.at` runs the same open-source 0x0 server software (it links the upstream GitHub project).

```bash
curl -F 'file=@probe.txt' https://x0.at/
```

HTTP 200, response body is the **plain-text URL** (not JSON):

```
https://x0.at/hcr6.txt
```

- Max size **1024 MiB** [DOC].
- Retention **3 to 100 days**, size-dependent, per the site's own formula [DOC]:
  `MIN_AGE + (MAX_AGE - MIN_AGE) * (1 - (size / MAX_SIZE))**2` — small files live longest.
- Optional fields: `keep_name=1` (keep the sanitised filename in the link) and `id_length=<n>` (up to 24) [DOC].
- **Content-addressed dedup — [LIVE]:** uploading identical bytes returned the *same* URL both times. Two identical DCS archives collide onto one link; harmless, but do not assume a fresh id per upload.
- Anonymous, no key, no account. Files are public to anyone with the URL.

```python
import pathlib
import requests

def x0at_upload(path: str | pathlib.Path) -> str:
    path = pathlib.Path(path)
    with path.open("rb") as fh:
        r = requests.post("https://x0.at/",
                          files={"file": (path.name, fh)},
                          data={"keep_name": "1"},
                          timeout=(30, 3600))
    r.raise_for_status()
    return r.text.strip()          # plain text URL, e.g. https://x0.at/hcr6.txt
```

---

## 5. tmpfiles.org

### 5.1 Upload — [LIVE]

```bash
curl -F 'file=@probe.txt' https://tmpfiles.org/api/v1/upload
```

HTTP 200:

```json
{"status":"success","data":{"url":"https://tmpfiles.org/wjA7Kl3ZZfQ7/probe.txt"}}
```

Multipart field is `file`. JSON response; `data.url` is the **share page**, not a direct download.

### 5.2 The direct-link rewriting rule HAS CHANGED — [LIVE]

The widely documented rule "replace `tmpfiles.org/` with `tmpfiles.org/dl/`" **is broken**:

```bash
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  https://tmpfiles.org/dl/wTAAKd3kHH7a/probe.txt
# -> 302  https://tmpfiles.org/wTAAKd3kHH7a/probe.txt     (bounces back to the page!)
```

The direct link is now a **signed, time-stamped** URL that only exists inside the page HTML:

```
https://tmpfiles.org/dl/1791911918.8c1ff99d3978f2c3/wTAAKd3kHH7a/probe.txt
```

I extracted it from the page's `href` and fetched it: **HTTP 200, `content-type: application/octet-stream`, 56 bytes** — the real file. So any hard-coded rewriting silently hands users an HTML page instead of the archive. If DCS wants a direct link it must **scrape the page** for `https://tmpfiles\.org/dl/<digits>.<hex>/<id>/<name>`.

For DCS's use case (email a link) the page URL is acceptable — and more robust than scraping.

### 5.3 Limits — [DOC]

- **Max 100 MB** (site header).
- Retention options: **60 minutes / 6 hours / 24 hours / 48 hours**; "All files are deleted after…".
- A file with **no extension is rejected** — [LIVE] HTTP 422 `{"status":"error","message":"Invalid file extension."}` when uploading `/etc/hostname`. **A `.zip` is fine.** Guard against this in the adapter.
- A 14 MB upload succeeded [LIVE]; Cloudflare fronts the service.

```python
import pathlib
import re
import requests

_TMPFILES_DIRECT = re.compile(
    r"https://tmpfiles\.org/dl/[0-9]+\.[a-f0-9]+/[A-Za-z0-9]+/[^\"'<> ]+"
)

def tmpfiles_upload(path: str | pathlib.Path,
                    want_direct: bool = False) -> str:
    path = pathlib.Path(path)
    if not path.suffix:
        raise ValueError("tmpfiles.org rejects files without an extension")
    with path.open("rb") as fh:
        r = requests.post("https://tmpfiles.org/api/v1/upload",
                          files={"file": (path.name, fh)},
                          timeout=(30, 900))
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"tmpfiles: {body}")
    page_url = body["data"]["url"]

    if not want_direct:
        return page_url

    html = requests.get(page_url, timeout=60).text
    m = _TMPFILES_DIRECT.search(html)
    if not m:
        raise RuntimeError("tmpfiles: signed direct link not found in page")
    return m.group(0)
```

**Risk note:** anonymous, no key, trivial API. But the signed-link redesign shows they actively fight direct hotlinking, retention is very short (60 min default), and the 100 MB cap is far too small for a real DCS disk image. Files are public. **Best used as a small-archive fallback, not the default.**

---

## 6. litterbox.catbox.moe — RECOMMENDED SECOND

### 6.1 Upload — [LIVE]

```bash
curl -F 'reqtype=fileupload' -F 'time=1h' -F 'fileToUpload=@probe.txt' \
     https://litterbox.catbox.moe/resources/internals/api.php
```

HTTP 200, body is the **plain-text URL** (not JSON):

```
https://litter.catbox.moe/u492gf.txt
```

- Multipart fields: **`reqtype=fileupload`**, **`time=<1h|12h|24h|72h>`**, **`fileToUpload=@<file>`**.
- I verified **all four** time values work (`1h`, `12h`, `24h`, `72h` all HTTP 200, each returning a fresh URL).
- Invalid `time` → HTTP **412** `No expire time specified.` [LIVE].
- **Max 1 GB**, retention exactly as chosen [DOC]. The site also advertises a 6- or 16-character filename-length option.
- Anonymous, no key, no account, no User-Agent requirement (I sent default `requests`/`curl` UAs successfully).

### 6.2 Related: catbox.moe (permanent) — [LIVE]

```bash
curl -F 'reqtype=fileupload' -F 'fileToUpload=@probe.txt' https://catbox.moe/user/api.php
# -> https://files.catbox.moe/63lswu.txt
```

Same API shape, same plain-text response, but hosts on `files.catbox.moe` and is **permanent**. Limit **200 MB** [DOC]. Worth adding as a "keep forever" option, but the 200 MB cap limits its usefulness for disk images.

```python
import pathlib
import requests

LITTERBOX = "https://litterbox.catbox.moe/resources/internals/api.php"
CATBOX = "https://catbox.moe/user/api.php"

def litterbox_upload(path: str | pathlib.Path, expire: str = "72h") -> str:
    """expire: one of '1h', '12h', '24h', '72h'."""
    if expire not in {"1h", "12h", "24h", "72h"}:
        raise ValueError("litterbox expire must be 1h, 12h, 24h or 72h")
    path = pathlib.Path(path)
    if path.stat().st_size > 1024 ** 3:
        raise ValueError("litterbox limit is 1 GB")
    with path.open("rb") as fh:
        r = requests.post(LITTERBOX,
                          data={"reqtype": "fileupload", "time": expire},
                          files={"fileToUpload": (path.name, fh)},
                          timeout=(30, 3600))
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("https://"):
        raise RuntimeError(f"litterbox: {url}")   # e.g. "No expire time specified."
    return url


def catbox_upload(path: str | pathlib.Path) -> str:
    path = pathlib.Path(path)
    if path.stat().st_size > 200 * 1024 ** 2:
        raise ValueError("catbox limit is 200 MB")
    with path.open("rb") as fh:
        r = requests.post(CATBOX, data={"reqtype": "fileupload"},
                          files={"fileToUpload": (path.name, fh)},
                          timeout=(30, 3600))
    r.raise_for_status()
    return r.text.strip()
```

**Risk note:** long-running, widely used, anonymous, and honest about limits. Files are **public and unencrypted**. 1 GB is the smallest anonymous cap among the recommended hosts, but the API is the simplest to implement correctly — plain text in, plain text out, no JSON schema to drift. Catbox has been known to block certain file types, and abuse reports are acted on.

---

## 7. transfer.sh — DEAD; use temp.sh

### 7.1 Live result — [LIVE]

```bash
curl --upload-file probe.txt https://transfer.sh/probe.txt
# exit code 28 (operation timed out); HTTP 000; no response after 12-20 s
```

`curl -sI https://transfer.sh` likewise never completes. **transfer.sh is unreachable from this machine — do not implement it, and do not recommend it.** Even when it was up it was a single-maintainer service with repeated multi-day outages.

### 7.2 Recommended replacement: temp.sh — [LIVE]

temp.sh describes itself as *"Inspired by transfer.sh"* [DOC].

```bash
curl -F "file=@probe.txt" https://temp.sh/upload
```

HTTP 200, plain-text URL:

```
https://temp.sh/xxqMn/probe.txt
```

- **Syntax gotchas [LIVE]:** it is a **multipart POST to the `/upload` path with field name `file`**. `curl --upload-file` and `POST /` both return **405 Method Not Allowed**. Getting this wrong is the most likely integration bug.
- **Files expire after 3 days**; **current size limit 4 GB** [DOC] — the largest anonymous cap of any provider here.
- The returned URL serves an HTML share page (HTTP 200, 3249 bytes); the file is downloadable from it [LIVE].
- Anonymous, no key. Public to anyone with the URL.

```python
import pathlib
import requests

def tempsh_upload(path: str | pathlib.Path) -> str:
    path = pathlib.Path(path)
    with path.open("rb") as fh:
        r = requests.post("https://temp.sh/upload",
                          files={"file": (path.name, fh)},
                          timeout=(30, 3600))
    r.raise_for_status()
    return r.text.strip()          # https://temp.sh/<id>/<name>
```

**Risk note:** a small, single-operator hobby service (its contact address is a joke domain) — treat availability as best-effort, and there is no published ToS. Files are public.

---

## 8. Recommendation and adapter architecture

### 8.1 Implementation order

1. **gofile.io** — the only provider with a real JSON API *and* a working anonymous tier. Implement first.
2. **litterbox.catbox.moe** — dead-simple plain-text API, 1 GB, user-selectable expiry. Best fallback; different failure domain from gofile.
3. **tmpfiles.org** — trivial, but 100 MB and ~1 hour default. Small archives only.
4. **catbox.moe** — permanent option, 200 MB.
5. **x0.at** — 1 GiB, long retention, plain-text API. Cheap to add.
6. **temp.sh** — 4 GB, 3 days; largest anonymous cap.
7. **pixeldrain.com** — opt-in only, requires the user's own API key.
8. **SwissTransfer** — last; unofficial, needs a verified email + ALTCHA, and breaks without warning.

For DCS specifically, note that **a USB disk image is large**: gofile (unpublished, effectively large) and temp.sh (4 GB) are the only realistic primaries; litterbox caps at 1 GB; tmpfiles and catbox are too small for anything but logs and reports.

### 8.2 Plugin interface

Design goals: a provider is a small object knowing *how* to upload; the pipeline knows nothing about HTTP specifics, matching the existing separation in `dcs/core/events.py` (the pipeline pushes plain dataclasses into an `EventSink` and never touches Qt). Same idea here — **the uploader never touches Qt; it reports progress through a plain callback.**

```python
# src/dcs/core/upload/base.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

#: called as progress(sent_bytes, total_bytes)
ProgressCallback = Callable[[int, int], None]


class UploadError(RuntimeError):
    """Any provider-side failure, normalised for the GUI."""
    def __init__(self, provider: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class UploadResult:
    """Everything the REPORT/EMAIL stages need."""
    provider: str
    url: str                       # the link that goes into the email
    direct_url: str | None = None  # raw bytes, when the host offers one
    expires_at: float | None = None  # unix ts, when known
    size: int = 0
    delete_url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class UploadProvider(Protocol):
    #: stable identifier used in config.toml and the CLI
    name: str
    #: human label for the GUI
    label: str
    #: hard byte limit, None when unpublished
    max_size: int | None
    #: True when the provider needs an API key / credentials
    needs_key: bool
    #: True when the provider's API is unofficial and may break
    unofficial: bool

    def upload(
        self,
        path: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> UploadResult:
        """Upload *path* and return the shareable link.

        Raises UploadError on any failure.
        """
        ...
```

A concrete provider is then a thin module — the shape of every section above collapses into the same template:

```python
# src/dcs/core/upload/providers/litterbox.py
from pathlib import Path

import requests

from ..base import UploadError, UploadResult


class LitterboxProvider:
    name = "litterbox"
    label = "Litterbox (Catbox) — 72 h"
    max_size = 1024 ** 3
    needs_key = False
    unofficial = False

    URL = "https://litterbox.catbox.moe/resources/internals/api.php"

    def __init__(self, expire: str = "72h", api_key: str | None = None) -> None:
        if expire not in {"1h", "12h", "24h", "72h"}:
            raise ValueError("expire must be 1h, 12h, 24h or 72h")
        self.expire = expire

    def upload(self, path: Path, progress_callback=None) -> UploadResult:
        size = path.stat().st_size
        with path.open("rb") as fh:
            r = requests.post(
                self.URL,
                data={"reqtype": "fileupload", "time": self.expire},
                files={"fileToUpload": (path.name, fh)},
                timeout=(30, 3600),
            )
        if r.status_code == 412:
            raise UploadError(self.name, "server rejected the expire time")
        if r.status_code >= 400:
            raise UploadError(self.name, f"HTTP {r.status_code}: {r.text[:200]}",
                              retryable=r.status_code in (429, 500, 502, 503, 504))
        url = r.text.strip()
        if not url.startswith("https://"):
            raise UploadError(self.name, f"unexpected response: {url[:200]}")
        if progress_callback:
            progress_callback(size, size)   # single-shot host: report completion
        return UploadResult(provider=self.name, url=url, size=size)
```

**Registry and config wiring.** Keep a name→class map so a new provider is one file plus one line, and let the TOML config address providers by the same `name`:

```python
# src/dcs/core/upload/__init__.py
from .providers.gofile import GofileProvider
from .providers.litterbox import LitterboxProvider
from .providers.tmpfiles import TmpfilesProvider
from .providers.catbox import CatboxProvider
from .providers.x0at import X0AtProvider
from .providers.tempsh import TempShProvider
from .providers.pixeldrain import PixeldrainProvider

REGISTRY: dict[str, type] = {
    p.name: p for p in (
        GofileProvider, LitterboxProvider, TmpfilesProvider, CatboxProvider,
        X0AtProvider, TempShProvider, PixeldrainProvider,
    )
}


def build(cfg) -> object:
    """Instantiate the configured provider, resolving its secret."""
    cls = REGISTRY[cfg.provider]
    kwargs = {}
    if cls.needs_key:
        kwargs["api_key"] = cfg.resolve_secret()
    return cls(**kwargs)
```

**Design rules that fall out of this research:**

- **`max_size` must be checked before upload.** The archive stage already knows the image size, so DCS should filter the provider list by `max_size` and refuse early rather than uploading 3 GB into a 200 MB limit. For tmpfiles, also enforce a non-empty suffix at the adapter boundary (see §5.3).
- **Never let a provider construct the archive**; `upload()` takes a path and returns an `UploadResult`. That keeps the ARCHIVE and UPLOAD stages independent.
- **Cache credentials between runs.** This is not optional for gofile: naive per-run account creation hits HTTP 429 and can get the user's IP blocked (§1.2). Persist `guestToken` under `default_data_dir()`.
- **Expose `unofficial`, `needs_key` and `max_size` to the GUI** so the provider combo box can annotate ("unofficial API — may break", "requires your API key", "max 200 MB").
- **Map every provider failure to `UploadError(retryable=...)`** and let the pipeline try the next provider in the user's preference list on a retryable failure. Cross-provider fallback is the single highest-value resilience feature here, because three of these services changed or vanished during this research alone.
- **Progress is a callback, not an event loop.** Streaming multipart via a generator (as in the gofile snippet) gives real byte progress for the big-image case; single-shot hosts simply report completion.

### 8.3 Legal / privacy summary

| Provider | Unofficial? | Public files? | Account/key needed? | ToS risk |
| --- | --- | --- | --- | --- |
| gofile.io | Semi-official | Yes | No (auto guest) | Low-moderate; rate-limit blocks |
| SwissTransfer | **Yes** | Yes | **Verified email** | **High** — automation gates, no public API |
| pixeldrain | Official | Yes | **Yes, API key** | Low |
| x0.at | Official | Yes | No | Low |
| tmpfiles.org | Official | Yes | No | Low |
| litterbox / catbox | Official | Yes | No | Low |
| temp.sh | Official-ish | Yes | No | Moderate — hobby service |

**All of these hosts serve files publicly to anyone with the URL.** DCS uploads a forensic image of a USB drive, which may contain personal data and live malware. Two mitigations, both already anticipated by the existing config (`confirm_before_upload`): encrypt or password-protect the archive before upload, and always show the user the exact host and the fact that the link is public before the upload starts.

---

## Appendix: raw verification log

All commands were run on 2026-10-13 from this machine. Status codes and bodies are quoted verbatim in the sections above.

| Check | Command (abridged) | Result |
| --- | --- | --- |
| gofile servers | `GET api.gofile.io/servers` | 200, server list |
| gofile guest | `POST api.gofile.io/accounts` | 200, token + rootFolder |
| gofile upload | `POST {srv}.gofile.io/contents/uploadfile` | 200, downloadPage |
| gofile upload, no token | same, no auth header | 200, guestToken issued |
| gofile folder | `POST api.gofile.io/contents/createfolder` | 200; 400 `error-notOwner` on mismatched token |
| gofile read | `GET api.gofile.io/contents/{id}` | **401 `error-notPremium`** |
| gofile direct DL | `store3.gofile.io/download/{id}/name` | 200 but **SPA HTML**, not the file |
| gofile rate limit | ~10 calls / 5 min | **429 `error-rateLimit`** on both endpoints |
| gofile API host block | `curl api.gofile.io/servers` after the above | **HTTP 000 connect timeout** on `api.gofile.io` *and* `gofile.io`; `store-*.gofile.io` still 200 |
| gofile upload while API blocked | `POST store-eu-par-4.gofile.io/contents/uploadfile` | **200 OK** — uploads survive the API-host block |
| SwissTransfer UA gate | any `/api/...` without browser UA | 403 `Access denied` |
| SwissTransfer old API | `/api/container`, `/api/anonymous/user` | **404 `Object not found`** |
| SwissTransfer new API | route table from live bundle | 9 `/api/1/...` endpoints |
| SwissTransfer transfers | `POST /api/1/transfers` | 422 revealing required `email`, `expires_in_days`, `files` |
| SwissTransfer files shape | `"files":[{"path":..,"size":..}]` | clears all `files.*` errors; only `verified_email` remains |
| SwissTransfer ALTCHA | `GET /api/1/altcha-challenge` | 200, SHA-256 challenge issued |
| pixeldrain anonymous | `PUT /api/file/...` | **401 `authentication_required`** |
| 0x0.st | `POST 0x0.st` | **503 uploads disabled** |
| x0.at | `POST x0.at` | 200, plain-text URL |
| tmpfiles upload | `POST /api/v1/upload` | 200, JSON `data.url` |
| tmpfiles old `/dl/` | `/dl/<id>/<name>` | **302 back to page** (rewrite broken) |
| tmpfiles signed `/dl/` | `/dl/<ts>.<hex>/<id>/<name>` | 200 `application/octet-stream`, real bytes |
| tmpfiles no extension | upload of `/etc/hostname` | 422 `Invalid file extension.` |
| litterbox | `POST .../api.php` for 1h/12h/24h/72h | 200 ×4, plain-text URLs |
| litterbox bad time | `time=999h` | 412 `No expire time specified.` |
| catbox | `POST catbox.moe/user/api.php` | 200, `files.catbox.moe` URL |
| transfer.sh | `curl --upload-file` | **timeout, exit 28, HTTP 000** |
| temp.sh | `POST temp.sh/upload` | 200, plain-text URL; `--upload-file` → 405 |

Test artifacts (a 56-byte text file and a 14 MB text file) were used; nothing larger than a few hundred KB of harmless text was uploaded. No real email address was submitted to SwissTransfer's verification endpoint.
