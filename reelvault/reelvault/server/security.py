"""Security core (DESIGN-SAAS.md 11): passwords, sessions, CSRF, device tokens, key encryption, redact,
client IP, SSRF-safe fetch, and the FastAPI auth dependencies. WP1.

    from reelvault.server.security import current_user, current_admin, extension_device, desktop_device
    @router.get("/api/thing")
    def thing(user = Depends(current_user)): ...          # user is a Row: user.id, user.email, user.role ...

CLI:  python -m reelvault.server.security genkey   -> prints a new RV_MASTER_KEYS entry (kid:base64)
      python -m reelvault.server.security rotate   -> re-encrypts every saved AI key with the current kid
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

from fastapi import Depends, Request, Response
from sqlalchemy import select

from . import db
from .errors import ApiError
from .settings import get_settings

# ============================================================================================== passwords

MIN_PASSWORD = 10
MAX_PASSWORD = 128


@lru_cache(maxsize=1)
def _hasher():
    from argon2 import PasswordHasher

    return PasswordHasher()  # argon2-cffi defaults: argon2id, time_cost 3, 64 MiB, parallelism 4 (11.1)


def hash_password(p: str) -> str:
    return _hasher().hash(p)


def verify_password(h: str, p: str) -> bool:
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    try:
        return _hasher().verify(h, p)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(h: str) -> bool:
    try:
        return _hasher().check_needs_rehash(h)
    except Exception:  # noqa: BLE001
        return False


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    path = Path(__file__).parent / "data" / "common_passwords.txt"
    if not path.exists():
        return frozenset()
    return frozenset(line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def password_problem(p: str) -> str | None:
    """Plain message when a new password is not allowed, else None (11.1)."""
    if not isinstance(p, str) or len(p) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters for your password."
    if len(p) > MAX_PASSWORD:
        return f"Use at most {MAX_PASSWORD} characters for your password."
    if p.lower() in _common_passwords() or len(set(p)) <= 2:
        return "That password is too common. Pick one that is harder to guess."
    return None


# ============================================================================================== hashing, tokens, codes

def sha256_hex(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.sha256(s).hexdigest()


def consteq(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(str(a).encode(), str(b).encode())


def new_token(prefix: str) -> tuple[str, str]:
    """(token, sha256 hex). token = '<prefix>_' + base64url(32 random bytes). Store only the hash."""
    tok = f"{prefix.rstrip('_')}_{base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')}"
    return tok, sha256_hex(tok)


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_code(groups: int = 2, size: int = 4) -> str:
    """Human code in Crockford base32, e.g. '7KQ4-M2XP' (8 chars ~ 40 bits). Used for pairing and invites."""
    return "-".join("".join(secrets.choice(CROCKFORD) for _ in range(size)) for _ in range(groups))


def norm_code(code: str) -> str:
    """Normalize a typed code before hashing: upper case, no spaces/dashes, Crockford look-alikes fixed."""
    c = re.sub(r"[\s\-_]", "", (code or "")).upper()
    return c.translate(str.maketrans({"O": "0", "I": "1", "L": "1", "U": "V"}))


def code_hash(code: str) -> str:
    return sha256_hex(norm_code(code))


# ============================================================================================== secret encryption (11.3)

class KeyUnavailable(Exception):
    """The master key that encrypted this secret is not configured. Member must paste the key again."""

    message = "Your saved key can't be read. Paste it again."


def _keys() -> tuple[tuple[str, bytes], ...]:
    keys = get_settings().master_keys
    if not keys:
        raise KeyUnavailable("RV_MASTER_KEYS is not set.")
    return keys


def encrypt_secret(plain: str, aad: str) -> tuple[bytes, bytes, str]:
    """AES-256-GCM with the current master key. aad = 'user_id|connection_id|provider'. -> (ct, nonce, kid)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    kid, key = _keys()[0]
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plain.encode("utf-8"), aad.encode("utf-8"))
    return ct, nonce, kid


def decrypt_secret(ct: bytes, nonce: bytes, kid: str, aad: str) -> str:
    """Raises KeyUnavailable when the kid is unknown or the ciphertext/AD does not match."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = dict(_keys()).get(kid)
    if key is None:
        raise KeyUnavailable(f"master key {kid!r} is not configured")
    try:
        return AESGCM(key).decrypt(bytes(nonce), bytes(ct), aad.encode("utf-8")).decode("utf-8")
    except InvalidTag:
        raise KeyUnavailable("saved key does not decrypt") from None


def connection_aad(user_id: str, connection_id: str, provider: str) -> str:
    return f"{user_id}|{connection_id}|{provider}"


def genkey(kid: str | None = None) -> str:
    kid = kid or "k" + datetime.now().strftime("%Y%m%d")
    return f"{kid}:{base64.b64encode(os.urandom(32)).decode()}"


def rotate_all() -> int:
    """Re-encrypt every ai_connections key with the current kid. Returns the number re-encrypted."""
    current = _keys()[0][0]
    t = db.ai_connections
    n = 0
    with db.tx(immediate=False) as conn:
        rows = conn.execute(select(t).where(t.c.key_ciphertext.is_not(None), t.c.key_kid != current)).all()
    for r in rows:
        aad = connection_aad(r.user_id, r.id, r.provider)
        try:
            plain = decrypt_secret(r.key_ciphertext, r.key_nonce, r.key_kid, aad)
        except KeyUnavailable:
            continue
        ct, nonce, kid = encrypt_secret(plain, aad)
        with db.tx() as conn:
            conn.execute(t.update().where(t.c.id == r.id).values(key_ciphertext=ct, key_nonce=nonce, key_kid=kid))
        n += 1
    return n


# ============================================================================================== redact

_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), re.compile(r"gsk_[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), re.compile(r"rv[adr]_[A-Za-z0-9_\-]{20,}"),
]


def redact(text: str, secrets_: list[str] | None = None) -> str:
    """Remove the given secrets (and anything that looks like an API key or token) from text."""
    if not text:
        return text
    out = str(text)
    for s in sorted([s for s in (secrets_ or []) if s and len(s) >= 4], key=len, reverse=True):
        out = out.replace(s, "[redacted]")
    for pat in _KEY_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "[redacted]", out)
    return out


# ============================================================================================== client IP (11.6)

def _trusted(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in get_settings().trusted_proxies if net.version == addr.version)


def client_ip(request: Request) -> str:
    """Peer address; X-Forwarded-For is honoured ONLY when the direct peer is in TRUSTED_PROXIES."""
    peer = request.client.host if request.client else "0.0.0.0"
    xff = request.headers.get("x-forwarded-for")
    if not xff or not _trusted(peer):
        return peer
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _trusted(hop):
            try:
                ipaddress.ip_address(hop)
                return hop
            except ValueError:
                return peer
    return hops[0] if hops else peer


# ============================================================================================== safe_fetch (11.8)

class FetchError(Exception):
    """Plain-words reason an outbound fetch failed (message is safe to show)."""


@dataclass
class FetchResult:
    url: str                      # final URL after redirects
    status: int
    headers: dict[str, str]
    content: bytes
    redirects: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        enc = "utf-8"
        m = re.search(r"charset=([\w\-]+)", self.headers.get("content-type", ""))
        if m:
            enc = m.group(1)
        return self.content.decode(enc, errors="replace")


def safe_fetch(url: str, *, max_bytes: int, timeout: float = 30, headers: dict | None = None,
               allow_http: bool = True, allow_private: bool = False, method: str = "GET",
               transport: Any = None) -> FetchResult:
    """SSRF-safe GET: http(s) only, private ranges refused, re-checked on every redirect (max 5), pinned IP,
    10 s connect / `timeout` total, body capped at max_bytes. Raises FetchError with a plain message."""
    import time

    import httpx

    from ..ai.net import PinnedTransport, UnsafeAddress

    tr = transport or PinnedTransport(allow_private=allow_private)
    hdrs = {"User-Agent": "ReelVaultMembers/1.0 (+server fetch)", "Accept": "*/*"}
    hdrs.update(headers or {})
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    cur = url
    with httpx.Client(transport=tr, follow_redirects=False,
                      timeout=httpx.Timeout(timeout, connect=min(10.0, timeout))) as client:
        for _ in range(6):
            p = urlparse(cur)
            if p.scheme not in (("http", "https") if allow_http else ("https",)):
                raise FetchError("That link must start with https://." if not allow_http
                                 else "That link must start with http:// or https://.")
            if not p.hostname:
                raise FetchError("That link has no address.")
            if p.username or p.password:
                raise FetchError("Links with a user name or password are not allowed.")
            try:
                with client.stream(method, cur, headers=hdrs) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        seen.append(cur)
                        cur = urljoin(cur, resp.headers["location"])
                        if len(seen) > 5:
                            raise FetchError("That link redirects too many times.")
                        continue
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > max_bytes:
                        raise FetchError("That page is too big to read.")
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise FetchError("That page is too big to read.")
                        if time.monotonic() > deadline:
                            raise FetchError("That page took too long to load.")
                    return FetchResult(url=cur, status=resp.status_code, headers=dict(resp.headers),
                                       content=bytes(buf), redirects=seen)
            except UnsafeAddress:
                raise FetchError("That link points to a private address. We can't read it.") from None
            except httpx.TimeoutException:
                raise FetchError("That page took too long to load.") from None
            except httpx.HTTPError:
                raise FetchError("We couldn't reach that page.") from None
    raise FetchError("That link redirects too many times.")


# ============================================================================================== sessions (11.2)

SESSION_COOKIE = "rv_session"
SESSION_ABSOLUTE = timedelta(days=30)
SESSION_IDLE = timedelta(days=14)
SEEN_EVERY = timedelta(minutes=5)


def create_session(conn, user_id: str, request: Request | None = None) -> tuple[str, str]:
    """Insert a session row; returns (cookie_value, csrf_token)."""
    value = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    csrf = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    now = db.utcnow()
    conn.execute(db.sessions.insert().values(
        id_hash=sha256_hex(value), user_id=user_id, csrf_token=csrf, created_at=now, last_seen_at=now,
        expires_at=now + SESSION_ABSOLUTE, ip=client_ip(request) if request else None,
        user_agent=(request.headers.get("user-agent", "")[:300] if request else None)))
    return value, csrf


def set_session_cookie(response: Response, value: str) -> None:
    response.set_cookie(SESSION_COOKIE, value, max_age=int(SESSION_ABSOLUTE.total_seconds()), httponly=True,
                        secure=get_settings().cookie_secure, samesite="lax", path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=get_settings().cookie_secure, httponly=True,
                           samesite="lax")


def end_sessions(conn, user_id: str, *, except_hash: str | None = None) -> int:
    q = db.sessions.delete().where(db.sessions.c.user_id == user_id)
    if except_hash:
        q = q.where(db.sessions.c.id_hash != except_hash)
    return conn.execute(q).rowcount


def revoke_everything(conn, user_id: str) -> None:
    """Disabled user (11.1): sessions, device tokens and OAuth tokens revoked; active jobs canceled."""
    now = db.utcnow()
    end_sessions(conn, user_id)
    conn.execute(db.devices.update().where(db.devices.c.user_id == user_id, db.devices.c.revoked_at.is_(None))
                 .values(revoked_at=now))
    conn.execute(db.oauth_tokens.update().where(db.oauth_tokens.c.user_id == user_id,
                                                db.oauth_tokens.c.revoked_at.is_(None)).values(revoked_at=now))
    j = db.jobs
    conn.execute(j.update().where(j.c.user_id == user_id, j.c.state.in_(db.ACTIVE_JOB_STATES)).values(
        state="canceled", finished_at=now, lease_owner=None, lease_until=None, dedupe_done=j.c.dedupe_key,
        dedupe_key=None, error_message="Your account was turned off."))


# ============================================================================================== dependencies

UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
RECONFIRM_EXEMPT = ("/api/me", "/api/auth/reconfirm", "/api/auth/logout")


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    want = get_settings().public_origin
    if origin:
        return origin.rstrip("/") == want
    ref = request.headers.get("referer")
    if ref:
        return ref == want or ref.startswith(want + "/")
    return True  # non-browser clients send neither; the CSRF token still applies


def load_session(request: Request):
    """(session_row, user_row) for the cookie, or (None, None). Applies absolute + idle expiry."""
    value = request.cookies.get(SESSION_COOKIE)
    if not value:
        return None, None
    h = sha256_hex(value)
    now = db.utcnow()
    with db.tx(immediate=False) as conn:
        row = conn.execute(select(db.sessions).where(db.sessions.c.id_hash == h)).first()
        if row is None:
            return None, None
        user = conn.execute(select(db.users).where(db.users.c.id == row.user_id)).first()
    if row.expires_at <= now or row.last_seen_at + SESSION_IDLE <= now or user is None:
        with db.tx() as conn:
            conn.execute(db.sessions.delete().where(db.sessions.c.id_hash == h))
        return None, None
    if now - row.last_seen_at >= SEEN_EVERY:
        with db.tx() as conn:
            conn.execute(db.sessions.update().where(db.sessions.c.id_hash == h).values(last_seen_at=now))
            conn.execute(db.users.update().where(db.users.c.id == user.id).values(last_seen_at=now))
    return row, user


def reconfirm_due(user) -> bool:
    days = int(db.get_setting("reconfirm_days") or 0)
    if days <= 0 or user.role == "admin":
        return False
    last = user.membership_checked_at or user.created_at
    return last + timedelta(days=days) <= db.utcnow()


def current_user(request: Request):
    """Session user (Row). 401 when signed out; 403 when pending/disabled or CSRF/Origin fails; reconfirm gate."""
    path = request.url.path
    if path.startswith("/api/ext/v2/") or path.startswith("/api/worker/"):
        raise ApiError(401, "not_signed_in", "This path needs a device token.")
    sess, user = load_session(request)
    if sess is None:
        raise ApiError(401, "not_signed_in", "Please sign in.")
    if user.status == "pending":
        raise ApiError(403, "forbidden", "Your account is waiting for approval.")
    if user.status != "active":
        raise ApiError(403, "forbidden", "Your account is turned off. Message the community admin.")
    if request.method in UNSAFE_METHODS:
        if not consteq(request.headers.get("x-csrf-token"), sess.csrf_token):
            raise ApiError(403, "forbidden", "Your session is out of date. Refresh the page and try again.")
        if not _origin_ok(request):
            raise ApiError(403, "forbidden", "This request came from another site. Refresh the page and try again.")
    if path not in RECONFIRM_EXEMPT and reconfirm_due(user):
        raise ApiError(403, "reconfirm_needed", "Enter the current community code to keep using ReelVault.")
    from . import ratelimit

    ratelimit.hit("user", user.id, 300, "minute", message="You're going a bit fast. Wait a minute and try again.")
    request.state.user = user
    request.state.session = sess
    return user


def current_admin(user=Depends(current_user)):
    if user.role != "admin":
        raise ApiError(403, "forbidden", "Only the admin can do that.")
    return user


DEVICE_SEEN_EVERY = timedelta(seconds=20)
DEVICE_PER_MINUTE = 600


def current_device(request: Request, kind: Literal["extension", "desktop"]):
    """Device (Row) for 'Authorization: Bearer rvd_...'. Extension tokens only work on /api/ext/v2/*,
    desktop tokens only on /api/worker/* (11.4). 401 for missing/revoked tokens or inactive members."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise ApiError(401, "not_signed_in", "Pair this device with ReelVault again.")
    token = auth[7:].strip()
    with db.tx(immediate=False) as conn:
        dev = conn.execute(select(db.devices).where(db.devices.c.token_hash == sha256_hex(token))).first()
        user = conn.execute(select(db.users).where(db.users.c.id == dev.user_id)).first() if dev else None
    if dev is None or dev.revoked_at is not None or dev.kind != kind or user is None or user.status != "active":
        raise ApiError(401, "not_signed_in", "Pair this device with ReelVault again.")
    now = db.utcnow()
    if dev.last_seen_at is None or now - dev.last_seen_at >= DEVICE_SEEN_EVERY:
        with db.tx() as conn:
            conn.execute(db.devices.update().where(db.devices.c.id == dev.id).values(last_seen_at=now))
    from . import ratelimit

    # 11.6: device calls are limited per device token (not per IP: a whole office can share one IP).
    ratelimit.hit("device", dev.id, DEVICE_PER_MINUTE, "minute",
                  message="This device is calling ReelVault too fast. It will slow down by itself.")
    request.state.device = dev
    request.state.user = user
    return dev


def extension_device(request: Request):
    """Depends(extension_device) for /api/ext/v2/* routes."""
    return current_device(request, "extension")


def desktop_device(request: Request):
    """Depends(desktop_device) for /api/worker/* routes."""
    return current_device(request, "desktop")


def optional_user(request: Request):
    """Session user or None (no CSRF check). For pages like the OAuth consent screen."""
    sess, user = load_session(request)
    if sess is None or user is None or user.status != "active":
        return None
    request.state.session = sess
    return user


# ============================================================================================== CLI

def _main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "genkey":
        print(genkey(argv[1] if len(argv) > 1 else None))
        return 0
    if cmd == "rotate":
        n = rotate_all()
        print(f"Re-encrypted {n} saved keys with the current master key.")
        return 0
    print("usage: python -m reelvault.server.security genkey [kid] | rotate")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
