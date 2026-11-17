"""Fake Instagram (+ CDN, Skool, LinkedIn) for the extension tests (DESIGN-SAAS.md 12.3). WP2.

    python tests/ext/fake_ig_server.py --port 9143 --cert-dir /tmp/x

HTTPS on 127.0.0.1 with a self-signed certificate. Chromium reaches it through
--host-resolver-rules="MAP www.instagram.com 127.0.0.1:9143, MAP scontent.cdninstagram.com 127.0.0.1:9143, ..."
and --ignore-certificate-errors, so every request still carries Host: www.instagram.com and the instagram.com
cookies. Answers are built with tests/fixtures/ig/make_fixtures.py (the same shapes as the JSON fixtures).

Control (Host 127.0.0.1:<port>):  POST /__ctl/scenario {"set": [...]} · POST /__ctl/reset · GET /__ctl/log
Scenarios: 429_on_page2, login_redirect, challenge_redirect, suspended_redirect, cdn_429, checkpoint, feedback_required, html_200, page_size_12, page_size_400,
stale_cursor, cursor_error, suggested_reels, collab, bigint_pk, ds_user_httponly (cookie set by the test),
page_trap (robots.txt is an HTML page whose script patches window.fetch), big_video (2nd video is 60 MB),
clips_404 (clips/user answers 404: the reader must switch to feed/user), slow (0.8 s per API answer).

LOGGED-OUT reads (DESIGN-FETCH.md 3.2, 3.5), built with tests/fixtures/ig/logged_out/make_lo_fixtures.py:
GET /<handle>/ · GET /<handle>/reels/ · POST /graphql/query (reels tab) · POST /api/graphql (posts, post detail,
media detail) · GET /api/v1/users/web_profile_info/ · GET /reel/<code>/embed/captioned/.
Cookie detector: a logged-out request that carries a Cookie or X-CSRFToken header gets HTTP 400 "cookie_leak",
is marked "leak" in the log and counted in GET /__ctl/log -> "leaks". On the two paths that the logged-in reader
also uses (/<handle>/ and web_profile_info) a request is logged-out when it has no X-IG-WWW-Claim header (the
logged-in reader always sends one).
Logged-out scenarios: lo_login_wall (pages redirect to /accounts/login/, JSON answers 401 require_login),
lo_rate_429 and lo_wait_401 (every logged-out answer but the profile page), lo_rate_after_<n> (429 from logged-out
call n+1 on), lo_no_embedded (pages load without embedded data), lo_reels_blocked (the reels page has no data, the
reels query answers 401), lo_no_views (post and media detail without view counts), lo_docid_rotated (the first doc
id of every query is dead), lo_gated (the 2nd reel is gated), lo_web_refused (web_profile_info answers 400),
lo_codes_only (timeline stubs carry only `code`), lo_private, lo_challenge (pages redirect to /challenge/),
lo_slow (0.6 s per logged-out answer).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("rv_mkfx", ROOT / "tests" / "fixtures" / "ig" / "make_fixtures.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

_lspec = importlib.util.spec_from_file_location(
    "rv_mklofx", ROOT / "tests" / "fixtures" / "ig" / "logged_out" / "make_lo_fixtures.py")
lo = importlib.util.module_from_spec(_lspec)
_lspec.loader.exec_module(lo)

APP_ID = "936619743392459"
NOW = int(time.time())
TIMELINE = mk.account_timeline(250, now=NOW)
BY_PK = {m["pk"]: m for m in TIMELINE}
PK = mk.TARGET["pk"]
USERS = {u["username"]: u for u in (mk.TARGET, mk.PARTNER, mk.OTHER)}
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 4096
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1024

STATE = {"scenarios": set(), "log": [], "lock": threading.Lock(), "leaks": 0, "lo_calls": 0}

# ---------------------------------------------------------------------------------------------- logged-out data
IG_BRAND = {"pk": 25025, "username": "instagram", "full_name": "Instagram"}   # the probe's well-known account


def _mixed(reels: list[dict], owner: dict) -> list[dict]:
    """The profile timeline: the reels plus a photo at every 4th place (photos must be skipped)."""
    tl = []
    for i, m in enumerate(reels):
        if i % 4 == 3:
            tl.append(lo.photo(owner, m["taken_at"] + 600, 500 + i))
        tl.append(m)
    return tl


def _lo_account(u: dict, reels: list[dict]) -> dict:
    return {"u": u, "reels": reels, "tl": _mixed(reels, u)}


LO_ACCOUNTS = {
    mk.TARGET["username"]: _lo_account(mk.TARGET, TIMELINE),
    IG_BRAND["username"]: _lo_account(IG_BRAND, mk.account_timeline(30, seed=11, owner=IG_BRAND, now=NOW - 5000)),
}
assert sum(len(a["tl"]) for a in LO_ACCOUNTS.values()) == len({m["pk"] for a in LO_ACCOUNTS.values() for m in a["tl"]})
LO_BY_CODE = {m["code"]: (a, m) for a in LO_ACCOUNTS.values() for m in a["tl"]}
LO_BY_PK = {str(m["pk"]): (a, m) for a in LO_ACCOUNTS.values() for m in a["tl"]}
LO_BY_USER_PK = {str(a["u"]["pk"]): a for a in LO_ACCOUNTS.values()}
LO_BY_PAGE_ID = {lo.page_id(a["u"]): a for a in LO_ACCOUNTS.values()}
LO_DOC = {doc: strategy for strategy, ids in lo.DOC_IDS.items() for doc in ids}
LO_FIRST_DOC = {ids[0] for ids in lo.DOC_IDS.values()}
RESERVED = {"accounts", "challenge", "api", "web", "reel", "reels", "p", "graphql", "explore", "robots.txt"}
PROFILE_RE = re.compile(r"^/([a-z0-9._]{1,30})/$")
REELS_PAGE_RE = re.compile(r"^/([a-z0-9._]{1,30})/reels/$")
EMBED_RE = re.compile(r"^/reel/([A-Za-z0-9_-]{5,20})/embed/captioned/$")


def lo_rate_after() -> int | None:
    for sc in STATE["scenarios"]:
        if sc.startswith("lo_rate_after_"):
            return int(sc.rsplit("_", 1)[1])
    return None


def make_cert(cert_dir: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cert_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "www.instagram.com")])
    sans = [x509.DNSName(h) for h in ("www.instagram.com", "scontent.cdninstagram.com", "www.skool.com",
                                      "www.linkedin.com", "localhost")]
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=7)).add_extension(x509.SubjectAlternativeName(sans), False)
            .sign(key, hashes.SHA256()))
    c, k = cert_dir / "cert.pem", cert_dir / "key.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return c, k


def on(name: str) -> bool:
    return name in STATE["scenarios"]


def clips_page(offset: int, size: int) -> dict:
    items = TIMELINE[offset:offset + size]
    if offset == 0 and on("bigint_pk") and items:
        items = [mk.media(mk.TARGET, NOW - 3600, 77, views=1_830_000, likes=51_200, comments=930,
                          pk=mk.BIGINT_PK)] + items[1:]
    if on("suggested_reels"):
        items = list(items)
        for k in range(2):
            items.insert(2 + k * 3, mk.media(mk.OTHER, NOW - (offset + k) * 3600, 900 + offset + k,
                                             views=2_000_000, likes=80_000, comments=900))
    if offset == 0 and on("collab"):
        items = [mk.media(mk.PARTNER, NOW - 2 * mk.DAY, 801, views=900_000, likes=30_000, comments=700,
                          coauthors=[mk.TARGET], caption="Collab with @scaleit.demo")] + list(items)
    more = offset + size < len(TIMELINE)
    return mk.clips_page(items, mk.cursor_for(offset + size) if more else None, more)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    # ------------------------------------------------------------------------------------------ plumbing
    def _send(self, status, body=b"", ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = mk.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _record(self, body: bytes):
        u = urlparse(self.path)
        cookies = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k] = v
        entry = {"t": time.time(), "host": (self.headers.get("Host") or "").split(":")[0], "method": self.command,
                 "path": u.path, "query": {k: v[0] for k, v in parse_qs(u.query).items()},
                 "headers": {k.lower(): v for k, v in self.headers.items()}, "cookies": cookies,
                 "form": {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
                 if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded") else {}}
        with STATE["lock"]:
            STATE["log"].append(entry)
        return entry

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        self._route(b"")

    def do_POST(self):
        self._route(self._body())

    def _route(self, body: bytes):
        host = (self.headers.get("Host") or "").split(":")[0]
        u = urlparse(self.path)
        if host in ("127.0.0.1", "localhost") and u.path.startswith("/__ctl/"):
            return self._ctl(u, body)
        e = self._record(body)
        try:
            if host == "scontent.cdninstagram.com":
                return self._cdn(u)
            if host == "www.skool.com":
                return self._skool(u)
            if host == "www.linkedin.com":
                return self._linkedin(u)
            return self._ig(u, e)
        finally:
            e["t_end"] = time.time()  # with "t": proves one Instagram request at a time

    # ------------------------------------------------------------------------------------------ control
    def _ctl(self, u, body):
        if u.path == "/__ctl/scenario":
            req = json.loads(body or b"{}")
            STATE["scenarios"] = set(req.get("set") or [])
            STATE["lo_calls"] = 0
            return self._send(200, {"ok": True, "set": sorted(STATE["scenarios"])})
        if u.path == "/__ctl/reset":
            with STATE["lock"]:
                STATE["log"].clear()
            STATE["scenarios"] = set()
            STATE["leaks"] = 0
            STATE["lo_calls"] = 0
            return self._send(200, {"ok": True})
        if u.path == "/__ctl/log":
            with STATE["lock"]:
                return self._send(200, {"log": STATE["log"], "leaks": STATE["leaks"]})
        if u.path == "/__ctl/timeline":
            return self._send(200, {"pks": [str(m["pk"]) for m in TIMELINE],
                                    "lo": {h: {"reels": [str(m["pk"]) for m in a["reels"]],
                                               "codes": [m["code"] for m in a["reels"]],
                                               "posts": [m["code"] for m in a["tl"]]}
                                           for h, a in LO_ACCOUNTS.items()}})
        return self._send(404, {"error": "no"})

    # ------------------------------------------------------------------------------------------ Instagram
    def _ig(self, u, e):
        p, q = u.path, e["query"]
        if p == "/robots.txt":
            if on("page_trap"):
                html = ("<!doctype html><html><head><script>window.__seen = [];const f = window.fetch;"
                        "window.fetch = function(u, o){ navigator.sendBeacon('/__trap', String(u)); return f(u, o); };"
                        "</script></head><body>robots</body></html>")
                return self._send(200, html, "text/html; charset=utf-8")
            return self._send(200, "User-agent: *\nDisallow: /\n", "text/plain; charset=utf-8")
        if p == "/__trap":
            return self._send(204, b"", "text/plain")
        kind = self._lo_kind(p, e)
        if kind:
            return self._lo(kind, p, q, e)
        if p.startswith("/accounts/login"):
            return self._send(200, lo.login_page_html(), "text/html; charset=utf-8")
        if p.startswith("/challenge/"):
            return self._send(200, "<!doctype html><title>Confirm it's you</title>", "text/html; charset=utf-8")
        if p == "/":
            return self._send(200, "<!doctype html><title>Instagram</title><p>fake</p>", "text/html")
        if p.startswith("/api/") or p.startswith("/web/"):
            if on("slow"):
                time.sleep(0.8)
            if e["headers"].get("x-ig-app-id") != APP_ID:
                return self._send(400, {"message": "useragent mismatch", "status": "fail"})
            if on("login_redirect"):
                return self._send(302, b"", "text/html", {"Location": "https://www.instagram.com/accounts/login/"})
            if on("html_200"):
                return self._send(200, "<!doctype html><html><body>Something went wrong</body></html>", "text/html")
        if p == "/api/v1/clips/user/":
            return self._clips(e)
        if p.startswith("/api/v1/feed/user/"):
            return self._feed(p, q)
        if p == "/api/v1/users/web_profile_info/":
            u_ = USERS.get(q.get("username", ""))
            if not u_:
                return self._send(404, {"message": "User not found", "status": "fail"})
            return self._send(200, mk.web_profile_info(u_, followers=182_000 if u_ is mk.TARGET else 41_000))
        if (p == "/api/v1/accounts/current_user/" or (p.startswith("/api/v1/users/") and p.endswith("/info/"))) \
                and not e["cookies"].get("sessionid"):     # signed out (cookies cleared): what Instagram answers
            return self._send(403, {"message": "login_required", "require_login": True, "status": "fail"})
        if p == "/api/v1/accounts/current_user/":
            return self._send(200, mk.current_user())
        if p.startswith("/api/v1/users/") and p.endswith("/info/"):
            uid = p.split("/")[4]
            if uid == str(mk.VIEWER["pk"]):
                return self._send(200, mk.user_info(mk.VIEWER, followers=12))
            for u_ in USERS.values():
                if str(u_["pk"]) == uid:
                    return self._send(200, mk.user_info(u_, followers=182_000))
            return self._send(404, {"message": "Target user not found", "status": "fail"})
        if p == "/web/search/topsearch/":
            return self._send(200, mk.topsearch([mk.TARGET, mk.PARTNER, mk.OTHER], q.get("query", "")))
        if p == "/api/v1/fbsearch/web/top_serp/":
            return self._send(200, mk.top_serp([TIMELINE[2]]))
        if p == "/api/v1/discover/chaining/":
            return self._send(200, mk.chaining([mk.PARTNER, mk.OTHER]))
        if p.startswith("/api/v1/media/") and p.endswith("/info/"):
            pk = int(p.split("/")[4])
            m = BY_PK.get(pk)
            if m is None:
                return self._send(404, {"message": "Media not found or unavailable", "status": "fail"})
            return self._send(200, mk.media_info(m))
        name = p.strip("/")
        if name in USERS:
            return self._send(200, mk.profile_page_html(USERS[name]), "text/html; charset=utf-8")
        return self._send(404, {"message": "not found", "status": "fail"})

    # ------------------------------------------------------------------------------------------ logged out
    @staticmethod
    def _lo_kind(p: str, e: dict) -> str | None:
        """Which logged-out strategy request this is, or None for a logged-in / other request."""
        if p in ("/api/graphql", "/graphql/query"):
            return "gql"
        if REELS_PAGE_RE.match(p):
            return "reels_page"
        if EMBED_RE.match(p):
            return "embed"
        if "x-ig-www-claim" in e["headers"]:      # the logged-in reader (igFetch) always sends it
            return None
        if p == "/api/v1/users/web_profile_info/":
            return "web_profile"
        m = PROFILE_RE.match(p)
        if m and m.group(1) not in RESERVED:
            return "profile_page"
        return None

    def _lo(self, kind: str, p: str, q: dict, e: dict):
        e["lo"] = kind
        html = "text/html; charset=utf-8"
        # The cookie detector: a logged-out request must carry no cookie and no CSRF token.
        if e["headers"].get("cookie") or e["headers"].get("x-csrftoken") or e["cookies"]:
            e["leak"] = True
            with STATE["lock"]:
                STATE["leaks"] += 1
            return self._send(400, {"message": "cookie_leak", "status": "fail"})
        with STATE["lock"]:
            STATE["lo_calls"] += 1
            n = STATE["lo_calls"]
        page = kind in ("profile_page", "reels_page", "embed")
        if on("lo_slow"):
            time.sleep(0.6)
        if not page and e["headers"].get("x-ig-app-id") != APP_ID:
            return self._send(400, {"message": "useragent mismatch", "status": "fail"})
        after = lo_rate_after()
        if after is not None and n > after:
            return self._send(429, {"message": "Please wait a few minutes before you try again.", "status": "fail"})
        if on("lo_challenge") and page:
            return self._send(302, b"", "text/html", {"Location": "https://www.instagram.com/challenge/123/abc/"})
        if on("lo_login_wall"):
            if page:
                return self._send(302, b"", "text/html", {"Location": "https://www.instagram.com/accounts/login/"})
            return self._send(401, lo.LOGIN_BODY)
        if kind != "profile_page":
            if on("lo_rate_429"):
                return self._send(429, {"message": "Please wait a few minutes before you try again.",
                                        "status": "fail"})
            if on("lo_wait_401"):
                return self._send(401, lo.WAIT_BODY)
        if kind == "profile_page":
            a = LO_ACCOUNTS.get(PROFILE_RE.match(p).group(1))
            if a is None:
                return self._send(404, "<!doctype html><title>Page not found</title>", html)
            return self._send(200, lo.profile_page_html(a["u"], a["tl"], private=on("lo_private"),
                                                        codes_only=on("lo_codes_only"),
                                                        embedded=not on("lo_no_embedded")), html)
        if kind == "reels_page":
            a = LO_ACCOUNTS.get(REELS_PAGE_RE.match(p).group(1))
            if a is None:
                return self._send(404, "<!doctype html><title>Page not found</title>", html)
            if on("lo_no_embedded") or on("lo_reels_blocked"):
                return self._send(200, lo.profile_page_html(a["u"], a["tl"], embedded=False), html)
            return self._send(200, lo.reels_page_html(a["u"], a["reels"]), html)
        if kind == "embed":
            hit = LO_BY_CODE.get(EMBED_RE.match(p).group(1))
            if hit is None:
                return self._send(404, "<!doctype html><title>Page not found</title>", html)
            m = hit[1]
            if on("lo_no_views"):
                m = dict(m, play_count=None)
            return self._send(200, lo.embed_html(m, with_data=not on("lo_no_embedded")), html)
        if kind == "web_profile":
            a = LO_ACCOUNTS.get(q.get("username", ""))
            if a is None:
                return self._send(404, {"message": "User not found", "status": "fail"})
            if on("lo_web_refused"):
                return self._send(400, {"message": "Bad request", "status": "fail"})
            return self._send(200, lo.web_profile_info(a["u"], a["tl"]))
        return self._lo_gql(p, e)

    def _lo_gql(self, p: str, e: dict):
        f = e["form"]
        if f.get("lsd") != lo.LSD or e["headers"].get("x-fb-lsd") != lo.LSD:
            return self._send(400, {"message": "bad lsd", "status": "fail"})
        strategy = LO_DOC.get(f.get("doc_id", ""))
        try:
            v = json.loads(f.get("variables") or "{}")
        except ValueError:
            return self._send(400, {"message": "bad variables", "status": "fail"})
        if strategy is None or (on("lo_docid_rotated") and f.get("doc_id") in LO_FIRST_DOC):
            return self._send(200, lo.GQL_ERROR)
        if (p == "/graphql/query") != (strategy == "lo_reels_gql"):
            return self._send(404, {"message": "not found", "status": "fail"})
        if strategy == "lo_reels_gql":
            if on("lo_reels_blocked"):
                return self._send(401, lo.LOGIN_BODY)
            a = LO_BY_USER_PK.get(str((v.get("data") or {}).get("target_user_id")))
            if a is None:
                return self._send(200, lo.GQL_ERROR)
            off = mk.offset_of(v["after"], "lor") if v.get("after") else 0
            return self._send(200, lo.reels_gql(a["reels"], off or 0))
        if strategy == "lo_posts_gql":
            a = LO_BY_PAGE_ID.get(str(v.get("id")))
            if a is None:
                return self._send(200, {"data": {"node": None}, "extensions": {"is_final": True}})
            off = mk.offset_of(v["after"], "lo") if v.get("after") else 0
            return self._send(200, lo.posts_gql(a["u"], a["tl"], off or 0, codes_only=on("lo_codes_only")))
        hit = LO_BY_CODE.get(v.get("shortcode", "")) if strategy == "lo_post_gql" else LO_BY_PK.get(str(v.get("media_id")))
        if hit is not None and on("lo_gated") and hit[1]["pk"] == hit[0]["reels"][1]["pk"]:
            hit = None
        m = hit[1] if hit else None
        if strategy == "lo_post_gql":
            return self._send(200, lo.post_gql(m, views="none" if on("lo_no_views") else "both"))
        return self._send(200, lo.media_gql(m, views="none" if on("lo_no_views") else "view_count"))

    def _clips(self, e):
        f = e["form"]
        if e["headers"].get("x-csrftoken") != e["cookies"].get("csrftoken") or not f.get("target_user_id"):
            return self._send(403, {"message": "CSRF token missing or incorrect", "status": "fail"})
        if on("challenge_redirect"):  # what www pages do on a checkpoint
            return self._send(302, b"", "text/html", {"Location": "https://www.instagram.com/challenge/123/abc/"})
        if on("suspended_redirect"):
            return self._send(302, b"", "text/html", {"Location": "https://www.instagram.com/accounts/suspended/"})
        if on("checkpoint"):
            return self._send(400, {"message": "checkpoint_required", "checkpoint_url": "/challenge/123/abc/",
                                    "lock": True, "status": "fail"})
        if on("feedback_required"):
            return self._send(400, mk.FEEDBACK_REQUIRED)
        if on("clips_404"):
            return self._send(404, b"", "text/html")
        if f.get("target_user_id") != str(PK):
            if f.get("target_user_id") in {str(mk.PARTNER["pk"]), str(mk.OTHER["pk"])}:
                owner = mk.PARTNER if f["target_user_id"] == str(mk.PARTNER["pk"]) else mk.OTHER
                items = [mk.media(owner, NOW - k * 2 * mk.DAY, 3000 + k, views=90_000 + k * 1000, likes=2000,
                                  comments=30) for k in range(12)]
                return self._send(200, mk.clips_page(items, mk.cursor_for(12), True))
            return self._send(200, mk.clips_page([], None, False))
        size = int(f.get("page_size") or 12)
        if on("page_size_400") and size != 12:
            return self._send(400, {"message": "Invalid page_size parameter", "status": "fail"})
        if on("page_size_12"):
            size = 12
        max_id = f.get("max_id")
        offset = 0
        if max_id:
            if on("cursor_error"):
                return self._send(400, {"message": "Invalid max_id", "status": "fail"})
            if on("stale_cursor"):
                offset = 0
            else:
                offset = mk.offset_of(max_id) or 0
        if on("429_on_page2") and offset > 0:
            return self._send(429, {"message": "Please wait a few minutes before you try again.", "status": "fail"})
        return self._send(200, clips_page(offset, size))

    def _feed(self, p, q):
        if p.split("/")[5] != str(PK):
            return self._send(200, mk.feed_page([], int(p.split("/")[5]), False))
        start = 0
        mx = q.get("max_id")
        if mx:
            mpk = int(mx.split("_")[0])
            idx = next((i for i, m in enumerate(TIMELINE) if m["pk"] == mpk), None)
            if idx is None:  # an id-based cursor for a reel we don't list: everything older than it
                idx = next((i - 1 for i, m in enumerate(TIMELINE) if m["pk"] < mpk), len(TIMELINE) - 1)
            start = idx + 1
        items = TIMELINE[start:start + 12]
        more = start + 12 < len(TIMELINE)
        return self._send(200, mk.feed_page(items, PK, more))

    # ------------------------------------------------------------------------------------------ CDN
    def _cdn(self, u):
        name = u.path.rsplit("/", 1)[-1]
        if name.endswith(".mp4"):
            if on("cdn_429"):
                return self._send(429, b"", "text/plain")
            pk = int(name.split("_")[0])
            if on("big_video") and pk == TIMELINE[1]["pk"]:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(60 * 1024 * 1024))
                self.end_headers()
                try:
                    for _ in range(4):
                        self.wfile.write(b"\x00" * 65536)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
                return
            return self._send(200, MP4, "video/mp4")
        if name.endswith(".jpg"):
            return self._send(200, JPG, "image/jpeg")
        return self._send(404, b"", "text/plain")

    # ------------------------------------------------------------------------------------------ Skool, LinkedIn
    def _skool(self, u):
        if u.path == "/@mia-member":
            nd = {"props": {"pageProps": {"currentUser": {"firstName": "Mia", "lastName": "Member",
                                                          "metadata": {"bio": "I run a Meta ads agency for dentists.",
                                                                       "location": "Leeds"}},
                                          "currentGroup": None, "secret": "x" * 10}},
                  "page": "/[handle]", "buildId": "fake"}
            html = (f"<!doctype html><html><head><title>Mia Member · Skool</title></head><body><div id=\"__next\">"
                    f"</div><script id=\"__NEXT_DATA__\" type=\"application/json\">{json.dumps(nd)}</script>"
                    f"</body></html>")
            return self._send(200, html, "text/html; charset=utf-8")
        return self._send(302, b"", "text/html", {"Location": "https://www.skool.com/login"}) \
            if u.path != "/login" else self._send(200, "<html><title>Log in</title></html>", "text/html")

    def _linkedin(self, u):
        if u.path == "/in/me/":
            return self._send(302, b"", "text/html", {"Location": "https://www.linkedin.com/in/mia-member/"})
        if u.path == "/in/mia-member/":
            return self._send(200, "<!doctype html><html><head><title>Mia Member | LinkedIn</title></head><body>"
                                   "<nav>Home</nav><main><h1>Mia   Member</h1>\n<p>Founder, Dentist Ads Co.</p>"
                                   "</main></body></html>", "text/html; charset=utf-8")
        return self._send(404, b"", "text/html")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9143)
    ap.add_argument("--cert-dir", required=True)
    a = ap.parse_args(argv)
    cert, key = make_cert(Path(a.cert_dir))
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    srv.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"fake instagram on https://127.0.0.1:{a.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
