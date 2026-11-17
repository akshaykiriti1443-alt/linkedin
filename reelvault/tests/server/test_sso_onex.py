"""OneX one-click sign-in (/sso/onex): signed, short-lived, single-use tickets."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

import pytest

SECRET = "s" * 40


def ticket(email="newmember@example.com", name="New Member", exp_in=120, nonce=None, secret=SECRET):
    payload = base64.urlsafe_b64encode(json.dumps({
        "email": email, "name": name, "exp": int(time.time()) + exp_in,
        "nonce": nonce or secrets.token_hex(12)}).encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), f"v1.{payload}".encode(), hashlib.sha256).digest()
    return f"v1.{payload}." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def test_not_set_up(client, monkeypatch):
    monkeypatch.delenv("ONEX_SSO_SECRET", raising=False)
    r = client.get("/sso/onex", params={"t": ticket()}, follow_redirects=False)
    assert r.status_code == 503 and "not set up" in r.text


def test_new_member_signs_in_and_lands_active(client, monkeypatch):
    monkeypatch.setenv("ONEX_SSO_SECRET", SECRET)
    r = client.get("/sso/onex", params={"t": ticket()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    me = client.get("/api/me")
    assert me.status_code == 200
    body = me.json()
    user = body.get("user", body)
    assert user["email"] == "newmember@example.com" and user["status"] == "active" and user["role"] == "member"


def test_existing_member_signs_in(client, member, monkeypatch):
    monkeypatch.setenv("ONEX_SSO_SECRET", SECRET)
    r = client.get("/sso/onex", params={"t": ticket(email=member.email)}, follow_redirects=False)
    assert r.status_code == 303
    body = client.get("/api/me").json()
    assert body.get("user", body)["id"] == member.id


def test_bad_tickets_are_refused(client, monkeypatch):
    monkeypatch.setenv("ONEX_SSO_SECRET", SECRET)
    for bad in (ticket(secret="x" * 40), ticket(exp_in=-5), ticket(exp_in=3600), "v1.abc.def", "nonsense"):
        r = client.get("/sso/onex", params={"t": bad}, follow_redirects=False)
        assert r.status_code == 400, bad
        assert client.get("/api/me").status_code == 401


def test_ticket_works_once(client, monkeypatch):
    monkeypatch.setenv("ONEX_SSO_SECRET", SECRET)
    t = ticket(nonce="same-nonce-123456")
    assert client.get("/sso/onex", params={"t": t}, follow_redirects=False).status_code == 303
    r = client.get("/sso/onex", params={"t": t}, follow_redirects=False)
    assert r.status_code == 400 and "already used" in r.text


# ============================================================================================== embedded in OneX (D86)
# Test secret only ("s" * 40). OneX adds embed=1&parent=<its origin>&theme=..&to=<route>; ReelVault checks each one
# and redirects only to "/", "/#<route>" or "/?embed=1&parent=<origin>[&theme=..]#<route>".

HOSTED = "https://reelvault.scaleitads.com"
ONEX = "https://app.scaleitads.com"
REEL = "reel/rl_abcdefghijklmnop"
ONEX_Q = "/?embed=1&parent=https%3A%2F%2Fapp.scaleitads.com"


@pytest.fixture
def onex_env(monkeypatch, rv_env):
    from reelvault.server import settings

    def set_env(onex_url: str | None = ONEX, local: bool = False, public_url: str | None = None):
        monkeypatch.setenv("ONEX_SSO_SECRET", SECRET)
        if onex_url is None:
            monkeypatch.delenv("ONEX_URL", raising=False)
        else:
            monkeypatch.setenv("ONEX_URL", onex_url)
        if local:
            monkeypatch.setenv("RV_LOCAL_MEMBERS", "1")
        else:
            monkeypatch.delenv("RV_LOCAL_MEMBERS", raising=False)
        if public_url:
            monkeypatch.setenv("PUBLIC_URL", public_url)
        return settings.reload()

    return set_env


def sso(client, **params):
    params.setdefault("t", ticket())
    client.cookies.clear()
    return client.get("/sso/onex", params=params, follow_redirects=False)


def test_exact_location_for_each_redirect_form(client, onex_env):
    onex_env(ONEX)
    assert sso(client).headers["location"] == "/"
    assert sso(client, to="reels").headers["location"] == "/#reels"
    assert sso(client, to="working?dimension=hook&window=recent").headers["location"] == \
        "/#working?dimension=hook&window=recent"
    assert sso(client, theme="dark", to="reels").headers["location"] == "/#reels"   # theme only counts in embed
    r = sso(client, embed="1", parent=ONEX, theme="dark", to=REEL)
    assert r.status_code == 303 and r.headers["location"] == ONEX_Q + "&theme=dark#" + REEL
    assert r.headers["cache-control"] == "no-store"
    assert sso(client, embed="1", parent=ONEX).headers["location"] == ONEX_Q + "#home"
    assert sso(client, embed="1", parent=ONEX, theme="blue", to="home").headers["location"] == ONEX_Q + "#home"
    assert sso(client, embed="1", parent=ONEX, theme="light").headers["location"] == ONEX_Q + "&theme=light#home"
    assert sso(client, embed="yes", parent=ONEX, to="reels").headers["location"] == "/#reels"   # only "1" counts
    # the contract's local example
    onex_env("http://localhost:3000", local=True)
    assert sso(client, embed="1", parent="http://localhost:3000", theme="dark", to=REEL).headers["location"] == \
        "/?embed=1&parent=http%3A%2F%2Flocalhost%3A3000&theme=dark#" + REEL


@pytest.mark.parametrize("bad", ["//evil.example", "https://evil.example", "reels/../../x", "admin", "admin/members",
                                 "login", "join/ABCD", "reset/x", "verify/x", "reels\n", "reels#x", "reels/a/b/c",
                                 "reels?q=<script>", "reels?" + "a" * 201, "r" * 301, "", "/reels", "Reels",
                                 "reels/" + "a" * 65, "reels?q=a b", "home\\@evil.example"])
def test_bad_routes_fall_back_to_home(client, onex_env, bad):
    onex_env(ONEX)
    assert sso(client, to=bad).headers["location"] == "/"
    assert sso(client, embed="1", parent=ONEX, to=bad).headers["location"] == ONEX_Q + "#home"


def test_a_foreign_parent_is_replaced_or_dropped(client, onex_env):
    onex_env(ONEX, public_url=HOSTED)
    for bad in ("https://evil.example", "https://app.scaleitads.com.evil.example", "http://localhost:3001",
                "https://app.scaleitads.com/", "javascript:alert(1)", ""):
        assert sso(client, embed="1", parent=bad, to="reels").headers["location"] == ONEX_Q + "#reels", bad
    # nobody may frame this server (no ONEX_URL): embed is dropped, the sign-in still works
    onex_env(None, public_url=HOSTED)
    assert sso(client, embed="1", parent="https://evil.example", to="reels").headers["location"] == "/#reels"
    assert sso(client, embed="1", parent=ONEX).headers["location"] == "/"


def test_local_members_mode_accepts_any_localhost_parent(client, onex_env):
    onex_env("http://localhost:3000", local=True)
    loc = sso(client, embed="1", parent="http://localhost:3001", to="outputs").headers["location"]
    assert loc == "/?embed=1&parent=http%3A%2F%2Flocalhost%3A3001#outputs"
    loc = sso(client, embed="1", parent="http://127.0.0.1:5173").headers["location"]
    assert loc == "/?embed=1&parent=http%3A%2F%2F127.0.0.1%3A5173#home"
    # outside the local mode the same parent is replaced by ONEX_URL's origin
    onex_env("http://localhost:3000", local=False)
    loc = sso(client, embed="1", parent="http://localhost:3001").headers["location"]
    assert loc == "/?embed=1&parent=http%3A%2F%2Flocalhost%3A3000#home"


def test_session_is_reused_for_the_same_member(client, onex_env):
    from reelvault.server import security

    onex_env(ONEX)
    r1 = client.get("/sso/onex", params={"t": ticket(), "embed": "1", "parent": ONEX}, follow_redirects=False)
    cookie = r1.cookies.get(security.SESSION_COOKIE)
    assert r1.status_code == 303 and cookie
    sc = r1.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=lax" in sc and "path=/" in sc and "domain" not in sc
    # OneX opens ReelVault again (a fresh ticket): the same session stays, no new cookie
    r2 = client.get("/sso/onex", params={"t": ticket(), "embed": "1", "parent": ONEX}, follow_redirects=False)
    assert r2.status_code == 303 and "set-cookie" not in r2.headers
    assert client.cookies.get(security.SESSION_COOKIE) == cookie
    assert client.get("/api/me").status_code == 200
    # a ticket for someone else replaces the session
    r3 = client.get("/sso/onex", params={"t": ticket(email="other@example.com")}, follow_redirects=False)
    assert r3.status_code == 303 and r3.cookies.get(security.SESSION_COOKIE) not in (None, cookie)
    body = client.get("/api/me").json()
    assert body.get("user", body)["email"] == "other@example.com"


def test_reused_session_still_uses_up_the_ticket_and_counts_the_login(client, onex_env):
    from sqlalchemy import select

    from reelvault.server import db

    onex_env(ONEX)
    assert client.get("/sso/onex", params={"t": ticket()}, follow_redirects=False).status_code == 303
    with db.tx() as conn:
        u = conn.execute(select(db.users).where(db.users.c.email == "newmember@example.com")).first()
        conn.execute(db.users.update().where(db.users.c.id == u.id).values(last_login_at=None))
    t = ticket(nonce="reuse-nonce-123456")
    r = client.get("/sso/onex", params={"t": t}, follow_redirects=False)
    assert r.status_code == 303 and "set-cookie" not in r.headers
    with db.read() as conn:
        u2 = conn.execute(select(db.users).where(db.users.c.id == u.id)).first()
    assert u2.last_login_at is not None
    r = client.get("/sso/onex", params={"t": t}, follow_redirects=False)
    assert r.status_code == 400 and "already used" in r.text


def test_nonce_window_is_the_tickets_expiry_date(client, onex_env):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from reelvault.server import db

    onex_env(ONEX)
    t = ticket(nonce="window-nonce-123456", exp_in=300)
    exp_day = datetime.fromtimestamp(int(time.time()) + 300, timezone.utc).strftime("%Y-%m-%d")
    assert client.get("/sso/onex", params={"t": t}, follow_redirects=False).status_code == 303
    uc = db.usage_counters
    with db.read() as conn:
        row = conn.execute(select(uc).where(uc.c.key == "sso:window-nonce-123456")).first()
    assert row.window == exp_day and row.count == 1


def test_embed_error_pages_tell_onex_what_went_wrong(client, onex_env, make_user):
    onex_env(ONEX)
    emb = {"embed": "1", "parent": ONEX}
    for code, status, params in [("bad_link", 400, {"t": "nonsense"}), ("expired", 400, {"t": ticket(exp_in=-5)})]:
        r = sso(client, **emb, **params)
        assert r.status_code == status and r.headers["content-type"].startswith("text/html"), code
        assert f'data-rv-error="{code}"' in r.text and f'data-rv-parent="{ONEX}"' in r.text
        assert 'data-rv-message="' in r.text
        assert '<script src="/static/embed-error.js" defer></script>' in r.text
        assert "Back to OneX" not in r.text and "style=" not in r.text
        assert '<link rel="stylesheet" href="/static/app.css">' in r.text and '<main class="plain">' in r.text
        assert r.headers["cache-control"] == "no-store"
    t = ticket(nonce="used-nonce-1234567")
    assert sso(client, t=t).status_code == 303
    r = sso(client, t=t, **emb)
    assert r.status_code == 400 and 'data-rv-error="used"' in r.text
    make_user("off@example.com", status="disabled")
    r = sso(client, t=ticket(email="off@example.com"), **emb)
    assert r.status_code == 403 and 'data-rv-error="turned_off"' in r.text


@pytest.mark.parametrize("params", [{}, {"t": ""}])
def test_a_link_without_a_ticket_is_a_bad_link_page_not_json(client, onex_env, params):
    onex_env(ONEX)
    client.cookies.clear()
    r = client.get("/sso/onex", params={"embed": "1", "parent": ONEX, **params}, follow_redirects=False)
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")
    assert 'data-rv-error="bad_link"' in r.text and f'data-rv-parent="{ONEX}"' in r.text
    assert "embed-error.js" in r.text and "set-cookie" not in r.headers
    r = client.get("/sso/onex", params=params, follow_redirects=False)
    assert r.status_code == 400 and "Back to OneX" in r.text and "data-rv-error" not in r.text


def test_old_sso_nonce_rows_are_swept_after_a_sign_in(client, onex_env, monkeypatch):
    from reelvault.server import api_auth, db

    onex_env(ONEX)
    db.bump(db.usage_counters, {"scope": "global", "key": "sso:ancient-nonce-123", "window": "2020-01-01"}, count=1)
    monkeypatch.setattr(api_auth, "_nonce_swept", {})   # not swept yet in this process
    assert sso(client).status_code == 303
    with db.read() as c:
        keys = {r.key for r in c.execute(db.usage_counters.select()).all()}
    assert "sso:ancient-nonce-123" not in keys
    assert any(k.startswith("sso:") for k in keys)   # today's ticket nonce stays


def test_embed_error_page_not_set_up(client, onex_env, monkeypatch):
    onex_env(ONEX)
    monkeypatch.delenv("ONEX_SSO_SECRET", raising=False)
    r = sso(client, embed="1", parent=ONEX)
    assert r.status_code == 503 and 'data-rv-error="not_set_up"' in r.text


def test_too_many_tries_render_the_error_page(client, onex_env):
    onex_env(ONEX)
    rs = [sso(client, t="nonsense", embed="1", parent=ONEX) for _ in range(31)]
    assert [r.status_code for r in rs[:30]] == [400] * 30
    last = rs[-1]
    assert last.status_code == 429 and last.headers["content-type"].startswith("text/html")
    assert 'data-rv-error="too_many"' in last.text and last.headers.get("retry-after")


def test_plain_error_page_keeps_the_back_link(client, onex_env):
    onex_env(ONEX)
    r = sso(client, t="nonsense")
    assert r.status_code == 400 and f'<a href="{ONEX}">Back to OneX</a>' in r.text
    assert "data-rv-error" not in r.text and "embed-error.js" not in r.text and "style=" not in r.text
    onex_env("javascript:alert(1)")
    assert "Back to OneX" not in sso(client, t="nonsense").text
