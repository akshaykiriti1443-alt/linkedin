"""OAuth 2.1 authorization server for the Claude connector (DESIGN-SAAS.md 7.3.2, 3.15, 11.6). WP6.

Same origin as the app. Public clients only (PKCE S256, `token_endpoint_auth_method: none`). Two ways a client gets
a client_id:
  * CIMD: the client_id IS an https URL of a client metadata document (claude.ai uses this). We fetch it through the
    SSRF guard (security.safe_fetch -> ai.net.PinnedTransport), 10 s, <= 64 KB, cached 24 h in oauth_clients.
  * DCR (RFC 7591): POST /oauth/register -> "dcr_..." (Claude Code and older clients).

Routes (router is in app.ROUTERS; MCP itself is mounted by mcp_server.mount):
  GET  /.well-known/oauth-authorization-server     AS metadata (RFC 8414)
  GET  /.well-known/oauth-protected-resource[/mcp] protected resource metadata (RFC 9728), resource = PUBLIC_URL/mcp
  GET  /oauth/authorize                            checks the client, then login redirect or the consent page
  POST /oauth/authorize                            consent form (session + CSRF token) -> code -> redirect
  POST /oauth/token                                authorization_code / refresh_token (form encoding only)
  POST /oauth/register                             DCR
  POST /oauth/revoke                               RFC 7009
  GET  /api/connector/clients, POST /api/connector/disconnect   Settings > AI > Claude: connected apps
  GET  /download/claude-plugin.zip                 the Claude plugin with this server's address (7.3.5)

Tokens are opaque (`rva_` access 1 h, `rvr_` refresh 30 days, `rvc_` codes 5 min), stored only as sha256.
Refresh tokens rotate; reusing a rotated one revokes its whole family. Limits are keyed by client, not by IP
alone (every claude.ai member arrives from Anthropic's egress range).
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from . import db, ratelimit, security
from .errors import ApiError, bad_input, read_body
from .settings import get_settings

log = logging.getLogger("reelvault.oauth")

router = APIRouter()

SCOPE = "reelvault"
SCOPES_SUPPORTED = ("reelvault", "offline_access")
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=5)
CIMD_TTL = timedelta(hours=24)
CIMD_MAX_BYTES = 64 * 1024
CIMD_TIMEOUT = 4.0                  # httpx per-operation timeout inside the CIMD fetch thread
CIMD_WAIT = 5.0                     # hard cap a request waits for a CIMD fetch, however slowly bytes arrive
CIMD_MAX_CONCURRENT = 4             # CIMD fetches run in their own small pool, never in the shared threadpool
CIMD_FAIL_TTL = 180.0               # failed fetches are remembered (negative cache), seconds
CIMD_FETCH_PER_KEY_HOUR = 30        # cache-miss fetches per client (IP or IPv6 /64), before any session lookup
CIMD_FETCH_GLOBAL_HOUR = 300
DCR_UNUSED = timedelta(days=30)
DCR_NEVER_USED = timedelta(hours=24)   # a DCR client never used at /oauth/token is dropped after a day
CLAUDE_CALLBACKS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
LOOPBACK_PATH = "/callback"
# Claude Code uses /callback; the MCP Inspector (12.4 smoke test) uses /oauth/callback and /oauth/callback/debug.
LOOPBACK_PATHS = (LOOPBACK_PATH, "/oauth/callback", "/oauth/callback/debug")
_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9\-_]{43}$")
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
CORS = {"Access-Control-Allow-Origin": "*"}

# 11.6 limits
REGISTER_PER_KEY_HOUR = 30          # per (IP, redirect_uri, client_name); x10 from Anthropic's range
REGISTER_PER_IP_HOUR = 30           # per IP (or IPv6 /64) whatever the name or redirect; x10 from Anthropic's range
REGISTER_GLOBAL_HOUR = 500
TOKEN_PER_KEY_MINUTE = 120          # per (IP, client_id)
TOKEN_GLOBAL_MINUTE = 3000
AUTHORIZE_PER_MEMBER_HOUR = 30


# ============================================================================================== basics

def issuer() -> str:
    return get_settings().public_url.rstrip("/")


def resource_url() -> str:
    return issuer() + "/mcp"


def _test_http_cimd() -> bool:
    """Tests serve client metadata documents on http://127.0.0.1 (12.4). Never set in production."""
    return os.environ.get("RV_TEST_ALLOW_HTTP_CIMD", "").strip() in ("1", "true", "yes")


class OAuthError(Exception):
    """An RFC 6749 error. `redirectable` = may be sent back to the client's redirect_uri."""

    def __init__(self, error: str, description: str, status: int = 400, *, redirectable: bool = True,
                 member: str | None = None):
        super().__init__(f"{error}: {description}")
        self.error, self.description, self.status, self.redirectable = error, description, status, redirectable
        self.member = member  # what a member reads on an error PAGE (plain words); description stays technical

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.description}


# What members read on the consent error page (fix round 1: no developer words, no dead-end loops).
MEMBER_INCOMPLETE = "This link from Claude is not complete. Go back to Claude and press Connect again."
MEMBER_UNKNOWN_APP = "In Claude, remove the ReelVault connector, add it again, then press Connect."
MEMBER_CIMD = "We couldn't check this app with Claude right now. Try again in a minute."


def _oauth_json(body: dict, status: int = 200, *, cors: bool = True, extra: dict | None = None) -> JSONResponse:
    headers = dict(NO_STORE)
    if cors:
        headers.update(CORS)
    if extra:
        headers.update(extra)
    return JSONResponse(body, status_code=status, headers=headers)


def as_metadata() -> dict:
    base = issuer()
    return {"issuer": base, "authorization_endpoint": base + "/oauth/authorize",
            "token_endpoint": base + "/oauth/token", "registration_endpoint": base + "/oauth/register",
            "revocation_endpoint": base + "/oauth/revoke", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True, "scopes_supported": list(SCOPES_SUPPORTED)}


def protected_resource_metadata() -> dict:
    return {"resource": resource_url(), "authorization_servers": [issuer()], "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"], "resource_name": "ReelVault Members"}


# ============================================================================================== redirect URIs

def _loopback(p) -> bool:
    return p.scheme == "http" and (p.hostname or "") in LOOPBACK_HOSTS and p.path in LOOPBACK_PATHS


def redirect_allowed(uri: str) -> bool:
    """7.3.2 step 2: claude.ai / claude.com callbacks, or a loopback /callback (Claude Code). Nothing else."""
    if not isinstance(uri, str) or len(uri) > 300 or "#" in uri:
        return False
    if uri in CLAUDE_CALLBACKS:
        return True
    try:
        p = urlparse(uri)
        _ = p.port  # raises on a bad port
    except ValueError:
        return False
    return _loopback(p) and not p.query and not p.username and not p.password


def redirect_matches(registered: list[str], uri: str) -> bool:
    """Exact match; a loopback URI matches a registered loopback URI with any port (RFC 8252 7.3)."""
    if uri in registered:
        return True
    try:
        p = urlparse(uri)
    except ValueError:
        return False
    if not _loopback(p):
        return False
    for r in registered:
        try:
            q = urlparse(r)
        except ValueError:
            continue
        if _loopback(q) and q.hostname == p.hostname and q.path == p.path and not q.query:
            return True
    return False


def _redirect_with(uri: str, params: dict) -> RedirectResponse:
    sep = "&" if urlparse(uri).query else "?"
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return RedirectResponse(uri + sep + urlencode(clean), status_code=303, headers=NO_STORE)


# ============================================================================================== clients

@dataclass
class Client:
    client_id: str
    kind: str                      # cimd | dcr
    redirect_uris: list[str]
    name: str = ""                 # self-asserted; never shown as the trusted name
    metadata: dict = field(default_factory=dict)

    @property
    def host(self) -> str:
        """What the consent page names: the client_id host for CIMD, else the redirect host."""
        if self.kind == "cimd":
            if self.client_id in KNOWN_CIMD:
                return KNOWN_CIMD[self.client_id]
            p = urlparse(self.client_id)
            if (p.hostname or "") in ("claude.ai", "claude.com"):
                hosts = {urlparse(u).hostname or "" for u in self.redirect_uris}
                return "Claude Code on your computer" if hosts and hosts <= set(LOOPBACK_HOSTS) \
                    else "Claude (claude.ai or the desktop app)"
            return p.hostname or self.client_id
        hosts = {urlparse(u).hostname or "" for u in self.redirect_uris}
        if hosts and hosts <= set(LOOPBACK_HOSTS):
            return "Claude Code on this computer"
        claude = {h for h in hosts if h in ("claude.ai", "claude.com")}
        return sorted(claude)[0] if claude else (sorted(hosts)[0] if hosts else "an app")


# Anthropic's client metadata documents (Claude Code 2.1.3xx signs in with CIMD, not DCR): plain names so the
# consent page and Settings > AI > Connected Claude apps tell them apart (fix round 1).
KNOWN_CIMD = {
    "https://claude.ai/oauth/mcp-oauth-client-metadata": "Claude (claude.ai or the desktop app)",
    "https://claude.ai/oauth/claude-code-client-metadata": "Claude Code on your computer",
}


def is_cimd_id(client_id: str) -> bool:
    if not isinstance(client_id, str) or len(client_id) > 500:
        return False
    p = urlparse(client_id)
    if p.scheme == "https" or (p.scheme == "http" and _test_http_cimd()):
        return bool(p.hostname) and p.path not in ("", "/") and not p.fragment and not p.username \
            and not p.password and "/./" not in p.path and "/../" not in p.path
    return False


def _fetch_cimd(url: str) -> dict:
    """Fetch and check one client metadata document (7.3.2 step 1). Raises OAuthError(invalid_client)."""
    allow_http = _test_http_cimd()
    try:
        res = security.safe_fetch(url, max_bytes=CIMD_MAX_BYTES, timeout=CIMD_TIMEOUT, allow_http=allow_http,
                                  allow_private=allow_http, headers={"Accept": "application/json"})
    except security.FetchError as e:
        raise OAuthError("invalid_client", f"Could not read the client metadata document: {e}",
                         redirectable=False, member=MEMBER_CIMD) from None
    if res.status != 200 or res.redirects:
        raise OAuthError("invalid_client", "The client metadata document must answer 200 without redirects.",
                         redirectable=False, member=MEMBER_CIMD)
    try:
        doc = json.loads(res.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise OAuthError("invalid_client", "The client metadata document is not JSON.", redirectable=False,
                         member=MEMBER_CIMD) from None
    if not isinstance(doc, dict):
        raise OAuthError("invalid_client", "The client metadata document must be a JSON object.", redirectable=False,
                         member=MEMBER_CIMD)
    if doc.get("client_id") != url:
        raise OAuthError("invalid_client", "client_id in the document must equal its URL.", redirectable=False,
                         member=MEMBER_CIMD)
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        raise OAuthError("invalid_client", "The document needs redirect_uris.", redirectable=False,
                         member=MEMBER_CIMD)
    if doc.get("token_endpoint_auth_method") != "none":
        raise OAuthError("invalid_client", "token_endpoint_auth_method must be none.", redirectable=False,
                         member=MEMBER_CIMD)
    keep = {k: doc[k] for k in ("client_id", "client_name", "client_uri", "logo_uri", "redirect_uris",
                                "grant_types", "response_types", "token_endpoint_auth_method", "scope")
            if k in doc}
    return keep


MEMBER_BUSY = "Too many apps are connecting right now. Wait a minute and try again."
_CIMD_POOL = ThreadPoolExecutor(max_workers=CIMD_MAX_CONCURRENT, thread_name_prefix="rv-cimd")
_CIMD_SLOTS = threading.BoundedSemaphore(CIMD_MAX_CONCURRENT)
_CIMD_FAILED: dict[str, tuple[float, OAuthError]] = {}
_CIMD_FAILED_LOCK = threading.Lock()


def _cimd_failed(url: str) -> OAuthError | None:
    with _CIMD_FAILED_LOCK:
        hit = _CIMD_FAILED.get(url)
        if hit is None:
            return None
        if hit[0] < time.monotonic():
            _CIMD_FAILED.pop(url, None)
            return None
        return hit[1]


def _remember_failed(url: str, err: OAuthError) -> None:
    with _CIMD_FAILED_LOCK:
        if len(_CIMD_FAILED) > 5000:     # bounded: drop the expired ones, then the oldest
            now = time.monotonic()
            for k in [k for k, v in _CIMD_FAILED.items() if v[0] < now] or list(_CIMD_FAILED)[:1000]:
                _CIMD_FAILED.pop(k, None)
        _CIMD_FAILED[url] = (time.monotonic() + CIMD_FAIL_TTL, err)


def reset_cimd_guard() -> None:
    """Tests: forget failed fetches."""
    with _CIMD_FAILED_LOCK:
        _CIMD_FAILED.clear()


def _guarded_fetch_cimd(url: str, rate_key: str | None) -> dict:
    """_fetch_cimd behind the fix round 2 guards: a negative cache, per-client and server-wide limits on fetches,
    at most CIMD_MAX_CONCURRENT fetches at once in their own pool (a refused request does not wait), and a hard
    CIMD_WAIT deadline for the caller. A slow or tarpit host can no longer hold the shared request threadpool."""
    cached = _cimd_failed(url)
    if cached is not None:
        raise cached
    busy = OAuthError("temporarily_unavailable", "Too many client metadata fetches. Try again later.", 503,
                      redirectable=False, member=MEMBER_BUSY)
    try:
        limits = [("oauth_cimd_all", "global", CIMD_FETCH_GLOBAL_HOUR)]
        if rate_key:
            limits.insert(0, ("oauth_cimd", rate_key, ratelimit.ip_limit(CIMD_FETCH_PER_KEY_HOUR,
                                                                          rate_key.split(":", 1)[-1])))
        ratelimit.hit_all(None, limits, "hour")
    except ratelimit.TooMany:
        raise busy from None
    if not _CIMD_SLOTS.acquire(blocking=False):
        raise busy

    def run() -> dict:
        try:
            return _fetch_cimd(url)
        finally:
            _CIMD_SLOTS.release()

    try:
        fut = _CIMD_POOL.submit(run)
    except RuntimeError:
        _CIMD_SLOTS.release()
        raise busy from None
    try:
        return fut.result(timeout=CIMD_WAIT)
    except FutureTimeout:
        err = OAuthError("invalid_client", "The client metadata document took too long to load.",
                         redirectable=False, member=MEMBER_CIMD)
        _remember_failed(url, err)
        raise err from None
    except OAuthError as e:
        _remember_failed(url, e)
        raise


def resolve_client(client_id: str, *, refresh: bool = False, rate_key: str | None = None) -> Client:
    """CIMD URL (fetched, cached 24 h) or a registered DCR client. Raises OAuthError(invalid_client).
    rate_key: the requesting client's ratelimit.client_key, so cache-miss fetches are limited per client."""
    if not client_id or not isinstance(client_id, str):
        raise OAuthError("invalid_request", "client_id is missing.", redirectable=False, member=MEMBER_INCOMPLETE)
    t = db.oauth_clients
    with db.read() as c:
        row = c.execute(select(t).where(t.c.client_id == client_id)).first()
    now = db.utcnow()
    if is_cimd_id(client_id):
        fetched = None
        if row is not None and row.kind == "cimd":
            fetched = (row.metadata or {}).get("_fetched_at")
        fresh = False
        if fetched and not refresh:
            try:
                from datetime import datetime

                fresh = now - datetime.fromisoformat(fetched.replace("Z", "+00:00")) < CIMD_TTL
            except ValueError:
                fresh = False
        if fresh:
            md = dict(row.metadata or {})
        else:
            md = _guarded_fetch_cimd(client_id, rate_key)
            md["_fetched_at"] = db.iso(now)
            with db.tx() as c:
                db.upsert(c, t, {"client_id": client_id, "kind": "cimd", "metadata": md,
                                 "redirect_uris": md["redirect_uris"], "created_at": now},
                          update={"metadata": md, "redirect_uris": md["redirect_uris"]})
        return Client(client_id, "cimd", list(md.get("redirect_uris") or []), str(md.get("client_name") or ""), md)
    if row is None or row.kind != "dcr":
        raise OAuthError("invalid_client", "This app is not registered. Connect it again.", redirectable=False,
                         member=MEMBER_UNKNOWN_APP)
    md = dict(row.metadata or {})
    return Client(client_id, "dcr", list(row.redirect_uris or []), str(md.get("client_name") or ""), md)


def _touch_client(conn, client_id: str) -> None:
    conn.execute(db.oauth_clients.update().where(db.oauth_clients.c.client_id == client_id)
                 .values(last_used_at=db.utcnow()))


# ============================================================================================== PKCE, scope, resource

def pkce_s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")


def normalize_scope(scope: str | None) -> str:
    """We grant one scope. Unknown scopes are dropped (RFC 6749 3.3 lets the server narrow the scope)."""
    return SCOPE


def check_resource(resource: str | None) -> str:
    """RFC 8707: the only resource is PUBLIC_URL/mcp (a trailing slash is tolerated)."""
    want = resource_url()
    if resource in (None, ""):
        return want
    if resource.rstrip("/") == want:
        return want
    raise OAuthError("invalid_target", f"This server only issues tokens for {want}.")


# ============================================================================================== tokens

def _family_for_code(code_hash: str) -> str:
    return "fam_" + code_hash[:32]


def _issue(conn, *, client_id: str, user_id: str, family_id: str, resource: str) -> dict:
    now = db.utcnow()
    access, ah = security.new_token("rva")
    refresh, rh = security.new_token("rvr")
    t = db.oauth_tokens
    conn.execute(t.insert().values(token_hash=ah, kind="access", client_id=client_id, user_id=user_id, scope=SCOPE,
                                   resource=resource, family_id=family_id, expires_at=now + ACCESS_TTL,
                                   created_at=now))
    conn.execute(t.insert().values(token_hash=rh, kind="refresh", client_id=client_id, user_id=user_id, scope=SCOPE,
                                   resource=resource, family_id=family_id, expires_at=now + REFRESH_TTL,
                                   created_at=now))
    _touch_client(conn, client_id)
    return {"access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_TTL.total_seconds()),
            "refresh_token": refresh, "scope": SCOPE, "_refresh_hash": rh}


def _revoke_family(conn, family_id: str) -> int:
    t = db.oauth_tokens
    return conn.execute(t.update().where(t.c.family_id == family_id, t.c.revoked_at.is_(None))
                        .values(revoked_at=db.utcnow())).rowcount


RECONFIRM_GRANT = "Enter the current community code in ReelVault first, then connect again."


def _user_active(conn, user_id: str) -> bool:
    u = conn.execute(select(db.users.c.status).where(db.users.c.id == user_id)).first()
    return bool(u and u.status == "active")


def _reconfirm_due(conn, user_id: str) -> bool:
    """11.1 (c): a member whose community code check is due gets no new codes or tokens (fix round 1)."""
    u = conn.execute(select(db.users).where(db.users.c.id == user_id)).first()
    return bool(u is not None and security.reconfirm_due(u))


def grant_authorization_code(form: dict) -> dict:
    code = form.get("code") or ""
    client_id = form.get("client_id") or ""
    redirect_uri = form.get("redirect_uri") or ""
    verifier = form.get("code_verifier") or ""
    if not code or not client_id or not redirect_uri or not verifier:
        raise OAuthError("invalid_request", "code, client_id, redirect_uri and code_verifier are required.")
    if not _VERIFIER.match(verifier):
        raise OAuthError("invalid_grant", "code_verifier is not valid.")
    resource = check_resource(form.get("resource"))
    h = security.sha256_hex(code)
    c_t = db.oauth_codes
    now = db.utcnow()
    replayed = False
    with db.tx() as conn:
        row = conn.execute(select(c_t).where(c_t.c.code_hash == h)).first()
        if row is None:
            raise OAuthError("invalid_grant", "This code is not valid.")
        if row.used_at is not None:
            _revoke_family(conn, _family_for_code(h))     # code replay: revoke what it gave out (RFC 6749 4.1.2)
            replayed = True
    if replayed:   # raised after the commit so the revocation sticks
        raise OAuthError("invalid_grant", "This code was already used.")
    with db.tx() as conn:
        row = conn.execute(select(c_t).where(c_t.c.code_hash == h)).first()
        if row is None or row.used_at is not None:
            raise OAuthError("invalid_grant", "This code was already used.")
        if row.expires_at <= now:
            raise OAuthError("invalid_grant", "This code has expired. Connect again.")
        if row.client_id != client_id or row.redirect_uri != redirect_uri:
            raise OAuthError("invalid_grant", "client_id or redirect_uri does not match the code.")
        if not security.consteq(pkce_s256(verifier), row.code_challenge):
            raise OAuthError("invalid_grant", "code_verifier does not match the code challenge.")
        if (row.resource or resource_url()) != resource:
            raise OAuthError("invalid_target", "resource does not match the code.")
        conn.execute(c_t.update().where(c_t.c.code_hash == h, c_t.c.used_at.is_(None)).values(used_at=now))
        if not _user_active(conn, row.user_id):
            raise OAuthError("invalid_grant", "This ReelVault account is turned off.")
        if _reconfirm_due(conn, row.user_id):
            raise OAuthError("invalid_grant", RECONFIRM_GRANT)
        out = _issue(conn, client_id=client_id, user_id=row.user_id, family_id=_family_for_code(h),
                     resource=resource)
        # A member who has picked no AI yet and connects Claude means "use my Claude plan" (fix round 1): set it
        # and hand the waiting AI jobs to the connector, so step 5 ("Analyze my new ReelVault reels") works.
        mode = conn.execute(select(db.users.c.ai_mode).where(db.users.c.id == row.user_id)).scalar()
        if mode is None:
            from . import work

            conn.execute(db.users.update().where(db.users.c.id == row.user_id, db.users.c.ai_mode.is_(None))
                         .values(ai_mode="claude_connector"))
            work.resume_ai_jobs(row.user_id, conn=conn)
    out.pop("_refresh_hash", None)
    return out


REFRESH_GRACE = timedelta(seconds=60)


def _in_refresh_grace(conn, row, now) -> bool:
    """True when `row` (a rotated refresh token) was rotated less than REFRESH_GRACE ago and the refresh token it
    was rotated into has not been used or revoked yet."""
    rotated = row.revoked_at
    if rotated is None or now - rotated > REFRESH_GRACE:
        return False
    t = db.oauth_tokens
    succ = conn.execute(select(t.c.replaced_by, t.c.revoked_at).where(t.c.token_hash == row.replaced_by)).first()
    return succ is not None and succ.replaced_by is None and succ.revoked_at is None


def grant_refresh(form: dict) -> dict:
    token = form.get("refresh_token") or ""
    client_id = form.get("client_id") or ""
    if not token or not client_id:
        raise OAuthError("invalid_request", "refresh_token and client_id are required.")
    if form.get("resource"):
        check_resource(form.get("resource"))
    h = security.sha256_hex(token)
    t = db.oauth_tokens
    now = db.utcnow()
    with db.tx() as conn:
        row = conn.execute(select(t).where(t.c.token_hash == h, t.c.kind == "refresh")).first()
        if row is None or row.client_id != client_id:
            raise OAuthError("invalid_grant", "This refresh token is not valid.")
        grace = row.replaced_by is not None and _in_refresh_grace(conn, row, now)
        if grace and row.expires_at > now and _user_active(conn, row.user_id) \
                and not _reconfirm_due(conn, row.user_id):
            # The same refresh token again within REFRESH_GRACE of its rotation while the token it was rotated
            # into is still unused: a retry after a lost answer, or two refreshes at once. Hand out a fresh pair
            # in the same family and leave the first answer's tokens working (fix round 2).
            err = None
            out = _issue(conn, client_id=row.client_id, user_id=row.user_id, family_id=row.family_id,
                         resource=row.resource or resource_url())
            out.pop("_refresh_hash", None)
            log.info("refresh token retried within the grace window")
        elif row.replaced_by is not None or row.revoked_at is not None:
            if row.replaced_by is not None:
                n = _revoke_family(conn, row.family_id)    # reuse of a rotated token: kill the chain
                log.warning("refresh token reuse: family revoked (%d tokens)", n)
                db.audit("oauth_reuse", user_id=row.user_id, actor="system",
                         detail={"client_id": row.client_id[:200]}, conn=conn)
            # the revocation must stick even though the request fails: commit, then answer invalid_grant
            err = OAuthError("invalid_grant", "This refresh token was already used or revoked. Connect again.")
        elif row.expires_at <= now:
            err = OAuthError("invalid_grant", "This refresh token has expired. Connect again.")
        elif not _user_active(conn, row.user_id):
            err = OAuthError("invalid_grant", "This ReelVault account is turned off.")
        elif _reconfirm_due(conn, row.user_id):
            err = OAuthError("invalid_grant", RECONFIRM_GRANT)
        else:
            err = None
            out = _issue(conn, client_id=row.client_id, user_id=row.user_id, family_id=row.family_id,
                         resource=row.resource or resource_url())
            conn.execute(t.update().where(t.c.token_hash == h).values(replaced_by=out.pop("_refresh_hash"),
                                                                      revoked_at=now))
    if err is not None:
        raise err
    return out


def verify_access(token: str) -> dict | None:
    """For mcp_server.DbTokenVerifier: {"user_id", "client_id", "expires_at"} or None. Access tokens only, not
    revoked, not expired, bound to PUBLIC_URL/mcp, and the member is active."""
    if not token or not token.startswith("rva_") or len(token) > 200:
        return None
    h = security.sha256_hex(token)
    t, u = db.oauth_tokens, db.users
    now = db.utcnow()
    with db.read() as c:
        row = c.execute(select(t.c.user_id, t.c.client_id, t.c.expires_at, t.c.revoked_at, t.c.resource,
                               u.c.status).join(u, u.c.id == t.c.user_id)
                        .where(t.c.token_hash == h, t.c.kind == "access")).first()
    if row is None or row.revoked_at is not None or row.expires_at <= now or row.status != "active":
        return None
    if (row.resource or resource_url()) != resource_url():
        return None
    return {"user_id": row.user_id, "client_id": row.client_id, "expires_at": int(row.expires_at.timestamp())}


def revoke_token(token: str, client_id: str | None = None) -> bool:
    h = security.sha256_hex(token or "")
    t = db.oauth_tokens
    with db.tx() as conn:
        row = conn.execute(select(t).where(t.c.token_hash == h)).first()
        if row is None or (client_id and row.client_id != client_id):
            return False
        if row.kind == "refresh":
            _revoke_family(conn, row.family_id)
        else:
            conn.execute(t.update().where(t.c.token_hash == h, t.c.revoked_at.is_(None)).values(revoked_at=db.utcnow()))
    return True


# ============================================================================================== housekeeping

_CLEAN = {"at": 0.0}
_CLEAN_LOCK = threading.Lock()


def cleanup(now=None) -> dict:
    """Delete used/expired codes, tokens past expiry (+1 day), and DCR clients unused for 30 days (7.3.2).
    Runs on its own every 10 minutes from the OAuth endpoints; WP1's daily sweep may also call it."""
    now = now or db.utcnow()
    c_t, t, cl = db.oauth_codes, db.oauth_tokens, db.oauth_clients
    day = timedelta(days=1)
    with db.tx() as conn:
        codes = conn.execute(c_t.delete().where(c_t.c.expires_at < now - day)).rowcount
        toks = conn.execute(t.delete().where(t.c.expires_at < now - day)).rowcount
        live = select(t.c.client_id).where(t.c.expires_at >= now)
        stale = conn.execute(cl.delete().where(
            cl.c.kind == "dcr", func.coalesce(cl.c.last_used_at, cl.c.created_at) < now - DCR_UNUSED,
            cl.c.client_id.not_in(live))).rowcount
        # registered but never used at /oauth/token: kept one day, not 30 (fix round 2)
        stale += conn.execute(cl.delete().where(
            cl.c.kind == "dcr", cl.c.last_used_at.is_(None), cl.c.created_at < now - DCR_NEVER_USED,
            cl.c.client_id.not_in(select(t.c.client_id)),
            cl.c.client_id.not_in(select(c_t.c.client_id)))).rowcount
        stale += conn.execute(cl.delete().where(
            cl.c.kind == "cimd", func.coalesce(cl.c.last_used_at, cl.c.created_at) < now - DCR_UNUSED,
            cl.c.client_id.not_in(live))).rowcount
    return {"codes": codes, "tokens": toks, "clients": stale}


def _maybe_cleanup() -> None:
    with _CLEAN_LOCK:
        if time.monotonic() - _CLEAN["at"] < 600:
            return
        _CLEAN["at"] = time.monotonic()
    try:
        cleanup()
    except Exception:  # noqa: BLE001 - housekeeping must never break a request
        log.exception("oauth cleanup failed")


# ============================================================================================== metadata routes

@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
def well_known_as():
    return JSONResponse(as_metadata(), headers={**CORS, "Cache-Control": "public, max-age=3600"})


@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
def well_known_prm():
    return JSONResponse(protected_resource_metadata(), headers={**CORS, "Cache-Control": "public, max-age=3600"})


@router.options("/oauth/token", include_in_schema=False)
@router.options("/oauth/register", include_in_schema=False)
@router.options("/oauth/revoke", include_in_schema=False)
def oauth_preflight():
    return Response(status_code=204, headers={**CORS, "Access-Control-Allow-Methods": "POST",
                                              "Access-Control-Allow-Headers": "Content-Type, Authorization, "
                                                                              "MCP-Protocol-Version",
                                              "Access-Control-Max-Age": "600"})


# ============================================================================================== pages

_STYLE = ("body{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f6f7f9;"
          "color:#15171a}main{max-width:440px;margin:10vh auto;padding:28px;background:#fff;border-radius:14px;"
          "box-shadow:0 2px 16px rgba(0,0,0,.08)}h1{font-size:22px;margin:0 0 12px}p{margin:0 0 12px}"
          ".who{color:#5b6270;font-size:14px}.row{display:flex;gap:12px;margin-top:20px}button{flex:1;"
          "font:inherit;padding:12px;border-radius:10px;border:1px solid #c9ced6;background:#fff;cursor:pointer}"
          "button.go{background:#2f5bea;border-color:#2f5bea;color:#fff;font-weight:600}"
          "@media (prefers-color-scheme:dark){body{background:#111317;color:#e9ebef}main{background:#1b1e24;"
          "box-shadow:none}.who{color:#9aa1ad}button{background:#262a31;color:#e9ebef;border-color:#3a3f48}}")
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()


def _csp(form_target: str | None = None) -> str:
    targets = "'self'"
    if form_target:
        p = urlparse(form_target)
        targets += f" {p.scheme}://{p.netloc}"
    return (f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; form-action {targets}; "
            "frame-ancestors 'none'; base-uri 'none'")


def _page(title: str, body_html: str, *, status: int = 200, form_target: str | None = None) -> HTMLResponse:
    doc = (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
           f"content=\"width=device-width,initial-scale=1\"><title>{html.escape(title)}</title>"
           f"<style>{_STYLE}</style></head><body><main>{body_html}</main></body></html>")
    return HTMLResponse(doc, status_code=status, headers={**NO_STORE, "Content-Security-Policy": _csp(form_target),
                                                           "X-Frame-Options": "DENY"})


def _error_page(message: str, status: int = 400) -> HTMLResponse:
    return _page("Can't connect", f"<h1>We can't connect this app</h1><p>{html.escape(message)}</p>"
                 "<p class=\"who\">Close this tab and try again from Claude. If it keeps happening, "
                 "message the community admin.</p>", status=status)


def _reconfirm_page() -> HTMLResponse:
    return _page("Enter the community code", "<h1>Enter the community code first</h1>"
                 "<p>ReelVault needs the current community code before it connects to Claude.</p>"
                 "<p><a href=\"/#login\">Open ReelVault and enter the code</a>. Then go back to Claude and press "
                 "Connect again.</p>", status=403)


FIELDS = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state", "scope",
          "resource")


@dataclass
class AuthRequest:
    client: Client
    redirect_uri: str
    code_challenge: str
    state: str
    scope: str
    resource: str


def _check_request(params: dict, rate_key: str | None = None) -> AuthRequest:
    """Validate an authorize request. Errors with redirectable=False must be shown as a page (7.3.2 step 2)."""
    client_id = params.get("client_id") or ""
    redirect_uri = params.get("redirect_uri") or ""
    client = resolve_client(client_id, rate_key=rate_key)
    if not redirect_uri:
        raise OAuthError("invalid_request", "redirect_uri is missing.", redirectable=False, member=MEMBER_INCOMPLETE)
    if not redirect_allowed(redirect_uri) or not redirect_matches(client.redirect_uris, redirect_uri):
        raise OAuthError("invalid_request", "This app's return address is not allowed.", redirectable=False,
                         member=MEMBER_INCOMPLETE)
    state = params.get("state") or ""
    if len(state) > 1000:
        raise OAuthError("invalid_request", "state is too long.")
    if params.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "Only response_type=code is supported.")
    challenge = params.get("code_challenge") or ""
    if params.get("code_challenge_method") != "S256" or not _CHALLENGE.match(challenge):
        raise OAuthError("invalid_request", "PKCE with code_challenge_method=S256 is required.")
    resource = check_resource(params.get("resource"))
    return AuthRequest(client, redirect_uri, challenge, state, normalize_scope(params.get("scope")), resource)


def _login_redirect(request: Request) -> RedirectResponse:
    nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
    return RedirectResponse("/#login?next=" + quote(nxt, safe=""), status_code=303, headers=NO_STORE)


def _consent_html(ar: AuthRequest, user, csrf: str, params: dict) -> HTMLResponse:
    host = html.escape(ar.client.host)
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(str(params.get(k) or ""))}">'
                     for k in FIELDS)
    body = (f"<h1>Connect Claude to ReelVault</h1>"
            f"<p><b>{host}</b> wants to use your ReelVault account.</p>"
            "<p>It can read your profile, competitors and reels, and save analyses and scripts. "
            "It can also add competitors and ask your Chrome extension to read their reels, within your daily "
            "limits. It can't see your password or API keys.</p>"
            f"<p class=\"who\">Signed in as {html.escape(user.email)}. You can disconnect it any time in "
            "Settings &gt; AI.</p>"
            f"<form method=\"post\" action=\"/oauth/authorize\">{hidden}"
            f"<input type=\"hidden\" name=\"csrf_token\" value=\"{html.escape(csrf)}\">"
            "<div class=\"row\"><button type=\"submit\" name=\"decision\" value=\"cancel\">Cancel</button>"
            "<button class=\"go\" type=\"submit\" name=\"decision\" value=\"allow\">Allow</button></div></form>")
    return _page("Connect Claude to ReelVault", body, form_target=ar.redirect_uri)


def _authorize_get(request: Request) -> Response:
    params = {k: request.query_params.get(k) for k in FIELDS}
    try:
        ar = _check_request(params, ratelimit.client_key(request))
    except OAuthError as e:
        if not e.redirectable or not params.get("redirect_uri"):
            log.info("authorize refused: %s", e.description)
            return _error_page(e.member or MEMBER_INCOMPLETE, e.status)
        return _redirect_with(params["redirect_uri"], {"error": e.error, "error_description": e.description,
                                                       "state": params.get("state")})
    user = security.optional_user(request)
    if user is None:
        return _login_redirect(request)
    if security.reconfirm_due(user):
        return _reconfirm_page()
    try:
        ratelimit.hit("oauth_authorize", user.id, AUTHORIZE_PER_MEMBER_HOUR, "hour")
    except ratelimit.TooMany:
        return _error_page("Too many tries. Wait an hour and try again.", 429)
    return _consent_html(ar, user, request.state.session.csrf_token, params)


@router.get("/oauth/authorize", include_in_schema=False)
def authorize(request: Request):
    return _authorize_get(request)


def _same_origin(request: Request) -> bool:
    want = get_settings().public_origin
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return origin.rstrip("/") == want
    ref = request.headers.get("referer")
    if ref:
        return ref == want or ref.startswith(want + "/")
    return True


def _authorize_post(request: Request, form: dict) -> Response:
    user = security.optional_user(request)
    if user is None:
        return _error_page("Your sign-in ended. Go back to Claude and press Connect again.", 401)
    if security.reconfirm_due(user):
        return _reconfirm_page()
    sess = request.state.session
    if not _same_origin(request) or not security.consteq(form.get("csrf_token"), sess.csrf_token):
        return _error_page("This page expired. Go back to Claude and press Connect again.", 403)
    params = {k: form.get(k) for k in FIELDS}
    try:
        ar = _check_request(params, ratelimit.client_key(request))
    except OAuthError as e:
        if not e.redirectable:
            log.info("authorize refused: %s", e.description)
            return _error_page(e.member or MEMBER_INCOMPLETE, e.status)
        return _redirect_with(params["redirect_uri"], {"error": e.error, "error_description": e.description,
                                                       "state": params.get("state")})
    if form.get("decision") != "allow":
        return _redirect_with(ar.redirect_uri, {"error": "access_denied",
                                                "error_description": "The member said no.", "state": ar.state})
    code, ch = security.new_token("rvc")
    now = db.utcnow()
    with db.tx() as conn:
        conn.execute(db.oauth_codes.insert().values(code_hash=ch, client_id=ar.client.client_id, user_id=user.id,
                                                    redirect_uri=ar.redirect_uri, code_challenge=ar.code_challenge,
                                                    scope=ar.scope, resource=ar.resource,
                                                    expires_at=now + CODE_TTL))
        db.audit("oauth_grant", user_id=user.id, actor=user.id, ip=security.client_ip(request), conn=conn,
                 detail={"client": ar.client.host, "kind": ar.client.kind})
    return _redirect_with(ar.redirect_uri, {"code": code, "state": ar.state})


@router.post("/oauth/authorize", include_in_schema=False)
async def authorize_post(request: Request):
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        return _error_page("This page expired. Go back to Claude and press Connect again.", 400)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    return await run_in_threadpool(_authorize_post, request, form)


# ============================================================================================== token, register, revoke

def _form_only(request: Request) -> OAuthError | None:
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        return OAuthError("invalid_request", "Send the token request as application/x-www-form-urlencoded.")
    return None


def _basic_client_id(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(auth[6:].strip()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    from urllib.parse import unquote

    return unquote(raw.split(":", 1)[0]) or None


def _token_sync(request: Request, form: dict) -> JSONResponse:
    _maybe_cleanup()
    client_id = form.get("client_id") or _basic_client_id(request) or ""
    form["client_id"] = client_id
    try:
        ratelimit.hit_all(request, [("oauth_token", ratelimit.key(ratelimit.client_key(request), client_id),
                                     TOKEN_PER_KEY_MINUTE),
                                    ("oauth_token_all", "global", TOKEN_GLOBAL_MINUTE)], "minute")
    except ratelimit.TooMany as e:
        return _oauth_json({"error": "invalid_request", "error_description": e.message}, 429,
                           extra={"Retry-After": e.headers.get("Retry-After", "60")})
    gt = form.get("grant_type")
    try:
        if gt == "authorization_code":
            out = grant_authorization_code(form)
        elif gt == "refresh_token":
            out = grant_refresh(form)
        else:
            raise OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token.")
    except OAuthError as e:
        return _oauth_json(e.body(), e.status)
    return _oauth_json(out)


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request):
    bad = _form_only(request)
    if bad is not None:
        return _oauth_json(bad.body(), 400)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    return await run_in_threadpool(_token_sync, request, form)


def _register_sync(request: Request, body: Any) -> JSONResponse:
    _maybe_cleanup()
    if not isinstance(body, dict):
        return _oauth_json({"error": "invalid_client_metadata", "error_description": "Send a JSON object."}, 400)
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 5 or not all(isinstance(u, str) for u in uris):
        return _oauth_json({"error": "invalid_redirect_uri", "error_description": "redirect_uris is required."}, 400)
    bad = [u for u in uris if not redirect_allowed(u)]
    if bad:
        return _oauth_json({"error": "invalid_redirect_uri",
                            "error_description": "Only Claude's callback or a loopback /callback (or /oauth/callback) is allowed."}, 400)
    method = body.get("token_endpoint_auth_method") or "none"
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        return _oauth_json({"error": "invalid_client_metadata",
                            "error_description": "token_endpoint_auth_method must be none."}, 400)
    gts = body.get("grant_types") or ["authorization_code", "refresh_token"]
    if not isinstance(gts, list) or not set(gts) <= {"authorization_code", "refresh_token"}:
        return _oauth_json({"error": "invalid_client_metadata",
                            "error_description": "grant_types may only be authorization_code and refresh_token."},
                           400)
    rts = body.get("response_types") or ["code"]
    if rts != ["code"]:
        return _oauth_json({"error": "invalid_client_metadata",
                            "error_description": "response_types must be [\"code\"]."}, 400)
    name = body.get("client_name") if isinstance(body.get("client_name"), str) else ""
    name = name.strip()[:100]
    ip = security.client_ip(request)
    ck = ratelimit.client_key(request)
    try:
        # The plain per-client limit comes first and, once over it, nothing else is counted: a client that
        # changes client_name on every call can no longer fill the server-wide budget (fix round 2).
        ratelimit.hit("oauth_reg_ip", ck, ratelimit.ip_limit(REGISTER_PER_IP_HOUR, ip), "hour")
        ratelimit.hit_all(request, [
            ("oauth_reg", ratelimit.key(ck, "|".join(sorted(uris)), name),
             ratelimit.ip_limit(REGISTER_PER_KEY_HOUR, ip)),
            ("oauth_reg_all", "global", REGISTER_GLOBAL_HOUR)], "hour")
    except ratelimit.TooMany as e:
        return _oauth_json({"error": "invalid_request", "error_description": e.message}, 429,
                           extra={"Retry-After": e.headers.get("Retry-After", "60")})
    cid = db.new_id("dcr")
    now = db.utcnow()
    md = {"client_name": name, "grant_types": gts, "response_types": ["code"], "token_endpoint_auth_method": "none",
          "requested_auth_method": method}
    for k in ("client_uri", "logo_uri", "software_id", "software_version"):
        if isinstance(body.get(k), str):
            md[k] = body[k][:300]
    with db.tx() as conn:
        conn.execute(db.oauth_clients.insert().values(client_id=cid, kind="dcr", metadata=md, redirect_uris=uris,
                                                      created_at=now))
    out = {"client_id": cid, "client_id_issued_at": int(now.timestamp()), "redirect_uris": uris,
           "token_endpoint_auth_method": "none", "grant_types": gts, "response_types": ["code"]}
    if name:
        out["client_name"] = name
    return _oauth_json(out, 201)


@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request):
    try:
        body = json.loads((await read_body(request, 64 * 1024)) or b"null")
    except ValueError:
        body = None
    return await run_in_threadpool(_register_sync, request, body)


@router.post("/oauth/revoke", include_in_schema=False)
async def revoke(request: Request):
    bad = _form_only(request)
    if bad is not None:
        return _oauth_json(bad.body(), 400)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    if not form.get("token"):
        return _oauth_json({"error": "invalid_request", "error_description": "token is required."}, 400)
    await run_in_threadpool(revoke_token, form["token"], form.get("client_id") or _basic_client_id(request))
    return _oauth_json({}, 200)   # RFC 7009: 200 whether or not the token existed


# ============================================================================================== member: connected apps

def clients_for(user_id: str) -> list[dict]:
    """Apps that hold a live refresh token for this member (Settings > AI > Claude)."""
    t, cl = db.oauth_tokens, db.oauth_clients
    now = db.utcnow()
    with db.read() as c:
        rows = c.execute(select(t.c.client_id, func.min(t.c.created_at), func.max(t.c.created_at))
                         .where(t.c.user_id == user_id, t.c.kind == "refresh", t.c.revoked_at.is_(None),
                                t.c.expires_at > now).group_by(t.c.client_id)).all()
        meta = {r.client_id: r for r in c.execute(select(cl).where(cl.c.client_id.in_([x[0] for x in rows])))}
    out = []
    for cid, first, last in rows:
        m = meta.get(cid)
        kind = m.kind if m is not None else ("cimd" if cid.startswith(("https://", "http://")) else "dcr")
        cli = Client(cid, kind, list(m.redirect_uris or []) if m is not None else [])
        out.append({"client_id": cid, "name": cli.host, "kind": kind, "connected_at": db.iso(first),
                    "last_used_at": db.iso(last)})
    out.sort(key=lambda x: x["last_used_at"] or "", reverse=True)
    return out


def disconnect(user_id: str, client_id: str | None = None) -> int:
    t = db.oauth_tokens
    with db.tx() as conn:
        q = t.update().where(t.c.user_id == user_id, t.c.revoked_at.is_(None))
        if client_id:
            q = q.where(t.c.client_id == client_id)
        n = conn.execute(q.values(revoked_at=db.utcnow())).rowcount
        db.audit("oauth_disconnect", user_id=user_id, actor=user_id, conn=conn,
                 detail={"client": (client_id or "all")[:200], "tokens": n})
    return n


@router.get("/api/connector/clients")
def connector_clients(user=Depends(security.current_user)):
    return {"clients": clients_for(user.id), "url": resource_url()}


@router.post("/api/connector/disconnect")
def connector_disconnect(body: Any = Body(None), user=Depends(security.current_user)):
    b = body if isinstance(body, dict) else {}
    cid = b.get("client_id")
    if cid is not None and not isinstance(cid, str):
        raise bad_input("client_id must be text.")
    if not cid and b.get("all") is not True:
        raise bad_input("Say which app to disconnect, or send all: true.")
    n = disconnect(user.id, cid or None)
    return {"ok": True, "revoked": n, "clients": clients_for(user.id)}


# ============================================================================================== plugin zip (7.3.5)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def plugin_dir() -> Path | None:
    for cand in (os.environ.get("RV_PLUGIN_DIR"), PROJECT_ROOT / "plugin", "/opt/reelvault/plugin"):
        if cand and (Path(cand) / ".claude-plugin" / "plugin.json").is_file():
            return Path(cand)
    return None


def _fallback_plugin_files() -> dict[str, str]:
    """Used when the plugin/ folder is not shipped (an installed wheel): the same three files, built in code."""
    from .mcp_server import INSTRUCTIONS

    return {".claude-plugin/plugin.json": json.dumps(
                {"name": "reelvault-members", "version": "1.0.0",
                 "description": "Tag and break down your competitors' Instagram reels with your Claude plan, "
                                "and write scripts from what works."}, indent=2) + "\n",
            ".mcp.json.tmpl": json.dumps({"mcpServers": {"reelvault": {"type": "http",
                                                                        "url": "https://{{HOST}}/mcp"}}},
                                         indent=2) + "\n",
            "skills/reelvault/SKILL.md": "---\nname: reelvault\ndescription: Work through ReelVault Members "
                                         "jobs (tag reels, analyze outliers, write outputs).\n---\n\n"
                                         + INSTRUCTIONS + "\n"}


def plugin_zip() -> bytes:
    """The plugin with .mcp.json rendered for this server (https://HOST/mcp, or the http dev URL)."""
    src = plugin_dir()
    files: dict[str, str] = {}
    if src is not None:
        for p in sorted(src.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith((".pyc", ".zip")):
                files[p.relative_to(src).as_posix()] = p.read_text(encoding="utf-8")
    else:
        files = _fallback_plugin_files()
    tmpl = files.pop(".mcp.json.tmpl", None)
    if tmpl is not None:
        s = get_settings()
        rendered = tmpl.replace("https://{{HOST}}", s.public_origin).replace("{{HOST}}", s.public_host)
        files[".mcp.json"] = rendered
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo("reelvault-members/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name])
    return buf.getvalue()


@router.get("/download/claude-plugin.zip", include_in_schema=False)
def download_plugin():
    return Response(plugin_zip(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="reelvault-claude-plugin.zip"',
                             "Cache-Control": "no-cache"})


__all__ = ["router", "OAuthError", "Client", "as_metadata", "protected_resource_metadata", "issuer", "resource_url",
           "redirect_allowed", "redirect_matches", "resolve_client", "verify_access", "revoke_token", "cleanup",
           "clients_for", "disconnect", "plugin_zip", "pkce_s256", "ApiError"]
