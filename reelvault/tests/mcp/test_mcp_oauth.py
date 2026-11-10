"""OAuth 2.1 authorization server + MCP auth over real HTTP (DESIGN-SAAS.md 7.3.1, 7.3.2, 12.4). WP6."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from _mcp_support import (CLAUDE_CB, CimdServer, authorize_params, code_from, consent, exchange, free_port,
                          full_flow, mcp_client, parse_form, pkce, register, run, session_cookie)


# ============================================================================================== discovery

def test_mcp_without_token_is_401_with_resource_metadata(live):
    r = httpx.post(live.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    wa = r.headers["www-authenticate"]
    assert wa.startswith("Bearer") and f'resource_metadata="{live.url}/.well-known/oauth-protected-resource/mcp"' in wa
    bad = httpx.post(live.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                     headers={"Authorization": "Bearer rva_not-a-real-token",
                              "Accept": "application/json, text/event-stream"})
    assert bad.status_code == 401


def test_protected_resource_and_authorization_server_metadata(live):
    prm = httpx.get(live.url + "/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == live.mcp_url
    assert prm["authorization_servers"] == [live.url]
    assert prm["scopes_supported"] == ["reelvault"]
    md = httpx.get(live.url + "/.well-known/oauth-authorization-server").json()
    assert md["issuer"] == live.url
    for k, path in (("authorization_endpoint", "/oauth/authorize"), ("token_endpoint", "/oauth/token"),
                    ("registration_endpoint", "/oauth/register"), ("revocation_endpoint", "/oauth/revoke")):
        assert md[k] == live.url + path
    assert md["response_types_supported"] == ["code"]
    assert md["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert md["code_challenge_methods_supported"] == ["S256"]
    assert md["token_endpoint_auth_methods_supported"] == ["none"]
    assert md["client_id_metadata_document_supported"] is True
    assert set(md["scopes_supported"]) == {"reelvault", "offline_access"}
    assert httpx.get(live.url + "/.well-known/openid-configuration").status_code == 404


# ============================================================================================== DCR + code flow

def test_dcr_code_flow_then_mcp_works(live, member):
    tok = full_flow(live, member)
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 3600 and tok["scope"] == "reelvault"
    assert tok["access_token"].startswith("rva_") and tok["refresh_token"].startswith("rvr_")

    async def go():
        async with mcp_client(live.mcp_url, tok["access_token"]) as c:
            tools = await c.list_tools()
            return {t.name for t in tools.tools}

    assert "list_pending_work" in run(go)
    from reelvault.server import db, security

    with db.read() as c:
        rows = c.execute(db.oauth_tokens.select()).all()
        assert {r.token_hash for r in rows} == {security.sha256_hex(tok["access_token"]),
                                                security.sha256_hex(tok["refresh_token"])}
        assert all(r.user_id == member.id and r.resource == live.mcp_url for r in rows)
        grant = c.execute(db.audit_log.select().where(db.audit_log.c.action == "oauth_grant")).first()
        assert grant is not None and grant.user_id == member.id


def test_official_sdk_client_runs_the_whole_oauth_flow(live, member):
    """The mcp SDK's own OAuth client: 401 -> PRM -> AS metadata -> DCR -> authorize (we press Allow) -> token."""
    import httpx2
    from mcp import Client
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

    cookie = session_cookie(member)
    got: dict = {}
    redirect = f"http://localhost:{free_port()}/callback"

    class Mem:
        def __init__(self):
            self.tokens = None
            self.info = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, t):
            self.tokens = t

        async def get_client_info(self):
            return self.info

        async def set_client_info(self, i):
            self.info = i

    async def on_redirect(url: str) -> None:
        p = urlparse(url)
        assert f"{p.scheme}://{p.netloc}" == live.url and p.path == "/oauth/authorize"
        q = parse_qs(p.query)
        assert q["code_challenge_method"] == ["S256"] and q["resource"] == [live.mcp_url]
        async with httpx2.AsyncClient(cookies={"rv_session": cookie}, follow_redirects=False) as h:
            page = await h.get(url)
            assert page.status_code == 200
            fields = parse_form(page.text)
            fields["decision"] = "allow"
            r = await h.post(live.url + "/oauth/authorize", data=fields, headers={"Origin": live.url})
        assert r.status_code == 303
        got.update({k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()})

    async def on_callback():
        return AuthorizationCodeResult(code=got["code"], state=got.get("state"))

    store = Mem()
    auth = OAuthClientProvider(live.mcp_url, OAuthClientMetadata(client_name="Claude Code (test)",
                                                                 redirect_uris=[redirect],
                                                                 grant_types=["authorization_code", "refresh_token"],
                                                                 response_types=["code"],
                                                                 token_endpoint_auth_method="none"),
                               store, redirect_handler=on_redirect, callback_handler=on_callback)

    async def go():
        async with httpx2.AsyncClient(auth=auth, timeout=60) as hc:
            async with Client(streamable_http_client(live.mcp_url, http_client=hc)) as c:
                r = await c.call_tool("list_pending_work", {})
                return r

    r = run(go)
    assert not r.is_error
    assert store.tokens is not None and store.tokens.access_token.startswith("rva_")
    assert store.info.client_id.startswith("dcr_")


def test_cimd_client_flow_with_loopback_any_port(live, member):
    port = free_port()
    doc = lambda url: {"client_id": url, "client_name": "Totally Claude", "redirect_uris":  # noqa: E731
                       ["http://localhost:33418/callback", CLAUDE_CB], "token_endpoint_auth_method": "none",
                       "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    srv = CimdServer(port, doc)
    try:
        redirect = "http://localhost:51234/callback"     # another port than the document lists: allowed (loopback)
        verifier, challenge = pkce()
        url = "/oauth/authorize?" + urlencode(authorize_params(live, srv.url, redirect, challenge))
        cookie = session_cookie(member)
        with httpx.Client(base_url=live.url, cookies={"rv_session": cookie}) as h:
            page = h.get(url)
        assert page.status_code == 200
        assert "<b>127.0.0.1</b> wants to use your ReelVault account." in page.text   # the URL host, not the name
        assert "Totally Claude" not in page.text
        assert "form-action 'self' http://localhost:51234" in page.headers["content-security-policy"]
        code, _q = code_from(consent(live.url, cookie, url))
        t = exchange(live.url, grant_type="authorization_code", code=code, redirect_uri=redirect, client_id=srv.url,
                     code_verifier=verifier, resource=live.mcp_url)
        assert t.status_code == 200, t.text
        hits = srv.hits
        consent(live.url, cookie, url)   # cached for 24 h: no new fetch
        assert srv.hits == hits
    finally:
        srv.close()


@pytest.mark.parametrize("bad", ["mismatch", "secret", "no_uris", "not_listed"])
def test_cimd_bad_documents_are_refused_without_redirect(live, member, bad):
    port = free_port()

    def doc(url):
        d = {"client_id": url, "redirect_uris": [CLAUDE_CB], "token_endpoint_auth_method": "none"}
        if bad == "mismatch":
            d["client_id"] = "https://claude.ai/oauth/claude-code-client-metadata"
        elif bad == "secret":
            d["token_endpoint_auth_method"] = "client_secret_basic"
        elif bad == "no_uris":
            d["redirect_uris"] = []
        return d

    srv = CimdServer(port, doc)
    try:
        redirect = "http://localhost:4444/callback" if bad == "not_listed" else CLAUDE_CB
        _v, challenge = pkce()
        url = "/oauth/authorize?" + urlencode(authorize_params(live, srv.url, redirect, challenge))
        with httpx.Client(base_url=live.url, cookies={"rv_session": session_cookie(member)}) as h:
            r = h.get(url, follow_redirects=False)
        assert r.status_code == 400 and "location" not in r.headers
        assert "We can't connect this app" in r.text
    finally:
        srv.close()


def test_redirect_uri_rules(live, member):
    assert register(live.url, "https://evil.example/callback").status_code == 400
    assert register(live.url, "http://localhost:8080/not-callback").status_code == 400
    assert register(live.url, "https://claude.ai/api/mcp/auth_callback#x").status_code == 400
    assert register(live.url, "http://localhost:9999/callback").status_code == 201
    cid = register(live.url).json()["client_id"]
    _v, challenge = pkce()
    with httpx.Client(base_url=live.url, cookies={"rv_session": session_cookie(member)}) as h:
        for uri in ("https://evil.example/cb", "https://claude.com/api/mcp/auth_callback",
                    "http://localhost:1234/callback"):
            r = h.get("/oauth/authorize?" + urlencode(authorize_params(live, cid, uri, challenge)),
                      follow_redirects=False)
            assert r.status_code == 400 and "location" not in r.headers, uri
        r = h.get("/oauth/authorize?" + urlencode(authorize_params(live, "dcr_unknown", CLAUDE_CB, challenge)))
        assert r.status_code == 400


def test_bad_request_details_go_back_to_the_client(live, member):
    cid = register(live.url).json()["client_id"]
    _v, challenge = pkce()
    cookie = session_cookie(member)
    with httpx.Client(base_url=live.url, cookies={"rv_session": cookie}, follow_redirects=False) as h:
        for extra, err in (({"code_challenge_method": "plain"}, "invalid_request"),
                           ({"code_challenge": None}, "invalid_request"),
                           ({"response_type": "token"}, "unsupported_response_type"),
                           ({"resource": "https://other.example/mcp"}, "invalid_target")):
            r = h.get("/oauth/authorize?" + urlencode(authorize_params(live, cid, CLAUDE_CB, challenge, **extra)))
            assert r.status_code == 303, extra
            q = parse_qs(urlparse(r.headers["location"]).query)
            assert r.headers["location"].startswith(CLAUDE_CB) and q["error"] == [err] and q["state"] == ["st-1"]


def test_no_session_goes_to_login_and_cancel_says_no(live, member):
    cid = register(live.url).json()["client_id"]
    _v, challenge = pkce()
    url = "/oauth/authorize?" + urlencode(authorize_params(live, cid, CLAUDE_CB, challenge))
    r = httpx.get(live.url + url, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/#login?next=%2Foauth%2Fauthorize%3F")
    _code, q = code_from(consent(live.url, session_cookie(member), url, decision="cancel"))
    assert q["error"] == "access_denied" and q["state"] == "st-1" and "code" not in q


def test_consent_needs_the_csrf_token_and_same_origin(live, member):
    cid = register(live.url).json()["client_id"]
    _v, challenge = pkce()
    url = "/oauth/authorize?" + urlencode(authorize_params(live, cid, CLAUDE_CB, challenge))
    with httpx.Client(base_url=live.url, cookies={"rv_session": session_cookie(member)}, follow_redirects=False) as h:
        fields = parse_form(h.get(url).text)
        fields["decision"] = "allow"
        bad = dict(fields, csrf_token="nope")
        assert h.post("/oauth/authorize", data=bad, headers={"Origin": live.url}).status_code == 403
        assert h.post("/oauth/authorize", data=fields, headers={"Origin": "https://evil.example"}).status_code == 403
        assert h.post("/oauth/authorize", data=fields, headers={"Origin": live.url}).status_code == 303
    page = httpx.get(live.url + url, cookies={"rv_session": session_cookie(member)})
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"


# ============================================================================================== token endpoint

def test_token_endpoint_takes_form_encoding_only(live, member):
    r = httpx.post(live.url + "/oauth/token", json={"grant_type": "authorization_code", "code": "x"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    r = exchange(live.url, grant_type="password", username="a", password="b")
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"
    assert r.headers["cache-control"] == "no-store"


def test_pkce_redirect_and_client_must_match_and_codes_are_single_use(live, member):
    cid = register(live.url).json()["client_id"]
    verifier, challenge = pkce()
    url = "/oauth/authorize?" + urlencode(authorize_params(live, cid, CLAUDE_CB, challenge))
    code, _q = code_from(consent(live.url, session_cookie(member), url))
    base = dict(grant_type="authorization_code", code=code, redirect_uri=CLAUDE_CB, client_id=cid,
                code_verifier=verifier)
    other_v, _c = pkce()
    assert exchange(live.url, **dict(base, code_verifier=other_v)).json()["error"] == "invalid_grant"
    assert exchange(live.url, **dict(base, redirect_uri="https://claude.com/api/mcp/auth_callback")).status_code == 400
    assert exchange(live.url, **dict(base, client_id="dcr_someoneelse")).json()["error"] == "invalid_grant"
    assert exchange(live.url, **dict(base, resource="https://x.example/mcp")).json()["error"] == "invalid_target"
    ok = exchange(live.url, **base)
    assert ok.status_code == 200, ok.text
    again = exchange(live.url, **base)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    from reelvault.server import oauth

    assert oauth.verify_access(ok.json()["access_token"]) is None     # a replayed code revokes what it gave out


def test_expired_code_is_refused(live, member):
    from reelvault.server import db

    cid = register(live.url).json()["client_id"]
    verifier, challenge = pkce()
    url = "/oauth/authorize?" + urlencode(authorize_params(live, cid, CLAUDE_CB, challenge))
    code, _q = code_from(consent(live.url, session_cookie(member), url))
    with db.tx() as c:
        c.execute(db.oauth_codes.update().values(expires_at=db.utcnow() - timedelta(seconds=1)))
    r = exchange(live.url, grant_type="authorization_code", code=code, redirect_uri=CLAUDE_CB, client_id=cid,
                 code_verifier=verifier)
    assert r.json()["error"] == "invalid_grant"


def test_refresh_rotates_and_reuse_revokes_the_family(live, member):
    tok = full_flow(live, member)
    cid = tok["client_id"]
    r1 = exchange(live.url, grant_type="refresh_token", refresh_token=tok["refresh_token"], client_id=cid)
    assert r1.status_code == 200, r1.text
    new = r1.json()
    assert new["refresh_token"] != tok["refresh_token"] and new["access_token"] != tok["access_token"]
    from reelvault.server import oauth

    assert oauth.verify_access(new["access_token"]) is not None
    # fix round 2: the same refresh token again within 60 s while its successor is unused is a retry: a fresh pair,
    # and the first answer's tokens keep working
    retry = exchange(live.url, grant_type="refresh_token", refresh_token=tok["refresh_token"], client_id=cid)
    assert retry.status_code == 200 and oauth.verify_access(new["access_token"]) is not None
    used = exchange(live.url, grant_type="refresh_token", refresh_token=retry.json()["refresh_token"], client_id=cid)
    assert used.status_code == 200
    # once a successor was used, reusing a rotated token revokes the whole family
    from reelvault.server import db, security

    with db.tx() as c:   # and the grace window is over
        c.execute(db.oauth_tokens.update().where(
            db.oauth_tokens.c.token_hash == security.sha256_hex(tok["refresh_token"]))
            .values(revoked_at=db.utcnow() - __import__("datetime").timedelta(minutes=5)))
    reuse = exchange(live.url, grant_type="refresh_token", refresh_token=tok["refresh_token"], client_id=cid)
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    assert oauth.verify_access(used.json()["access_token"]) is None
    assert oauth.verify_access(new["access_token"]) is None
    assert oauth.verify_access(tok["access_token"]) is None
    dead = exchange(live.url, grant_type="refresh_token", refresh_token=new["refresh_token"], client_id=cid)
    assert dead.json()["error"] == "invalid_grant"
    junk = exchange(live.url, grant_type="refresh_token", refresh_token="rvr_nothing", client_id=cid)
    assert junk.json()["error"] == "invalid_grant"


def test_expired_access_token_and_disabled_member_get_401(live, member):
    from reelvault.server import db

    tok = full_flow(live, member)
    hdr = {"Authorization": f"Bearer {tok['access_token']}", "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    assert httpx.post(live.mcp_url, json=body, headers=hdr).status_code == 200
    with db.tx() as c:
        c.execute(db.users.update().where(db.users.c.id == member.id).values(status="disabled"))
    assert httpx.post(live.mcp_url, json=body, headers=hdr).status_code == 401
    r = exchange(live.url, grant_type="refresh_token", refresh_token=tok["refresh_token"], client_id=tok["client_id"])
    assert r.json()["error"] == "invalid_grant"
    with db.tx() as c:
        c.execute(db.users.update().where(db.users.c.id == member.id).values(status="active"))
        c.execute(db.oauth_tokens.update().values(expires_at=db.utcnow() - timedelta(seconds=5)))
    assert httpx.post(live.mcp_url, json=body, headers=hdr).status_code == 401


def test_revoke_endpoint(live, member):
    from reelvault.server import oauth

    tok = full_flow(live, member)
    r = httpx.post(live.url + "/oauth/revoke", data={"token": tok["refresh_token"], "client_id": tok["client_id"]})
    assert r.status_code == 200
    assert oauth.verify_access(tok["access_token"]) is None      # revoking the refresh token ends the family
    assert httpx.post(live.url + "/oauth/revoke", data={"token": "rva_unknown"}).status_code == 200


# ============================================================================================== limits

def test_dcr_limits_are_per_client_and_lenient_for_anthropic(live, member, monkeypatch):
    # 50 claude.ai-style registrations from one Anthropic egress IP in an hour all succeed (12.4).
    for i in range(50):
        r = register(live.url, CLAUDE_CB, "Claude", headers={"X-Forwarded-For": "160.79.104.10"})
        assert r.status_code == 201, (i, r.text)
    # Anyone else: 30 per (IP, redirect_uri, client_name) per hour.
    hdr = {"X-Forwarded-For": "203.0.113.7"}
    for i in range(30):
        assert register(live.url, CLAUDE_CB, "Same name", headers=hdr).status_code == 201, i
    over = register(live.url, CLAUDE_CB, "Same name", headers=hdr)
    assert over.status_code == 429 and "retry-after" in {k.lower() for k in over.headers}
    # fix round 2: a new name from the same IP no longer gets around it (plain 30 per IP, whatever the name)
    assert register(live.url, CLAUDE_CB, "Other name", headers=hdr).status_code == 429
    assert register(live.url, CLAUDE_CB, "Other name", headers={"X-Forwarded-For": "203.0.113.8"}).status_code == 201
    from reelvault.server import oauth

    monkeypatch.setattr(oauth, "REGISTER_GLOBAL_HOUR", 81)   # 81 counted so far (refused per-IP calls don't count)
    assert register(live.url, CLAUDE_CB, "Third", headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 429


def test_token_limit_is_per_ip_and_client(live, monkeypatch):
    from reelvault.server import oauth

    monkeypatch.setattr(oauth, "TOKEN_PER_KEY_MINUTE", 3)
    for _ in range(3):
        assert exchange(live.url, grant_type="refresh_token", refresh_token="rvr_x", client_id="dcr_a").status_code == 400
    assert exchange(live.url, grant_type="refresh_token", refresh_token="rvr_x", client_id="dcr_a").status_code == 429
    assert exchange(live.url, grant_type="refresh_token", refresh_token="rvr_x", client_id="dcr_b").status_code == 400


# ============================================================================================== member pages + plugin

def test_connected_apps_and_disconnect(live, member, other_member):
    from reelvault.server import db, oauth, security

    tok = full_flow(live, member)
    with db.tx() as conn:
        value, csrf = security.create_session(conn, member.id, None)
    with httpx.Client(base_url=live.url, cookies={"rv_session": value}, headers={"X-CSRF-Token": csrf}) as h:
        got = h.get("/api/connector/clients").json()
        assert got["url"] == live.mcp_url
        assert [c["client_id"] for c in got["clients"]] == [tok["client_id"]]
        assert got["clients"][0]["name"] == "claude.ai"
        assert h.post("/api/connector/disconnect", json={}).status_code == 400
        r = h.post("/api/connector/disconnect", json={"client_id": tok["client_id"]})
        assert r.status_code == 200 and r.json()["clients"] == []
    assert oauth.verify_access(tok["access_token"]) is None
    assert httpx.get(live.url + "/api/connector/clients").status_code == 401


def test_ai_settings_show_the_connector_as_connected(live, member):
    from reelvault.server import db, security

    full_flow(live, member)
    with db.tx() as conn:
        value, csrf = security.create_session(conn, member.id, None)
    r = httpx.get(live.url + "/api/ai", cookies={"rv_session": value})
    assert r.status_code == 200
    assert r.json()["connector"] == {**r.json()["connector"], "url": live.mcp_url, "connected": True}


def test_plugin_zip_has_this_server(live):
    r = httpx.get(live.url + "/download/claude-plugin.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"reelvault-members/.claude-plugin/plugin.json", "reelvault-members/.mcp.json",
            "reelvault-members/skills/reelvault/SKILL.md"} <= names
    assert not any(n.endswith(".tmpl") for n in names)
    mcpj = json.loads(z.read("reelvault-members/.mcp.json"))
    assert mcpj == {"mcpServers": {"reelvault": {"type": "http", "url": live.mcp_url}}}
    pj = json.loads(z.read("reelvault-members/.claude-plugin/plugin.json"))
    assert pj["name"] == "reelvault-members" and pj["version"] == "1.0.0"
    skill = z.read("reelvault-members/skills/reelvault/SKILL.md").decode()
    assert "<untrusted>" in skill and "save_analysis" in skill and "get_taxonomy" in skill


def test_cleanup_drops_expired_rows_and_unused_dcr_clients(live, member):
    from reelvault.server import db, oauth

    tok = full_flow(live, member)
    stale = register(live.url).json()["client_id"]
    long_ago = db.utcnow() - timedelta(days=40)
    with db.tx() as c:
        c.execute(db.oauth_clients.update().where(db.oauth_clients.c.client_id == stale)
                  .values(created_at=long_ago, last_used_at=None))
    out = oauth.cleanup()
    assert out["clients"] == 1
    with db.read() as c:
        ids = {r.client_id for r in c.execute(db.oauth_clients.select())}
    assert stale not in ids and tok["client_id"] in ids
    out = oauth.cleanup(now=db.utcnow() + timedelta(days=32))
    assert out["tokens"] == 2
