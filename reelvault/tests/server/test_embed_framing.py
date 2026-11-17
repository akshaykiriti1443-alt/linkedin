"""Embed mode (D86): who may show ReelVault inside a frame. frame-ancestors lists exactly the OneX origin(s) on "/" and
"/sso/onex" only; every other HTML page stays 'none' + X-Frame-Options: DENY. Cases A-E of the OneX merge contract.
Test secrets only: "s" * 40. No real secret in this public repo."""
from __future__ import annotations

import pytest

LOCALS = "http://localhost:* http://127.0.0.1:*"


@pytest.fixture
def env(monkeypatch, rv_env):
    """env(onex_url=..., local=..., public_url=...) sets the environment and re-reads the settings."""
    from reelvault.server import settings

    def set_env(onex_url: str | None = None, local: bool = False, public_url: str | None = None):
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
        monkeypatch.setenv("ONEX_SSO_SECRET", "s" * 40)
        return settings.reload()

    return set_env


def _pages(client):
    """The two frameable HTML pages: the SPA and an /sso/onex error page."""
    spa = client.get("/")
    sso = client.get("/sso/onex", params={"t": "nonsense"}, follow_redirects=False)
    assert spa.headers["content-type"].startswith("text/html")
    assert sso.status_code == 400 and sso.headers["content-type"].startswith("text/html")
    return spa, sso


def _fa(r) -> str:
    csp = r.headers["content-security-policy"]
    part = next(p.strip() for p in csp.split(";") if p.strip().startswith("frame-ancestors"))
    return part


def test_a_local_members_with_onex_url(client, env):
    env("http://localhost:3000", local=True)
    for r in _pages(client):
        assert _fa(r) == "frame-ancestors http://localhost:3000 " + LOCALS
        assert "x-frame-options" not in r.headers
        assert "font-src 'self'" in r.headers["content-security-policy"]
        assert "default-src 'self'" in r.headers["content-security-policy"]


@pytest.mark.parametrize("onex", [None, "", "javascript:alert(1)", "https://user:pw@app.example.com",
                                  "https://app.example.com/path", "ftp://app.example.com"])
def test_b_local_members_without_a_good_onex_url(client, env, onex):
    env(onex, local=True)
    for r in _pages(client):
        assert _fa(r) == "frame-ancestors " + LOCALS
        assert "x-frame-options" not in r.headers


def test_c_hosted_with_onex_url(client, env):
    env("https://app.scaleitads.com/", local=False, public_url="https://reelvault.scaleitads.com")
    for r in _pages(client):
        assert _fa(r) == "frame-ancestors https://app.scaleitads.com"
        assert "x-frame-options" not in r.headers
    # the local flag set by mistake on a hosted (https) server loosens nothing
    env("https://app.scaleitads.com", local=True, public_url="https://reelvault.scaleitads.com")
    for r in _pages(client):
        assert _fa(r) == "frame-ancestors https://app.scaleitads.com"


@pytest.mark.parametrize("onex", [None, "", "http://app.scaleitads.com", "https://app.scaleitads.com/app",
                                  "https://app.scaleitads.com?x=1", "https://app.scaleitads.com; script-src *",
                                  "https://app.scaleitads.com:99999", "https://evil@app.scaleitads.com"])
def test_d_hosted_without_a_good_onex_url_stays_denied(client, env, onex):
    env(onex, local=False, public_url="https://reelvault.scaleitads.com")
    for r in _pages(client):
        assert _fa(r) == "frame-ancestors 'none'"
        assert r.headers["x-frame-options"] == "DENY"
        assert "script-src *" not in r.headers["content-security-policy"]


@pytest.mark.parametrize("local", [True, False])
def test_e_every_other_html_page_is_never_framed(app, client, env, local):
    from fastapi.responses import HTMLResponse

    env("http://localhost:3000", local=local)

    def _html():
        return HTMLResponse("<!doctype html><p>hi</p>")

    app.add_api_route("/x-test-html", _html, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())   # before the MCP catch-all mount

    r = client.get("/x-test-html")
    assert _fa(r) == "frame-ancestors 'none'" and r.headers["x-frame-options"] == "DENY"
    # OAuth pages (oauth.py) send their own CSP: still 'none', and DENY once
    o = client.get("/oauth/authorize")
    assert o.headers["content-type"].startswith("text/html")
    assert "frame-ancestors 'none'" in o.headers["content-security-policy"]
    assert o.headers.get_list("x-frame-options") == ["DENY"]
    # a path that only starts like a frameable one is not frameable
    j = client.get("/sso/onex/x")
    assert "frame-ancestors http" not in j.headers.get("content-security-policy", "")


def test_json_and_redirects_carry_no_csp(client, env):
    env("http://localhost:3000", local=True)
    r = client.get("/healthz")
    assert "content-security-policy" not in r.headers and r.headers["x-content-type-options"] == "nosniff"


def test_settings_origin_rules(env):
    s = env("HTTPS://App.ScaleItAds.com/", local=False, public_url="https://reelvault.scaleitads.com")
    assert s.onex_origin == "https://app.scaleitads.com"
    assert s.parent_origin_ok("https://app.scaleitads.com")
    for bad in ("https://app.scaleitads.com/", "https://evil.example", "http://localhost:3000", "", None, 5,
                "https://app.scaleitads.com.evil.example"):
        assert not s.parent_origin_ok(bad), bad
    s = env("http://127.0.0.1:3000", local=False, public_url="http://localhost")   # the flag is off
    assert s.onex_origin == "http://127.0.0.1:3000" and not s.is_local_members
    assert s.frame_ancestors() == "frame-ancestors http://127.0.0.1:3000"
    assert not s.parent_origin_ok("http://localhost:3001")
    s = env(None, local=True, public_url="https://reelvault.scaleitads.com")   # hosted: the flag does nothing
    assert not s.is_local_members and not s.parent_origin_ok("http://localhost:3000")
    assert s.frame_ancestors() == "frame-ancestors 'none'"
    s = env(None, local=True, public_url="http://127.0.0.1:8800")
    assert s.is_local_members
    s = env(None, local=True, public_url="http://localhost:8800")
    assert s.is_local_members and s.onex_origin is None
    for good in ("http://localhost:3000", "http://localhost:3001", "http://127.0.0.1:5173"):
        assert s.parent_origin_ok(good), good
    for bad in ("http://localhost", "http://localhost:", "http://localhost:123456", "https://localhost:3000",
                "http://localhost:3000/", "http://localhost:3000.evil.example", "http://localhost:３０００",
                "http://evil.example:3000"):
        assert not s.parent_origin_ok(bad), bad


@pytest.mark.parametrize("onex,origin", [
    ("https://app.scaleitads.com:443", "https://app.scaleitads.com"),     # default port: written like a browser
    ("https://app.scaleitads.com:443/", "https://app.scaleitads.com"),
    ("https://app.scaleitads.com:8443", "https://app.scaleitads.com:8443"),
    ("http://localhost:80", "http://localhost"),
    ("http://localhost:03000", "http://localhost:3000"),
    ("https://app.scaleitads.com:", None),                                 # empty port: not a valid CSP source
    ("https://app.scaleitads.com:0", None),
    ("https://app.scaleitads.com.", None),                                 # trailing dot
    ("https://.scaleitads.com", None),
    ("https://app..scaleitads.com", None),
])
def test_onex_origin_is_written_like_a_browser_origin(env, onex, origin):
    s = env(onex, local=False, public_url="https://reelvault.scaleitads.com")
    assert s.onex_origin == origin
    if origin:
        # OneX sends parent = new URL(request.url).origin, which is exactly this form
        assert s.parent_origin_ok(origin)
        assert s.frame_ancestors() == "frame-ancestors " + origin
    else:
        assert s.frame_ancestors() == "frame-ancestors 'none'"
