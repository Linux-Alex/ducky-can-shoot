# SwissTransfer (Infomaniak) — unofficial HTTP API, reverse-engineered

Research date: **2026-10-13** (server `date:` headers on every live call below).
API build advertised by the backend: `x-app-version: 0.69.0`.

Every claim is labelled:

| Label | Meaning |
|---|---|
| **VERIFIED LIVE** | I made the HTTP call and am quoting the real response. |
| **READ FROM SOURCE <url>** | Taken from client source code I fetched and read. |
| **INFERRED** | My deduction; not directly confirmed. |

---

## 0. Bottom line first — the flow in the brief no longer exists

The flow people keep re-posting (`POST /api/anonymous/user` -> bearer token -> `POST /api/container`
-> `POST /api/container/{uuid}/uploadChunk?fileUUID=...&chunkNumber=...`) **is dead**:

* `POST https://www.swisstransfer.com/api/anonymous/user` -> **404** `{"message":"Object not found","errorCode":404}` — **VERIFIED LIVE**
* `POST https://www.swisstransfer.com/api/container` -> **404** (same body) — **VERIFIED LIVE**
* `POST https://www.swisstransfer.com/api/uploadChunk/{c}/{f}/{i}/{last}` -> **302 -> /not_found** — **VERIFIED LIVE**
* `POST https://www.swisstransfer.com/api/uploadComplete` -> **302 -> /not_found** — **VERIFIED LIVE**
* `GET https://www.swisstransfer.com/api/links/{uuid}` -> **302 -> /not_found** — **VERIFIED LIVE**
* `GET .../api/mobile/containers/{c}/files/{f}/chunks/{i}/exists?chunk_size=...` -> **302 -> /not_found** — **VERIFIED LIVE**

There is **no anonymous bearer token and no user UUID** anywhere in the current API. The anonymous
flow is identified by the transfer UUID the server hands back, and uploads are **presigned S3/Ceph
PUT URLs**, not chunk POSTs to swisstransfer.com.

**The current, working anonymous upload flow** (all on `https://www.swisstransfer.com/api/1`):

```
POST /api/1/email-validations                     {email}          -> data.id (code mailed to user)
POST /api/1/email-validations/{id}/confirm        {token:"ABC123"} -> success
POST /api/1/transfers                             {..., files, email, email_validation_id, altcha}
   -> data.id (transferId), data.files[].id (fileIds)
POST /api/1/transfers/{t}/files/{f}                       -> data.url  (file fits ONE chunk)
POST /api/1/transfers/{t}/files/{f}/chunks/{n}            -> data.url  (n = 1..N, multi-chunk)
PUT  <that presigned url>                                  raw bytes -> ETag response header
PATCH /api/1/transfers/{t}/files/{f}   {}  or  {"etags":[{"chunk_index":1,"etag":"\"...\""}]}
PATCH /api/1/transfers/{t}             {"status":"completed"} -> data.link.id + data.link.download_url
Share URL: https://www.swisstransfer.com/dl/{linkUUID}
```

---

## 1. Live verification log (exact commands + real responses)

All calls used a normal desktop Chrome User-Agent. Nothing larger than a few bytes was ever sent, and
no file was ever uploaded.

### 1.1 Dead routes (the ones in the brief)

```bash
curl -s -i -X POST 'https://www.swisstransfer.com/api/anonymous/user' \
     -H 'Content-Type: application/json' --data '{}'
# HTTP/2 404
# {"message":"Object not found","errorCode":404}

curl -s -i -X POST 'https://www.swisstransfer.com/api/container' \
     -H 'Content-Type: application/json' \
     --data '{"duration":30,"numberOfFiles":1,"numberOfFolders":0,"size":10,"title":""}'
# HTTP/2 404
# {"message":"Object not found","errorCode":404}
```

### 1.2 Legacy `/api/containers` (plural) is alive but reCAPTCHA-gated

```bash
curl -s -X POST 'https://www.swisstransfer.com/api/containers' -H 'Content-Type: application/json' \
  --data '{"duration":1,"authorEmail":"","password":"","message":"","numberOfDownload":1,"lang":"en_GB",
           "recipientsEmails":"[]","sizeOfUpload":3,"numberOfFile":1,"recaptcha":"nope",
           "files":"[{\"name\":\"probe.txt\",\"size\":3}]"}'
# -> {"message":"The recaptcha version field is required.","errorCode":400}
# ... adding "recaptchaVersion":"v3" ->
# -> {"message":"The recaptcha version field must be an integer.","errorCode":400}
```
**VERIFIED LIVE.** The route exists, but its upload partner (`/api/uploadChunk`) and finish partner
(`/api/uploadComplete`) are gone, so it is a dead end for a desktop tool.

### 1.3 Mobile endpoints need an app-integrity JWT

```bash
curl -s -i -X POST 'https://www.swisstransfer.com/api/mobile/containers' \
     -H 'Content-Type: application/json' --data '{}'
# HTTP/2 401
# {"message":"missing_jwt","reason":"JWT is missing from request"}
```
**VERIFIED LIVE.** Header name is `Ik-mobile-token` — **READ FROM SOURCE**
`Infomaniak/android-core`, `AppIntegrity/src/standard/kotlin/com/infomaniak/core/appintegrity/AppIntegrityManager.kt`
(`const val ATTESTATION_TOKEN_HEADER = "Ik-mobile-token"`). The token comes from Google Play Integrity
via Infomaniak's device-check service (`AppIntegrityRoutes`: `{BASE}/1/attest/challenge`,
`{BASE}/1/attest/integrity`), so a Python desktop tool **cannot** obtain one.

### 1.4 Session / XSRF bootstrap

```bash
curl -s -i -c cj.txt 'https://www.swisstransfer.com/en'
# set-cookie: SWISSTRANSFER-API-XSRF-TOKEN=JS1RqdvPOSgB6AtDGPRqwq6jnXCDStBDr743hxsF; path=/; domain=swisstransfer.com; secure; samesite=lax
# set-cookie: ST_SESSION=<laravel payload>; ...; httponly
```
**VERIFIED LIVE.** The page is Inertia.js: the whole state sits in
`<script data-page="app" type="application/json">...</script>`, including `"version":"app:0.69.0"`
and the server-side limits (**VERIFIED LIVE**, component `transfer/store`):

```json
"limits": {"ads_st": false, "ads_ksuite": true,
           "expiry_days": [1, 3, 7, 15, 30],
           "download_limit": [1, 20, 100, 250],
           "customization_brand": false, "customization_background_images": 0}
```

### 1.5 `POST /api/1/transfers` — the real anonymous container-creation call

```bash
curl -s -i -X POST 'https://www.swisstransfer.com/api/1/transfers' \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  --data '{"method":"link","title":"","recipients":[],"email":"dsh-probe@example.com","message":"",
           "password":"","language":"en","max_download":3,"expires_in_days":2,
           "files":[{"path":"a.txt","size":3,"mime_type":"text/plain"}],
           "altcha":"","email_validation_id":"00000000-0000-0000-0000-000000000000"}'
# HTTP/2 422
# {"result":"error","error":{"code":"validation_failed","description":"Validation failed","errors":[
#   {"code":"validation_rule_verified_email","description":"validation.verified_email_check_failed","context":{"attribute":"email"}},
#   {"code":"validation_rule_in","description":"The selected expires in days is invalid.","context":{"attribute":"expires_in_days","values":["1","3","7","15","30","60"]}},
#   {"code":"validation_rule_in","description":"The selected max download is invalid.","context":{"attribute":"max_download","values":["1","20","100","250"]}}]}}
```
**VERIFIED LIVE.** **No cookie, no CSRF token and no Authorization header were needed** to reach
validation. Dropping `email` gives
`{"code":"validation_rule_required","description":"The email field is required.","context":{"attribute":"email"}}`
(**VERIFIED LIVE**). The server's own error context is the authoritative list of allowed values.
Response header `x-app-version: 0.69.0`; `x-ratelimit-limit: 500`.

### 1.6 Route existence of every upload step

```bash
curl -s -X POST 'https://www.swisstransfer.com/api/1/transfers/00000000-.../files/11111111-...'
# {"result":"error","error":{"code":"object_not_found","description":"Object not found","context":{"model":"Transfer"}}}
curl -s -X POST 'https://www.swisstransfer.com/api/1/transfers/00000000-.../files/11111111-.../chunks/1'
# same body
curl -s    'https://www.swisstransfer.com/api/1/links/00000000-...'
# {"result":"error","error":{"code":"object_not_found",...,"context":{"model":"Link"}}}
curl -s    'https://www.swisstransfer.com/api/1/links/00000000-.../files/11111111-...?redirect=true'
# {"result":"error","error":{"code":"object_not_found",...,"context":{"model":"Link"}}}
```
**VERIFIED LIVE** (four separate calls): these routes exist on `www.swisstransfer.com/api/1` and only
complain about the fake UUIDs.

### 1.7 Email validation (mandatory before any transfer)

```bash
curl -s -X POST 'https://www.swisstransfer.com/api/1/email-validations' \
     -H 'Content-Type: application/json' --data '{"email":"notanemail"}'
# {"result":"error","error":{"code":"validation_failed","description":"Validation failed","errors":[
#   {"code":"validation_rule_idn_email","description":"Email address is invalid","context":{"attribute":"email"}},
#   {"code":"validation_rule_email_domain_mx","description":"The email email address is invalid.","context":{"attribute":"email"}}]}}

curl -s -X POST 'https://www.swisstransfer.com/api/1/email-validations' \
     -H 'Content-Type: application/json' --data '{"email":"noreply@infomaniak.com"}'
# {"result":"success","data":{"id":"1776aa08-2f18-4160-ae92-e0a7a513b8ad","token_expires_at":1791912666}}
```
**VERIFIED LIVE** on both. `token_expires_at` was **now + 891 s (~15 minutes)**. The domain is
MX-checked, so fake domains are rejected before any mail is sent.

Confirm step (**VERIFIED LIVE**, but with a fake id — see section 9):
```bash
curl -s -X POST 'https://www.swisstransfer.com/api/1/email-validations/00000000-.../confirm' \
     -H 'Content-Type: application/json' --data '{"token":"ABC123"}'
# {"result":"error","error":{"code":"method_not_found","description":"Method not found"}}
```
The legacy confirm route still answers in the old envelope (**VERIFIED LIVE**):
`POST /api/emails-validation {"code":"ABC123","address":"x@example.com"} -> "Invalid code"`.

### 1.8 Altcha challenge (the anti-bot)

```bash
curl -s -i 'https://www.swisstransfer.com/api/1/altcha-challenge'
# HTTP/2 200
# x-ratelimit-limit: 10
# x-ratelimit-remaining: 9
# {"algorithm":"SHA-256","challenge":"15672902f54c5e42e8111d0414c4cd02a268248b659d5e4d563027a657852a03",
#  "maxNumber":1000000,"salt":"7c7037bf68406bdfb4e8d4d8?expires=1791911787&",
#  "signature":"3dfdd8816cc31ffbe1e11e013ebb9f1ac1ee5fd63fe599197d8d76054d0a8a46"}
```
**VERIFIED LIVE.** Standard ALTCHA proof of work (altcha.org): find
`number in [0, maxNumber)` with `SHA256(salt + str(number)) == challenge`. The answer rides in the
`altcha` field of the transfer payload; the web client builds `{algorithm, challenge, number, salt,
signature, took}` — **READ FROM SOURCE** `https://www.swisstransfer.com/build/assets/store-CMMDF7Sr.js`
(build 0.69.0). The exact base64 envelope is **INFERRED** from the ALTCHA spec.

### 1.9 Rate-limit headers observed (**VERIFIED LIVE**)

| Endpoint | `x-ratelimit-limit` |
|---|---|
| `POST /api/1/transfers` | 500 |
| `GET /api/1/links/{link}` | 10 |
| `GET /api/1/altcha-challenge` | 10 |

The web client treats **429 on the presigned PUT** as `RATE_LIMITED` and retries with backoff —
**READ FROM SOURCE** `https://www.swisstransfer.com/build/assets/transfer-Dd7pVxyd.js`.

### 1.10 Share-page paths

```bash
curl -s -i 'https://www.swisstransfer.com/d/00000000-...'            # 302 -> /not_found
curl -s -i 'https://www.swisstransfer.com/dl/00000000-...'           # 404 (route exists, link does not)
curl -s -i 'https://swisstransfer.infomaniak.com/dl/00000000-...'    # 404 (same app behind the api host)
```
**VERIFIED LIVE.** `/dl/{linkUUID}` is the current share path — **READ FROM SOURCE** swissfer
`src/main.rs` (accepts `/d/` and `/dl/`, always requests `/dl/`) and `SharedApiRoutes.shareTransfer`.

---

## 2. Step-by-step: the anonymous upload flow (implementation-ready)

Base: `https://www.swisstransfer.com/api/1` (the same app also answers on
`swisstransfer.infomaniak.com` for authenticated calls).

Common headers on every JSON call:

```
Accept: application/json
Content-Type: application/json
X-Requested-With: XMLHttpRequest      # the web client sends it; harmless
User-Agent: <normal desktop browser UA>
```

Recommended: first `GET https://www.swisstransfer.com/en` with a cookie jar, then send
`Cookie: SWISSTRANSFER-API-XSRF-TOKEN=...; ST_SESSION=...` plus
`X-XSRF-TOKEN: <value of SWISSTRANSFER-API-XSRF-TOKEN>` — **READ FROM SOURCE** swissfer
`src/swissfiles/mod.rs` (`XSRF_COOKIE = "SWISSTRANSFER-API-XSRF-TOKEN"`) and the web bundle
(`"X-XSRF-TOKEN": me() ?? ''`, `credentials: 'include'`). Live probing shows the transfer route
currently accepts calls without any of it, so treat it as belt-and-braces.

### Step 1 — email validation (once per sender address; cache the id)

```
POST /api/1/email-validations
{"email":"you@example.com"}

200 {"result":"success","data":{"id":"<validationUUID>","token_expires_at":<unix ts +900s>}}
```
A 6-character code is emailed. Then:
```
POST /api/1/email-validations/{validationUUID}/confirm
{"token":"A1B2C3"}
```
The confirmed `id` becomes `email_validation_id`. swissfer caches `{email: {id, validated}}` in
`~/.config/swissfer/email-validations.json` and reuses it until the server answers
`verified_email_check_failed` — **READ FROM SOURCE** `src/email.rs`. Expected rate-limit codes:
`email_validation_rate_limit_error`, `email_validation_request_locked`.

### Step 2 — create the transfer

```
POST /api/1/transfers
```
```json
{
  "method": "link",
  "title": "",
  "recipients": [],
  "email": "you@example.com",
  "message": "",
  "password": "",
  "language": "en",
  "max_download": 250,
  "expires_in_days": 30,
  "files": [{"path": "scan.img", "size": 3221225472, "mime_type": "application/octet-stream"}],
  "altcha": "",
  "email_validation_id": "<id from step 1>"
}
```
* The key set is exactly what the live web client builds:
  `{method:'link', title:'', recipients:o, email:'', message:'', password:'', language:t, max_download:250, files:s, altcha:'', email_validation_id:''}`
  — **READ FROM SOURCE** `transfer-Dd7pVxyd.js`.
* `files[].path / size / mime_type` — **READ FROM SOURCE** KMP `TransferFile.kt` and `CreateTransfer.kt`.
* Allowed `expires_in_days` / `max_download` — **VERIFIED LIVE** (section 1.5).

Success response: `{"result":"success","data":{...}}`, `data` = transfer with
`id, title, message, created_at, expires_at, total_size, files:[{id,path,size,mime_type}]` —
**READ FROM SOURCE** KMP `TransferApi.kt`, plus swissfer reading `transfer["id"]` and
`transfer["files"][i]["id"]` (unwrapping `data` when present) and the third-party mcp
`CreateTransferResponse {result, data: TransferData}`.
A successful create could not be exercised live (section 9).

### Step 3 — upload the bytes (per file)

**Case A — one chunk (the web client calls this "direct upload"):**
```
POST /api/1/transfers/{transferId}/files/{fileId}
(no body)

200 {"result":"success","data":{"url":"https://<storage-host>/...?signature=..."}}
```
**READ FROM SOURCE** web bundle (`url:'/api/1/transfers/{transfer}/{file}', method:'post'` then
`(await r.json()).data.url`), swissfer `presign()`, KMP `ApiRoutes.uploadDirectly` +
`PresignedUrlResponse.kt`.

**Case B — multi-chunk, one presigned URL per part, `n` starts at 1:**
```
POST /api/1/transfers/{transferId}/files/{fileId}/chunks/{n}     # n = 1,2,3,...
200 {"result":"success","data":{"url":"https://<storage-host>/..."}}
```
**READ FROM SOURCE** web bundle (`url:'/api/1/transfers/{transfer}/{file}/chunks/{chunk}'`; chunks are
built as `c.push({index: e+1, blob: ...})` -> **1-based**), swissfer (`chunk.index + 1`, comment
"The API numbers parts from one"), KMP `ApiRoutes.uploadChunk`, Android `TransferUploaderV2`
(`val chunkNumber = chunkIndex + 1`).

**PUT the bytes straight to that presigned URL** (different host; send no SwissTransfer headers):
```
PUT <data.url>
Content-Type: application/octet-stream
<body = raw chunk bytes>

-> 2xx with ETag: "<hex>"
```
**READ FROM SOURCE** web bundle: `o.open('PUT', e)`,
`o.setRequestHeader('Content-Type','application/octet-stream')`,
`o.getResponseHeader('etag') || o.getResponseHeader('ETag')`; the client lowercases the ETag and
re-wraps it in quotes before sending it back. swissfer keeps the quotes as returned and notes that S3
rejects the `Expect: 100-continue` handshake (send an empty `Expect:`). iOS V2 reads
`httpResponse.allHeaderFields["Etag"]`.

### Step 4 — finalize each file

```
PATCH /api/1/transfers/{transferId}/files/{fileId}
```
* single-chunk/direct -> body `{}` — **READ FROM SOURCE** web bundle (`body: JSON.stringify({})`) and swissfer (`json!({})`).
* multi-chunk -> body `{"etags":[{"chunk_index":1,"etag":"\"...\""},{"chunk_index":2,"etag":"\"...\""}]}` — **READ FROM SOURCE** web bundle (`e.chunks.map(e=>({chunk_index:e.index, etag:e.etag}))`), swissfer, KMP `ChunkEtag.kt` ("Start with index 1").

### Step 5 — close the transfer and get the link

```
PATCH /api/1/transfers/{transferId}
{"status":"completed"}
-> {"result":"success","data":{"link":{"id":"<linkUUID>","download_url":"https://www.swisstransfer.com/dl/<linkUUID>"}}}
```
`data.link.id` — **READ FROM SOURCE** KMP `CompletionResponse.kt` (`LinkWrapper(id)`) and the mcp;
`data.link.download_url` — **READ FROM SOURCE** swissfer (`create_download_link` prefers
`download_url`, falls back to `https://www.swisstransfer.com/dl/<link.id>`). Other statuses:
`"cancelled"`, `"failed"` (**READ FROM SOURCE** `UploadTransferStatus.kt`).

### Step 6 — share link

```
https://www.swisstransfer.com/dl/<linkUUID>
```
The UUID that makes the share link is the **link** UUID (`data.link.id`) — not the transfer id, and
not the `containerUUID` of the old API. **VERIFIED LIVE** that `/dl/` is the served route (1.10).

---

## 3. Chunk size — the "5 MiB" claim is wrong

| Client | Chunk size | Evidence |
|---|---|---|
| swissfer (Rust CLI, updated 2026) | `CHUNK_SIZE = 52428800` (**50 MiB**) | READ FROM SOURCE `src/swissfiles/swissfile/mod.rs` |
| LeGrizzly/Swisstransfer (legacy flow) | **50 MiB**, `MAX_UPLOAD_SIZE = 50 GiB` | READ FROM SOURCE `src/core/SwissTransferUploader.ts` |
| iOS app (V1 worker) | min/client/server all **50 MiB**, `optimalChunkCount 200`, `maxTotalChunks 10000` | READ FROM SOURCE `TransferManagerWorkerV1.swift` |
| Android app | dynamic: `ceil(size/200)` clamped to **[1 MiB, 50 MiB]**, <=10 000 chunks, 4 parallel | READ FROM SOURCE `workers/FileChunkSizeManager.kt` |
| Web app 0.69.0 | `chunks = max(1, ceil(size / d))`, `d` minified/unresolvable; **5 chunks uploaded in parallel** | READ FROM SOURCE `transfer-Dd7pVxyd.js` |

No client contains 5 MiB. **INFERRED**: 5 MiB is the S3/Ceph multipart *minimum part size*, which is
why it keeps appearing in third-party write-ups. Use **50 MiB**; the rule every client shares is
"one presigned URL + one PUT when `ceil(size/chunk) == 1`, otherwise one per chunk".

---

## 4. Limits, quotas, anti-bot

| Item | Value | Evidence |
|---|---|---|
| Max total upload | **50 GiB** (`53687091200`) | READ FROM SOURCE web bundle `let p = i || 53687091200` |
| Retention, anonymous UI | **1, 3, 7, 15, 30** days | VERIFIED LIVE (Inertia `limits.expiry_days`) |
| Retention, API-accepted | `"1","3","7","15","30","60"` | VERIFIED LIVE (422 error context) — `60` is accepted by the API but not offered to anonymous users |
| Max downloads | **1, 20, 100, 250** | VERIFIED LIVE (422 error context and Inertia limits) |
| Password | free-form string, >=6 chars in the legacy client | READ FROM SOURCE (LeGrizzly); min length not re-verified |
| Email verification | mandatory on every transfer | VERIFIED LIVE |
| Anti-bot | ALTCHA SHA-256 PoW, `maxNumber 1e6`, `/api/1/altcha-challenge` | VERIFIED LIVE |
| Altcha enforcement | **UNCONFIRMED** — `"altcha":""` produced no altcha error (the email check failed first); swissfer reports uploads working while sending an empty `altcha` | INFERRED |
| Rate limits | 500 on `POST /api/1/transfers`; 10 on `GET /api/1/links/{id}`; 10 on `/api/1/altcha-challenge`; the web client retries PUT on 429 | VERIFIED LIVE + READ FROM SOURCE |
| IP heuristics | repeated uploads of the same file get temporarily blocked per IP | READ FROM SOURCE swissfer README ("Known Issues") |

---

## 5. Download / share side

```
GET https://www.swisstransfer.com/dl/{linkUUID}                        # Inertia HTML page (component link/show)
GET /api/1/links/{linkId}/files/{fileId}?redirect=true                 # -> redirect to a presigned storage URL
GET https://{downloadHost}/api/download/{linkUUID}/{fileUUID}?token=...  # legacy direct download
POST /api/1/links/... / POST /api/generateDownloadToken                # password-protected transfers
```
* `/api/1/links/{link}/files/{file}?redirect=true` — READ FROM SOURCE `SharedApiV2Routes.downloadFile`; route existence **VERIFIED LIVE** (`object_not_found`, model `Link`).
* `downloadHost` arrives inside the link payload; the legacy direct pattern is
  `https://{downloadHost}/api/download/{linkUUID}/{fileUUID}` — READ FROM SOURCE pyload-ng
  `src/pyload/plugins/decrypters/SwisstransferComFolder.py`. That plugin still uses the retired
  `api/links/{id}` route, so it is broken today. There is **no** pyload hoster plugin:
  `src/pyload/plugins/hoster/SwisstransferCom.py` -> `404: Not Found`; the PyPI package
  `swisstransfer` -> `404 Not Found`.
* swissfer downloads by scraping the Inertia page `GET /dl/{uuid}` and then
  `GET /api/1/links/{linkId}/files/{fileId}` — READ FROM SOURCE.

---

## 6. All API versions you may meet in the wild

| Version | Base | Auth | Status today |
|---|---|---|---|
| v1 legacy web | `https://www.swisstransfer.com/api/` | reCAPTCHA | `api/containers` alive but recaptcha-gated; `api/container`, `api/uploadChunk`, `api/uploadComplete`, `api/links/{id}` **gone** (VERIFIED LIVE) |
| v1 mobile | `https://www.swisstransfer.com/api/mobile/containers` | `Ik-mobile-token: <Play-Integrity JWT>` | 401 `missing_jwt` without it (VERIFIED LIVE); its chunk/finish routes are gone |
| **v2 anonymous (web)** | `https://www.swisstransfer.com/api/1/` | `email_validation_id` (+ XSRF cookie, altcha) | **the one to implement** |
| v2 authenticated | `https://swisstransfer.infomaniak.com/api/1/` | `Authorization: Bearer <Infomaniak account token>` | same routes plus `accounts/{id}/transfers`; used by the apps and third-party MCP servers (READ FROM SOURCE `BaseRequest.appendBearer`, `ApiRoutes.createTransfer`, alexaltovate/swisstransfer-mcp) |

Guest mode does not exist yet: the Android source carries the TODO "Will we still need that when
guest mode comes to the v2 API?". Anonymous == the email-validated web flow above.

---

## 7. Minimal curl walkthrough

```bash
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
J=/tmp/st-cookies.txt; rm -f $J
API=https://www.swisstransfer.com/api/1

# 0) session + XSRF cookie
curl -s -c $J 'https://www.swisstransfer.com/en' -H "User-Agent: $UA" -o /dev/null
XSRF=$(awk '$6=="SWISSTRANSFER-API-XSRF-TOKEN"{print $7}' $J)

# 1) request a validation code (a 6-char code is mailed)
VID=$(curl -s -X POST "$API/email-validations" -H "User-Agent: $UA" \
        -H 'Content-Type: application/json' -H 'Accept: application/json' \
        --data '{"email":"you@example.com"}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["id"])')

# 2) confirm it
read -r -p 'code: ' CODE
curl -s -X POST "$API/email-validations/$VID/confirm" -H "User-Agent: $UA" \
     -H 'Content-Type: application/json' -H "X-XSRF-TOKEN: $XSRF" -b $J \
     --data '{"token":"'"$CODE"'"}'

# 3) create the transfer
IMG=/path/to/image.img
SIZE=$(stat -c%s "$IMG")
RESP=$(curl -s -X POST "$API/transfers" -H "User-Agent: $UA" -b $J -H "X-XSRF-TOKEN: $XSRF" \
      -H 'Content-Type: application/json' -H 'Accept: application/json' \
      --data '{"method":"link","title":"","recipients":[],"email":"you@example.com","message":"",
               "password":"","language":"en","max_download":250,"expires_in_days":30,
               "altcha":"","email_validation_id":"'"$VID"'",
               "files":[{"path":"image.img","size":'"$SIZE"',"mime_type":"application/octet-stream"}]}')
TID=$(printf '%s' "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["id"])')
FID=$(printf '%s' "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["files"][0]["id"])')

# 4+5) chunked upload, 50 MiB parts, 1-based indexes
CHUNK=52428800; N=$(( (SIZE + CHUNK - 1) / CHUNK )); ETAGS='['
for ((n=1; n<=N; n++)); do
  URL=$(curl -s -X POST "$API/transfers/$TID/files/$FID/chunks/$n" -H "User-Agent: $UA" -b $J \
        -H "X-XSRF-TOKEN: $XSRF" -H 'Accept: application/json' \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["url"])')
  ETAG=$(dd if="$IMG" bs=$CHUNK skip=$((n-1)) count=1 2>/dev/null \
         | curl -s -D - -o /dev/null -X PUT --data-binary @- "$URL" \
                -H 'Content-Type: application/octet-stream' \
         | tr -d '\r' | awk 'tolower($1)=="etag:"{print $2}')
  ETAGS="$ETAGS"'{"chunk_index":'"$n"',"etag":'"$ETAG"'},'
done
ETAGS=$(printf '%s' "$ETAGS" | sed 's/,$/]/')
curl -s -X PATCH "$API/transfers/$TID/files/$FID" -H "User-Agent: $UA" -b $J -H "X-XSRF-TOKEN: $XSRF" \
     -H 'Content-Type: application/json' --data '{"etags":'"$ETAGS"'}'

# 6) close it — the response carries the share link
curl -s -X PATCH "$API/transfers/$TID" -H "User-Agent: $UA" -b $J -H "X-XSRF-TOKEN: $XSRF" \
     -H 'Content-Type: application/json' --data '{"status":"completed"}'
```
Verified live: step 1 (id + expiry), step 3's validation rules and route, the existence of every route
in steps 4-6, and the share path. Not verified live: a successful create/upload/complete end to end.

---

## 8. Python implementation notes

* `requests` + a `requests.Session` (cookie jar) is sufficient; nothing needs HTTP/2 or TLS tricks.
* PUT the presigned URL with a **streaming body** — read one chunk (50 MiB) at a time via
  `f.seek(n*CHUNK); f.read(CHUNK)`; do not load a whole disk image into RAM.
* Chunk indexes are **1-based** both in the URL and in `chunk_index`. This off-by-one fails late and
  confusingly.
* Send the ETag back exactly as received (quoted) or normalized to lowercase-inside-quotes like the
  web client; both working clients do it.
* Retry policy from the reference clients: 3 attempts with exponential backoff per chunk (web),
  up to 40 s of retries (Android), special handling of 429/404/403.
* There is **no v2 "does chunk N exist" endpoint** (the v1 `.../exists?chunk_size=` route is retired —
  VERIFIED LIVE), so a resumed upload must re-POST the presign and re-PUT the chunk; whether the
  server accepts a repeated PUT for an already-complete part is **INFERRED/unverified**.
* Cache the `email_validation_id` per sender address: the code expires in ~15 min, but the validation
  id itself keeps working across runs (swissfer relies on this), which is what makes unattended
  uploads possible.
* Errors come back as one array with `context.attribute` per field — parse that instead of guessing
  field names.

---

## 9. Not confirmed (do not treat as fact)

1. **A successful create/upload/complete** — it needs a 6-character code from a real mailbox. I did
   not register a throwaway inbox. Everything up to the validation rules is live-verified; the
   success shape is source-verified only.
2. **Whether an empty `altcha` is rejected** — the challenge endpoint is live and the web client
   solves it, but my probe failed the email check before altcha was evaluated.
3. **The web app's exact chunk size** — minified variable; no asset contains such a literal. 50 MiB is
   the value used by swissfer, LeGrizzly and the iOS app.
4. **The exact confirm contract** — `method_not_found` for a fake uuid may mean "unknown validation
   id" rather than "bad route"; swissfer and the web bundle agree on route + `token` field.
5. **Maximum file count and per-file size for anonymous transfers** — not exposed in the props I read;
   only the 50 GiB total is known.
6. **`api/isPasswordValid`** — returned `{"message":"Object not found","errorCode":404}`, which in this
   API is ambiguous between "route gone" and "link unknown". Old clients used it; assume gone.
7. `mime_type` may legitimately be an empty string (KMP `TransferFile.kt` default).

---

## 10. Sources fetched and read

* **swissfer** — https://github.com/Blutsh/swissfer : `src/api/mod.rs`, `src/api/chunks/mod.rs`,
  `src/swissfiles/mod.rs`, `src/swissfiles/swissfile/mod.rs`, `src/swissfiles/uploadparameters/mod.rs`,
  `src/email.rs`, `src/main.rs`, `README.md`
* **Infomaniak/multiplatform-SwissTransfer** (KMP core of the Android/iOS apps):
  `STNetwork/.../network/utils/ApiRoutes.kt`, `SharedApiRoutes.kt`, `SharedApiV2Routes.kt`,
  `requests/BaseRequest.kt`, `requests/UploadRequest.kt`, `requests/v2/UploadRequest.kt`,
  `requests/TransferRequest.kt`, `models/upload/request/*`, `models/upload/request/v2/*`,
  `models/upload/response/*`, `models/transfer/v2/*`, `network/ApiClientProvider.kt`,
  `STCommon/.../common/utils/ApiEnvironment.kt`, `ValidityPeriod.kt`, `DownloadLimit.kt`,
  `STCore/.../managers/InMemoryUploadManager.kt`
* **Infomaniak/android-SwissTransfer**: `upload/UploadSessionStarterV1.kt`, `TransferUploader.kt`,
  `TransferUploaderV2.kt`, `workers/FileChunkSizeManager.kt`, `di/SwissTransferInjectionModule.kt`,
  `.gitmodules`
* **Infomaniak/ios-SwissTransfer**: `TransferManagerWorkerV1` (+Network), `TransferManagerWorkerV2` (+Network)
* **Infomaniak/android-core**: `AppIntegrity/src/standard/.../AppIntegrityManager.kt`
  (`ATTESTATION_TOKEN_HEADER = "Ik-mobile-token"`), `AppIntegrityRoutes.kt`
* **Live production web app, build 0.69.0**: `https://www.swisstransfer.com/build/assets/transfer-Dd7pVxyd.js`,
  `store-CMMDF7Sr.js`, `app-Dv4XBC2y.js`, and the Inertia payload of `https://www.swisstransfer.com/en`
* **LeGrizzly/Swisstransfer** — `src/core/SwissTransferUploader.ts`, `SwissTransferDownloader.ts`
  (legacy `api/containers` + `api/uploadChunk` + `api/uploadComplete` flow, historical reference)
* **alexaltovate/swisstransfer-mcp** — `src/api.ts` (third-party Bearer-token use of api/1)
* **pyload-ng** — `decrypters/SwisstransferComFolder.py` exists; `hoster/SwisstransferCom.py` does not (404)
* **PyPI** — package `swisstransfer` does not exist (404)
