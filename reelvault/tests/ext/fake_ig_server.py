"""Fake Instagram (+ CDN, Skool, LinkedIn) for the extension tests (DESIGN-SAAS.md 12.3). WP2.

    python tests/ext/fake_ig_server.py --port 9143 --cert-dir /tmp/x

HTTPS on 127.0.0.1 with a self-signed certificate. Chromium reaches it through
--host-resolver-rules="MAP www.instagram.com 127.0.0.1:9143, MAP scontent.cdninstagram.com 127.0.0.1:9143, ..."
and --ignore-certificate-errors, so every request still carries Host: www.instagram.com and the instagram.com
cookies. Answers are built with tests/fixtures/ig/make_fixtures.py (the same shapes as the JSON fixtures).

Control (Host 127.0.0.1:<port>):  POST /__ctl/scenario {"set": [...]} · POST /__ctl/reset · GET /__ctl/log
Scenarios: 429_on_page2, login_redirect, checkpoint, feedback_required, html_200, page_size_12, page_size_400,
stale_cursor, cursor_error, suggested_reels, collab, bigint_pk, ds_user_httponly (cookie set by the test),
page_trap (robots.txt is an HTML page whose script patches window.fetch), big_video (2nd video is 60 MB),
clips_404 (clips/user answers 404: the reader must switch to feed/user), slow (0.8 s per API answer).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
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

APP_ID = "936619743392459"
NOW = int(time.time())
TIMELINE = mk.account_timeline(250, now=NOW)
BY_PK = {m["pk"]: m for m in TIMELINE}
PK = mk.TARGET["pk"]
USERS = {u["username"]: u for u in (mk.TARGET, mk.PARTNER, mk.OTHER)}
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 4096
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1024

STATE = {"scenarios": set(), "log": [], "lock": threading.Lock()}


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
            return self._send(200, {"ok": True, "set": sorted(STATE["scenarios"])})
        if u.path == "/__ctl/reset":
            with STATE["lock"]:
                STATE["log"].clear()
            STATE["scenarios"] = set()
            return self._send(200, {"ok": True})
        if u.path == "/__ctl/log":
            with STATE["lock"]:
                return self._send(200, {"log": STATE["log"]})
        if u.path == "/__ctl/timeline":
            return self._send(200, {"pks": [str(m["pk"]) for m in TIMELINE]})
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

    def _clips(self, e):
        f = e["form"]
        if e["headers"].get("x-csrftoken") != e["cookies"].get("csrftoken") or not f.get("target_user_id"):
            return self._send(403, {"message": "CSRF token missing or incorrect", "status": "fail"})
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
