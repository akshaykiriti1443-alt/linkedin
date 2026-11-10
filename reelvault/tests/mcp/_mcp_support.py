"""Helpers for the WP6 tests: sessions, PKCE, the consent click, a CIMD document server and the mcp client."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import threading
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import anyio
import httpx

CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
PORTS = range(9220, 9240)


def free_port() -> int:
    """A free port in WP6's range 9220-9239 (the live server, CIMD servers, loopback redirects)."""
    for p in PORTS:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port in 9220-9239")


def run(fn, *args):
    return anyio.run(fn, *args)


def session_cookie(user) -> str:
    from reelvault.server import db, security

    with db.tx() as conn:
        value, _csrf = security.create_session(conn, user.id, None)
    return value


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class _Form(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields: dict[str, str] = {}
        self.action = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.action = a.get("action")
        if tag == "input" and a.get("type") == "hidden":
            self.fields[a["name"]] = a.get("value") or ""


def parse_form(html: str) -> dict:
    p = _Form()
    p.feed(html)
    return p.fields


def authorize_params(live, client_id: str, redirect_uri: str, challenge: str, state: str = "st-1", **extra) -> dict:
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": challenge,
         "code_challenge_method": "S256", "state": state, "scope": "reelvault", "resource": live.mcp_url}
    q.update(extra)
    return {k: v for k, v in q.items() if v is not None}


def consent(base: str, cookie: str, authorize_url: str, decision: str = "allow") -> httpx.Response:
    """Open the consent page as the signed-in member and press Allow (or Cancel). Returns the redirect response."""
    with httpx.Client(base_url=base, cookies={"rv_session": cookie}, follow_redirects=False, timeout=20) as h:
        page = h.get(authorize_url)
        assert page.status_code == 200, page.text[:500]
        fields = parse_form(page.text)
        fields["decision"] = decision
        return h.post("/oauth/authorize", data=fields, headers={"Origin": base})


def code_from(resp: httpx.Response) -> tuple[str | None, dict]:
    assert resp.status_code == 303, (resp.status_code, resp.text[:300])
    q = parse_qs(urlparse(resp.headers["location"]).query)
    return (q.get("code") or [None])[0], {k: v[0] for k, v in q.items()}


def register(base: str, redirect_uri: str = CLAUDE_CB, name: str = "Claude", headers: dict | None = None):
    return httpx.post(base + "/oauth/register", json={"redirect_uris": [redirect_uri], "client_name": name,
                                                      "token_endpoint_auth_method": "none",
                                                      "grant_types": ["authorization_code", "refresh_token"],
                                                      "response_types": ["code"]},
                      headers=headers or {}, timeout=20)


def exchange(base: str, **form) -> httpx.Response:
    return httpx.post(base + "/oauth/token", data=form, timeout=20)


def full_flow(live, user, *, client_id: str | None = None, redirect_uri: str = CLAUDE_CB) -> dict:
    """DCR (unless client_id is given) -> consent -> code -> tokens. Returns the token JSON (+ client_id)."""
    if client_id is None:
        r = register(live.url, redirect_uri)
        assert r.status_code == 201, r.text
        client_id = r.json()["client_id"]
    verifier, challenge = pkce()
    url = "/oauth/authorize?" + urlencode(authorize_params(live, client_id, redirect_uri, challenge))
    code, q = code_from(consent(live.url, session_cookie(user), url))
    assert code and q["state"] == "st-1"
    t = exchange(live.url, grant_type="authorization_code", code=code, redirect_uri=redirect_uri,
                 client_id=client_id, code_verifier=verifier, resource=live.mcp_url)
    assert t.status_code == 200, t.text
    out = t.json()
    out["client_id"] = client_id
    return out


# ============================================================================================== CIMD document server

class CimdServer:
    """Serves one client metadata document at http://127.0.0.1:<port>/oauth/client.json."""

    def __init__(self, port: int, doc_fn):
        self.port = port
        self.url = f"http://127.0.0.1:{port}/oauth/client.json"
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits += 1
                if self.path != "/oauth/client.json":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(doc_fn(outer.url)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ============================================================================================== mcp client

@asynccontextmanager
async def mcp_client(url: str, token: str | None):
    import httpx2

    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers, timeout=240) as hc:
        async with Client(streamable_http_client(url, http_client=hc)) as client:
            yield client


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text")


def images_of(result) -> list:
    return [c for c in result.content if getattr(c, "type", "") == "image"]


def result_chars(result) -> int:
    n = 0
    for c in result.content:
        n += len(getattr(c, "text", "") or "") + len(getattr(c, "data", "") or "")
    return n


def data_of(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(text_of(result))


UNTRUSTED = re.compile(r"<untrusted\b[^>]*>.*?</untrusted>", re.S)


def outside_untrusted(text: str) -> str:
    return UNTRUSTED.sub("", text)
