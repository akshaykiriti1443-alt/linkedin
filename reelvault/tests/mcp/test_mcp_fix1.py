"""WP6 fix round 1: reconfirm gate, connecting without an AI mode, cut-off chats, client names, Inspector paths and
plain consent error pages."""
from __future__ import annotations

import html
import re
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from _mcp_support import (authorize_params, consent, data_of, exchange, full_flow, pkce, register, session_cookie,
                          text_of)
from test_mcp_tools import call, calls, seed

from reelvault.server import db, oauth, work


def _due(member):
    db.set_setting("reconfirm_days", 30)
    with db.tx() as c:
        c.execute(db.users.update().where(db.users.c.id == member.id)
                  .values(membership_checked_at=db.utcnow() - timedelta(days=60)))


def test_reconfirm_gate_covers_the_connector(live, member, make_device):
    seed(member)
    tok = full_flow(live, member)
    _due(member)
    r = httpx.get(live.url + "/api/home", cookies={"rv_session": session_cookie(member)})
    assert r.status_code == 403 and r.json()["code"] == "reconfirm_needed"
    res = call(live.mcp_url, tok["access_token"], "list_pending_work")
    assert res.is_error and "community code" in text_of(res)
    r = exchange(live.url, grant_type="refresh_token", refresh_token=tok["refresh_token"], client_id=tok["client_id"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # a new consent shows the reconfirm page instead of a code
    cid = register(live.url).json()["client_id"]
    _, challenge = pkce()
    url = "/oauth/authorize?" + urlencode(authorize_params(live, cid, "https://claude.ai/api/mcp/auth_callback",
                                                           challenge))
    page = httpx.get(live.url + url, cookies={"rv_session": session_cookie(member)})
    assert page.status_code == 403 and "community code" in page.text and "/#login" in page.text
    # device tokens get a 403 (they stay paired)
    _, headers = make_device(member)
    h = httpx.post(live.url + "/api/ext/v2/hello", json={"version": "2.0.0"}, headers=headers)
    assert h.status_code == 403 and h.json()["code"] == "reconfirm_needed"


def test_connecting_with_no_ai_mode_uses_claude(live, member):
    seed(member, mode=None)
    with db.read() as c:
        assert c.execute(db.users.select().where(db.users.c.id == member.id)).first().ai_mode is None
    tok = full_flow(live, member)["access_token"]
    with db.read() as c:
        assert c.execute(db.users.select().where(db.users.c.id == member.id)).first().ai_mode == "claude_connector"
    p = data_of(call(live.mcp_url, tok, "list_pending_work"))
    assert p["tag_reels"] == 3 and p["next_tool"] == "get_tag_batch"
    b = call(live.mcp_url, tok, "get_tag_batch")
    assert not b.is_error and "batch_id" in text_of(b)


def test_pending_hint_when_no_ai_mode(live, member):
    from reelvault.server import mcp_server

    seed(member, mode="claude_connector")
    with db.tx() as c:
        c.execute(db.users.update().where(db.users.c.id == member.id).values(ai_mode=None))
        c.execute(db.reels.update().where(db.reels.c.user_id == member.id).values(tag_state="done"))
    out = mcp_server.pending(member.id)
    assert "Use my Claude plan" in out["hint"] and "another AI" not in out["hint"]


def test_a_cut_off_chat_gets_its_batch_back(live, member):
    ids = seed(member, n=3)
    tok = full_flow(live, member)["access_token"]
    first = text_of(call(live.mcp_url, tok, "get_tag_batch"))     # chat 1 stops before save_tags
    bid = re.search(r"batch_id: (\S+)", first).group(1)
    p, b = calls(live.mcp_url, tok, [("list_pending_work", {}), ("get_tag_batch", {})])
    assert data_of(p)["tag_reels"] == 3
    assert re.search(r"batch_id: (\S+)", text_of(b)).group(1) == bid
    assert set(re.findall(r"reel_[a-z0-9]{16}", text_of(b))) == set(re.findall(r"reel_[a-z0-9]{16}", first))
    # deep reads too
    work.queue_deep(member.id, [ids["reels"][1]])
    r1 = call(live.mcp_url, tok, "get_reel_for_analysis")
    assert not r1.is_error and ids["reels"][1] in text_of(r1)
    r2 = call(live.mcp_url, tok, "get_reel_for_analysis")
    assert not r2.is_error and ids["reels"][1] in text_of(r2)


def test_known_claude_clients_have_plain_names():
    cc = oauth.Client("https://claude.ai/oauth/claude-code-client-metadata", "cimd",
                      ["http://localhost/callback", "http://127.0.0.1/callback"])
    web = oauth.Client("https://claude.ai/oauth/mcp-oauth-client-metadata", "cimd",
                       ["https://claude.ai/api/mcp/auth_callback"])
    other = oauth.Client("https://claude.ai/oauth/some-new-client", "cimd", ["http://localhost/callback"])
    assert cc.host == "Claude Code on your computer"
    assert web.host == "Claude (claude.ai or the desktop app)"
    assert other.host == "Claude Code on your computer"
    assert oauth.Client("https://apps.example.com/c.json", "cimd", ["https://apps.example.com/cb"]).host == \
        "apps.example.com"


def test_mcp_inspector_can_register(live):
    r = httpx.post(live.url + "/oauth/register", json={
        "redirect_uris": ["http://localhost:6274/oauth/callback", "http://localhost:6274/oauth/callback/debug"],
        "client_name": "MCP Inspector", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    assert r.status_code == 201, r.text
    assert not oauth.redirect_allowed("http://localhost:6274/evil")
    assert not oauth.redirect_allowed("https://localhost/oauth/callback")


def test_consent_error_pages_use_plain_words(live, member):
    ck = {"rv_session": session_cookie(member)}
    page = httpx.get(live.url + "/oauth/authorize", cookies=ck)
    assert oauth.MEMBER_INCOMPLETE in page.text and "client_id" not in page.text.split("<main>")[1]
    _, challenge = pkce()
    url = "/oauth/authorize?" + urlencode(authorize_params(live, "dcr_gone", "https://claude.ai/api/mcp/auth_callback",
                                                           challenge))
    page = httpx.get(live.url + url, cookies=ck)
    assert oauth.MEMBER_UNKNOWN_APP in page.text and "not registered" not in page.text
    url = "/oauth/authorize?" + urlencode(authorize_params(live, "http://127.0.0.1:9/nothing.json",
                                                           "http://localhost/callback", challenge))
    page = httpx.get(live.url + url, cookies=ck)
    assert html.escape(oauth.MEMBER_CIMD) in page.text and "metadata" not in page.text, page.text.split("<main>")[1]
    assert consent is not None


def test_tools_list_stays_small(live, member):
    """Claude loads tools/list into every chat with the connector on: it was ~60,000 characters, mostly the typed
    save_output union. data is now a plain object checked on the server, its schema comes with the brief."""
    import json as _json

    from _mcp_support import mcp_client, run

    tok = full_flow(live, member)["access_token"]

    async def go():
        async with mcp_client(live.mcp_url, tok) as c:
            return await c.list_tools()

    tools = run(go).tools
    sizes = {t.name: len(_json.dumps(t.model_dump(mode="json", exclude_none=True))) for t in tools}
    total = len(_json.dumps([t.model_dump(mode="json", exclude_none=True) for t in tools]))
    print("tools/list chars", total, sorted(sizes.items(), key=lambda x: -x[1])[:5])
    assert sizes["save_output"] < 2000
    assert total < 40000, sizes
