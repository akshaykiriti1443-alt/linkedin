"""GET /api/onex/v1/findings: the OneX findings API (server-to-server). Spec: reelvault/docs/onex-findings-api.md.

OneX (app.scaleitads.com) asks for one member's best competitor hooks and formats so Ad studio can offer them.
Only OneX's server calls this; no browser ever does. There are no cookies, no sessions and no CORS here.

Every request is signed with ONEX_SSO_SECRET (the same secret as the /sso/onex ticket):
    X-OneX-Email      the member's email, trimmed and lower-cased
    X-OneX-Timestamp  Unix seconds
    X-OneX-Nonce      32 lowercase hex characters, single use
    X-OneX-Signature  "v1=" + base64url(HMAC-SHA256(secret, message)) without padding
    message = "onex-api-v1\\nGET\\n/api/onex/v1/findings\\n<email>\\n<timestamp>\\n<nonce>"   (UTF-8, no final newline)
The "onex-api-v1" first line keeps these signatures apart from SSO tickets (which sign "v1.<payload>").

Order of checks: rate limit, secret set up, header formats, clock (300 s), HMAC, then the single-use nonce. The nonce
is spent only after the signature checks out, so a stranger cannot burn OneX's nonces.

The answer holds only the named member's own rows. Every helper below is scoped by that user's id. It is read-only:
no jobs are queued and nothing is refreshed.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from .. import insights as ins
from . import api_auth, api_reels, db, ratelimit
from . import jobs_score as js
from .errors import ApiError

router = APIRouter()

PATH = "/api/onex/v1/findings"
PREFIX = "onex-api-v1"
MAX_SKEW_S = 300
MIN_SECRET = 32

MAX_HOOKS = 12
MAX_FORMATS = 6
HOOK_MIN_SCORE = 2.0
IG_PREFIX = "https://www.instagram.com/"

# Always used with fullmatch: in Python "$" also matches before a final "\n", which would let "nonce\n" pass and
# give one nonce two replay keys.
TS_RE = re.compile(r"[0-9]{1,12}")
NONCE_RE = re.compile(r"[0-9a-f]{32}")
SIG_RE = re.compile(r"v1=[A-Za-z0-9_-]{43}")

MSG_BAD = "This request is not signed right."
MSG_STALE = "This request is too old. Check the clock on the OneX server."
MSG_REPLAYED = "This request was already used. Sign a new one."
MSG_NOT_SET_UP = "Sign-in from OneX is not set up yet."
MSG_TOO_MANY = "Too many requests. Wait a minute."

NO_STORE = {"Cache-Control": "no-store"}


# ============================================================================================== signing

def signing_message(method: str, path: str, email: str, ts: str, nonce: str) -> bytes:
    """The exact bytes OneX signs: 6 lines joined by LF, no trailing newline."""
    return "\n".join((PREFIX, method, path, email, ts, nonce)).encode("utf-8")


def sign(secret: str, email: str, ts: str, nonce: str, *, method: str = "GET", path: str = PATH) -> str:
    """The X-OneX-Signature value for these fields (tests and docs; OneX does the same in Node)."""
    mac = hmac.new(secret.strip().encode("utf-8"), signing_message(method, path, email, ts, nonce), hashlib.sha256)
    return "v1=" + base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def _email_ok(email: str) -> bool:
    return (bool(email) and len(email) <= 254 and email.isascii() and email.isprintable()
            and email == api_auth.norm_email(email) and api_auth.EMAIL_RE.fullmatch(email) is not None)


def verify_onex_request(email: str, ts: str, nonce: str, sig_header: str, secret: str, now: float) -> None:
    """Raise ApiError(401, bad_signature | stale) unless the 4 header values are a fresh, valid signature.

    Pure: no database, no clock (pass `now`). The single-use nonce check happens in the route, after this."""
    email, ts, nonce, sig_header = (x if isinstance(x, str) else "" for x in (email, ts, nonce, sig_header))
    if not (_email_ok(email) and TS_RE.fullmatch(ts) and NONCE_RE.fullmatch(nonce) and SIG_RE.fullmatch(sig_header)):
        raise ApiError(401, "bad_signature", MSG_BAD)
    if abs(now - int(ts)) > MAX_SKEW_S:
        raise ApiError(401, "stale", MSG_STALE)
    try:
        got = base64.urlsafe_b64decode(sig_header[3:] + "=" * (-len(sig_header[3:]) % 4))
    except (binascii.Error, ValueError):
        raise ApiError(401, "bad_signature", MSG_BAD) from None
    want = hmac.new(secret.encode("utf-8"), signing_message("GET", PATH, email, ts, nonce), hashlib.sha256).digest()
    if not hmac.compare_digest(got, want):
        raise ApiError(401, "bad_signature", MSG_BAD)


def _spend_nonce(nonce: str, ts: str) -> None:
    """Single use: the counter window is the UTC date of the SIGNED timestamp, so a replay lands in the same row
    even across midnight."""
    window = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")
    used = db.bump(db.usage_counters, {"scope": "global", "key": "onexapi:" + nonce, "window": window}, count=1)
    if used.get("count") != 1:
        raise ApiError(401, "replayed", MSG_REPLAYED)
    api_auth.prune_old_nonces("onexapi:")   # dead nonce rows (windows over 2 days old), at most once an hour


# ============================================================================================== findings

def _cut(v: Any, n: int) -> str:
    return (v if isinstance(v, str) else "").strip()[:n]


def _opt(v: Any, n: int) -> str | None:
    s = _cut(v, n)
    return s or None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return round(float(v), 1)


def _count(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _hooks(user) -> list[dict]:
    rows = api_reels.bank_hooks(format=None, topic=None, min_score=HOOK_MIN_SCORE, level=None, user=user)["hooks"]
    rows = sorted(rows, key=lambda h: (h.get("level") != "deep", -(h.get("score") or 0)))
    out, seen = [], set()
    for h in rows:
        spoken, screen = _cut(h.get("spoken"), 300), _cut(h.get("on_screen_text"), 200)
        text = spoken or screen[:300]
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        ht = _opt(h.get("hook_type"), 40)
        url = h.get("url") if isinstance(h.get("url"), str) and h["url"].startswith(IG_PREFIX) else None
        out.append({"id": h.get("reel_id"), "text": text,
                    "on_screen_text": "" if screen == text else screen,
                    "hook_type": ht, "hook_label": ins.HOOK_LABELS.get(ht) if ht else None,
                    "formula": _opt(h.get("formula"), 200), "why": _opt(h.get("why"), 300),
                    "handle": _cut(h.get("handle"), 60).lstrip("@"), "score": _num(h.get("score")) or 0.0,
                    "level": "deep" if h.get("level") == "deep" else "quick", "reel_url": url})
        if len(out) >= MAX_HOOKS:
            break
    return out


def _formats(user) -> list[dict]:
    out = []
    for f in api_reels.list_formats(can_make=0, user=user)["formats"]:
        lift = f.get("lift")
        if not isinstance(lift, (int, float)) or isinstance(lift, bool) or lift < 1.0:
            continue
        vf = f.get("vf") if isinstance(f.get("vf"), str) and f.get("vf") else None
        cm = f.get("can_make")
        out.append({"id": f.get("id"), "name": _cut(f.get("name"), 120), "visual_format": vf,
                    "visual_format_label": ins.vf_label(vf) if vf else None, "line": _cut(f.get("line"), 200),
                    "lift": round(float(lift), 1), "evidence": f.get("evidence"),
                    "can_make": bool(cm.get("ok", True)) if isinstance(cm, dict) else bool(cm),
                    "reels": _count(f.get("n")), "competitors": _count(f.get("k"))})
        if len(out) >= MAX_FORMATS:
            break
    return out


def findings(email: str) -> dict:
    """The response body for one member (by exact email). Read-only; only that member's rows.
    Exactly the contract's fields (docs/onex-findings-api.md), nothing more."""
    base = {"v": 1, "status": "no_account", "email": email, "computed_at": None, "competitors": 0,
            "hooks": [], "formats": []}
    with db.read() as conn:
        user = conn.execute(select(db.users).where(db.users.c.email == email)).first()
    if user is None:
        return base
    if user.status != "active":
        return {**base, "status": "off"}
    a = db.accounts
    with db.read() as conn:
        competitors = conn.execute(select(func.count()).select_from(a).where(
            a.c.user_id == user.id, a.c.role == "competitor")).scalar() or 0
        snap = js.latest_snapshot(conn, user.id)
    hooks, formats = _hooks(user), _formats(user)
    return {**base, "status": "ready" if hooks or formats else "empty",
            "computed_at": db.iso(snap.computed_at) if snap is not None else None,
            "competitors": int(competitors), "hooks": hooks, "formats": formats}


# ============================================================================================== route

@router.get(PATH)
def get_findings(request: Request):
    try:
        ratelimit.hit("onex_api", ratelimit.client_key(request), 120, "minute", message=MSG_TOO_MANY)
        secret = os.environ.get("ONEX_SSO_SECRET", "").strip()
        if len(secret) < MIN_SECRET:
            raise ApiError(503, "not_set_up", MSG_NOT_SET_UP)
        h = request.headers
        email = h.get("x-onex-email", "")
        ts = h.get("x-onex-timestamp", "")
        nonce = h.get("x-onex-nonce", "")
        verify_onex_request(email, ts, nonce, h.get("x-onex-signature", ""), secret, time.time())
        _spend_nonce(nonce, ts)
        body = findings(email)
    except ApiError as e:
        e.headers = {**e.headers, **NO_STORE}
        raise
    return JSONResponse(body, headers=NO_STORE)
