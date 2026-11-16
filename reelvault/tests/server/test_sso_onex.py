"""OneX one-click sign-in (/sso/onex): signed, short-lived, single-use tickets."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

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
