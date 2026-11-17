# OneX findings API

OneX asks ReelVault for one member's best competitor hooks and formats. Ad studio shows them as "proven hooks".

- Only OneX's server calls this API. A browser never does.
- Each request is signed with `ONEX_SSO_SECRET`. This is the same secret as the one-click sign-in (`/sso/onex`).
- ReelVault returns only the named member's own data. It reads and never writes, apart from the single-use nonce.
- Code: `reelvault/reelvault/server/api_onex.py`. Tests: `reelvault/tests/server/test_onex_api.py`.

## Request

```
GET <REELVAULT_URL>/api/onex/v1/findings
```

There is no query string and no body. The API sets no cookies, reads no cookies and sends no CORS headers.

| Header | Value |
| --- | --- |
| `X-OneX-Email` | The member's OneX email, trimmed and lower-cased. Printable ASCII only; OneX does not call for any other email. |
| `X-OneX-Timestamp` | Unix seconds as a decimal string, e.g. `1763380800`. |
| `X-OneX-Nonce` | 32 lowercase hex characters (16 random bytes). Use each nonce once. |
| `X-OneX-Signature` | `v1=` + base64url without padding (43 characters) of HMAC-SHA256. |
| `Accept` | `application/json` |

## Signing recipe

- **Key:** the UTF-8 bytes of `ONEX_SSO_SECRET`, with surrounding whitespace removed. It must be at least 32 characters.
- **Message:** the UTF-8 bytes of these 6 lines joined by one LF (`\n`, 0x0A). There is no newline at the end.
  1. `onex-api-v1`
  2. `GET`
  3. `/api/onex/v1/findings`
  4. the email, exactly as sent in `X-OneX-Email`
  5. the timestamp, exactly as sent in `X-OneX-Timestamp`
  6. the nonce, exactly as sent in `X-OneX-Nonce`
- **Path line:** always the literal `/api/onex/v1/findings`, whatever base URL or proxy you use.
- **Why `onex-api-v1`:** the first line keeps these signatures apart from SSO tickets, which sign `v1.<payload>`. Neither can be replayed as the other.

### Test vector

The secret below is for tests only. Never use it on a real server.

| Field | Value |
| --- | --- |
| secret | `test-secret-that-is-at-least-32-characters-long` |
| email | `jay@climbranked.test` |
| timestamp | `1763380800` |
| nonce | `0123456789abcdef0123456789abcdef` |
| `X-OneX-Signature` | `v1=iuSXICirPXL1Sa8kel2VgzZQKDH52Yv2Lspnm-nvep8` |

ReelVault checks this vector in `test_contract_test_vector`. OneX checks it in `tests/reelvault.test.ts`.

### Node (OneX, server code only)

```ts
import crypto from "node:crypto";

const FINDINGS_PATH = "/api/onex/v1/findings";

export function signFindingsRequest(secret: string, email: string, now = Date.now(),
                                    nonce = crypto.randomBytes(16).toString("hex")) {
  const ts = String(Math.floor(now / 1000));
  const msg = ["onex-api-v1", "GET", FINDINGS_PATH, email, ts, nonce].join("\n");
  const sig = crypto.createHmac("sha256", secret.trim()).update(msg, "utf8").digest("base64url");
  return {
    "X-OneX-Email": email,
    "X-OneX-Timestamp": ts,
    "X-OneX-Nonce": nonce,
    "X-OneX-Signature": "v1=" + sig,
  };
}

// const res = await fetch(REELVAULT_URL + FINDINGS_PATH, {
//   headers: { ...signFindingsRequest(secret, email), Accept: "application/json" },
//   cache: "no-store", signal: AbortSignal.timeout(3500) });
```

### Python

```python
import base64, hashlib, hmac, secrets, time

def sign_findings_request(secret: str, email: str) -> dict:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    msg = "\n".join(["onex-api-v1", "GET", "/api/onex/v1/findings", email, ts, nonce]).encode("utf-8")
    digest = hmac.new(secret.strip().encode("utf-8"), msg, hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return {"X-OneX-Email": email, "X-OneX-Timestamp": ts, "X-OneX-Nonce": nonce,
            "X-OneX-Signature": "v1=" + sig, "Accept": "application/json"}
```

ReelVault has the same helper as `api_onex.sign(secret, email, ts, nonce)`.

## What ReelVault checks, in order

1. **Rate limit:** 120 requests a minute per client address. Over that gives `429 too_many` with `Retry-After`.
2. **Secret:** `ONEX_SSO_SECRET` must be set and at least 32 characters. Otherwise `503 not_set_up`.
3. **Formats:** the email equals its own trimmed, lower-cased form, looks like an email, is ASCII and is at most 254 characters. The timestamp matches `^[0-9]{1,12}$`. The nonce matches `^[0-9a-f]{32}$`. The signature matches `^v1=[A-Za-z0-9_-]{43}$`. Each pattern must match the whole value, so a trailing newline fails too. Any miss gives `401 bad_signature`.
4. **Clock:** the timestamp must be within 300 seconds of ReelVault's clock, either way. Otherwise `401 stale`.
5. **Signature:** a constant-time compare of the HMAC digest. A mismatch gives `401 bad_signature`.
6. **Single use:** the nonce is counted in `usage_counters` (scope `global`, key `onexapi:<nonce>`, window = the UTC date of the signed timestamp). A second use gives `401 replayed`. This step runs after the signature check, so a stranger cannot use up OneX's nonces. Rows older than 2 days can never match a fresh request, so ReelVault deletes them, at most once an hour.

The pure check is `verify_onex_request(email, ts, nonce, sig_header, secret, now)`. Steps 3 to 5 live there.

## Response 200

Headers: `Content-Type: application/json` and `Cache-Control: no-store`.

```json
{
  "v": 1,
  "status": "ready",
  "email": "jay@climbranked.test",
  "computed_at": "2026-11-17T09:12:03Z",
  "competitors": 4,
  "hooks": [{
    "id": "reel_abcdefghijklmnop",
    "text": "Stop doing cardio if you want to lose belly fat.",
    "on_screen_text": "STOP DOING CARDIO",
    "hook_type": "contrarian",
    "hook_label": "Contrarian",
    "formula": "Stop doing [habit] if you want [result].",
    "why": "It goes against what the viewer believes.",
    "handle": "fitdadcoach",
    "score": 4.2,
    "level": "deep",
    "reel_url": "https://www.instagram.com/reel/XYZ/"
  }],
  "formats": [{
    "id": "fmt_abcdefghijklmnop",
    "name": "Green-screen myth bust",
    "visual_format": "VF04",
    "visual_format_label": "Green screen over image",
    "line": "3.1x more likely to be an outlier · 12 reels from 3 competitors",
    "lift": 3.1,
    "evidence": "strong",
    "can_make": true,
    "reels": 12,
    "competitors": 3
  }]
}
```

### Status

| `status` | Meaning | Lists |
| --- | --- | --- |
| `ready` | At least one hook or format is there. | filled |
| `empty` | The member exists and is active, but nothing beats normal views yet. `competitors` tells you if they still need to add competitors (0) or wait for the first pull (more than 0). | empty |
| `no_account` | No ReelVault member has this email. | empty, `competitors` 0 |
| `off` | The member is pending or turned off. | empty, `competitors` 0 |

### Fields

**Top level**
- `computed_at`: the time of the latest insights snapshot, or `null`.
- `competitors`: how many competitor accounts the member added.

The body has exactly these top-level fields: `v`, `status`, `email`, `computed_at`, `competitors`, `hooks` and `formats`. A new field would come with a new version.

**`hooks`** (at most 12, deep reads first, then highest score; duplicate texts are dropped, ignoring case)
- `id`: the reel id. ReelVault ids look like `<prefix>_<16 base32 characters>`, e.g. `reel_…`.
- `text`: what is said in the first seconds, else the on-screen text. At most 300 characters.
- `on_screen_text`: the on-screen hook. It is `""` when there is none or when it equals `text`. At most 200 characters.
- `hook_type` and `hook_label`: the hook type and its plain label. Either can be `null`. `hook_label` is `null` for types ReelVault has no label for.
- `formula` and `why`: from deep reads only, else `null`. At most 200 and 300 characters.
- `handle`: the competitor's Instagram handle, without `@`.
- `score`: times that account's normal views, 1 decimal. Only reels scoring 2.0 or more are listed.
- `level`: `deep` (a full breakdown) or `quick` (tags only).
- `reel_url`: the Instagram link. It is `null` unless it starts with `https://www.instagram.com/`.

**`formats`** (at most 6 with lift of 1.0 or more, in ReelVault's order)
- `visual_format` and `visual_format_label`: the visual format id (`VF01`–`VF20`, `other`) and its label, or `null`.
- `line`: ReelVault's one-line summary.
- `lift`: how much more likely this format is to be an outlier, 1 decimal.
- `evidence`: `strong`, `promising` or `thin`.
- `can_make`: whether the member can film it, from their profile.
- `reels` and `competitors`: how many reels and competitors the format rests on.

## Errors

Errors use ReelVault's standard body `{"error": "<plain sentence>", "code": "<code>"}` and carry `Cache-Control: no-store`.

| HTTP | `code` | When |
| --- | --- | --- |
| 503 | `not_set_up` | The secret is missing or shorter than 32 characters. |
| 401 | `bad_signature` | A header is missing or malformed, or the HMAC does not match. |
| 401 | `stale` | The timestamp is more than 300 seconds off. |
| 401 | `replayed` | The nonce was used before. |
| 429 | `too_many` | Over 120 requests a minute. Wait for `Retry-After` seconds. |

OneX treats any error, timeout or unexpected body as "ReelVault is unavailable" and simply hides the block.

## Setup

- **This computer:** `ONEX_SSO_SECRET` comes from `%USERPROFILE%\ReelVault-Members\onex-sso-secret.txt`. OneX reads the same value from `.env.local`. OneX calls `http://localhost:8800`.
- **Hosted:** set the same new long random `ONEX_SSO_SECRET` on the ReelVault server and in Vercel. Set `REELVAULT_URL=https://reelvault.scaleitads.com` in Vercel.
- **Clocks:** both servers need a correct clock (within 5 minutes). Hosted servers keep time on their own.
