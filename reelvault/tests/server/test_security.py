"""Security core (DESIGN-SAAS.md 11, 12.1 test_security.py)."""
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from reelvault.ai import net
from reelvault.server import db, security, settings, storage


def test_argon2_hash_and_verify():
    h = security.hash_password("a long enough password")
    assert h.startswith("$argon2id$")
    assert security.verify_password(h, "a long enough password")
    assert not security.verify_password(h, "wrong password!!")
    assert not security.verify_password("garbage", "x")


def test_password_rules():
    assert security.password_problem("short") is not None
    assert security.password_problem("1234567890") is not None  # common
    assert security.password_problem("aaaaaaaaaaaa") is not None
    assert security.password_problem("x" * 129) is not None
    assert security.password_problem("plum-river-velvet-42") is None


def test_aes_gcm_round_trip_wrong_ad_and_rotation(rv_env, monkeypatch, make_user):
    ct, nonce, kid = security.encrypt_secret("sk-test-123456", "usr_a|aic_1|openai")
    assert kid == "test" and len(nonce) == 12 and b"sk-test" not in ct
    assert security.decrypt_secret(ct, nonce, kid, "usr_a|aic_1|openai") == "sk-test-123456"
    with pytest.raises(security.KeyUnavailable):
        security.decrypt_secret(ct, nonce, kid, "usr_b|aic_1|openai")  # copied to another row
    with pytest.raises(security.KeyUnavailable):
        security.decrypt_secret(ct, nonce, "nokid", "usr_a|aic_1|openai")

    # rotation: add a new current key, keep the old one second; rotate re-encrypts the saved row
    u = make_user()
    cid = db.new_id("aic")
    aad = security.connection_aad(u.id, cid, "openai")
    ct, nonce, kid = security.encrypt_secret("sk-live-abcdef", aad)
    with db.tx() as conn:
        conn.execute(db.ai_connections.insert().values(id=cid, user_id=u.id, provider="openai", model="m",
                                                       key_ciphertext=ct, key_nonce=nonce, key_kid=kid,
                                                       key_last4="cdef", options={}, is_default=True))
    import os

    monkeypatch.setenv("RV_MASTER_KEYS", security.genkey("new") + "," + os.environ["RV_MASTER_KEYS"])
    settings.reload()
    assert security.rotate_all() == 1
    with db.tx(immediate=False) as conn:
        r = conn.execute(db.ai_connections.select()).first()
    assert r.key_kid == "new"
    assert security.decrypt_secret(r.key_ciphertext, r.key_nonce, r.key_kid, aad) == "sk-live-abcdef"


def test_redact():
    text = "call failed for key sk-abcdefghijklmnopqrstuv and AIzaSyA1234567890abcdefghijk; Bearer abc.def.ghi.jkl"
    out = security.redact(text, ["my-own-secret"])
    assert "sk-abc" not in out and "AIza" not in out and "abc.def" not in out
    assert security.redact("token my-own-secret here", ["my-own-secret"]) == "token [redacted] here"


def test_tokens_and_codes():
    tok, h = security.new_token("rvd")
    assert tok.startswith("rvd_") and len(tok) > 40 and h == security.sha256_hex(tok)
    code = security.new_code()
    assert len(code) == 9 and code[4] == "-"
    assert security.code_hash(code.lower()) == security.code_hash(code.replace("-", " "))
    assert security.norm_code("o1l-i") == "0111"


def _req(peer: str, xff: str | None = None):
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_forged_x_forwarded_for_from_untrusted_peer_is_ignored(rv_env):
    assert security.client_ip(_req("203.0.113.9", "1.2.3.4")) == "203.0.113.9"
    assert security.client_ip(_req("127.0.0.1", "198.51.100.7")) == "198.51.100.7"
    # a client cannot prepend a fake hop: we take the right-most untrusted address
    assert security.client_ip(_req("172.18.0.2", "9.9.9.9, 198.51.100.7")) == "198.51.100.7"
    assert security.client_ip(_req("127.0.0.1")) == "127.0.0.1"


def test_session_idle_and_absolute_expiry(client, member):
    r = client.post("/api/auth/login", json={"email": member.email, "password": member.password})
    assert r.status_code == 200
    assert client.get("/api/me").status_code == 200
    with db.tx() as conn:
        conn.execute(db.sessions.update().values(last_seen_at=db.utcnow() - timedelta(days=15)))
    assert client.get("/api/me").status_code == 401  # idle 14 days
    r = client.post("/api/auth/login", json={"email": member.email, "password": member.password})
    with db.tx() as conn:
        conn.execute(db.sessions.update().values(expires_at=db.utcnow() - timedelta(seconds=1)))
    assert client.get("/api/me").status_code == 401  # 30-day absolute
    with db.tx(immediate=False) as conn:
        assert conn.execute(db.sessions.select()).first() is None


def test_session_cookie_flags(client, member):
    r = client.post("/api/auth/login", json={"email": member.email, "password": member.password})
    sc = r.headers["set-cookie"].lower()
    assert "rv_session=" in sc and "httponly" in sc and "samesite=lax" in sc and "path=/" in sc
    assert "secure" not in sc  # PUBLIC_URL is http://localhost in tests


def test_csrf_required_and_origin_checked(member_client):
    tok = member_client.headers.pop("X-CSRF-Token")
    r = member_client.post("/api/auth/password", json={"current": "x", "new": "y"})
    assert r.status_code == 403
    member_client.headers["X-CSRF-Token"] = "wrong"
    assert member_client.post("/api/auth/password", json={"current": "x", "new": "y"}).status_code == 403
    member_client.headers["X-CSRF-Token"] = tok
    r = member_client.post("/api/auth/password", json={"current": "x", "new": "y"},
                           headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = member_client.post("/api/auth/password", json={"current": "x", "new": "y"},
                           headers={"Origin": "http://localhost"})
    assert r.status_code == 400  # passed CSRF + origin, failed on the wrong current password


def test_device_token_kinds(client, member, make_device):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from reelvault.server.app import _install_errors

    mini = FastAPI()
    _install_errors(mini)

    @mini.post("/api/ext/v2/ping")
    def ping(dev=Depends(security.extension_device)):
        return {"device": dev.id}

    @mini.post("/api/worker/ping")
    def wping(dev=Depends(security.desktop_device)):
        return {"device": dev.id}

    ext_id, ext_h = make_device(member, "extension")
    desk_id, desk_h = make_device(member, "desktop")
    c = TestClient(mini)
    assert c.post("/api/ext/v2/ping", headers=ext_h).json() == {"device": ext_id}
    assert c.post("/api/ext/v2/ping", headers=desk_h).status_code == 401
    assert c.post("/api/worker/ping", headers=desk_h).json() == {"device": desk_id}
    assert c.post("/api/worker/ping", headers=ext_h).status_code == 401
    assert c.post("/api/ext/v2/ping").status_code == 401
    with db.tx() as conn:
        conn.execute(db.devices.update().values(revoked_at=db.utcnow()))
    assert c.post("/api/ext/v2/ping", headers=ext_h).status_code == 401


def test_pinned_transport_refuses_private_addresses():
    calls = []

    def rebinding(host, port):
        calls.append(host)
        return ["93.184.216.34"] if len(calls) == 1 else ["127.0.0.1"]

    assert net.resolve_checked("example.com", 443, resolver=rebinding) == "93.184.216.34"
    with pytest.raises(net.UnsafeAddress):
        net.resolve_checked("example.com", 443, resolver=rebinding)  # second lookup rebinds to loopback
    for bad in ("127.0.0.1", "169.254.169.254", "10.1.2.3", "100.64.0.1", "::1", "fd00::1", "0.0.0.0"):
        assert not net.is_public_ip(bad), bad
    assert net.is_public_ip("8.8.8.8")
    client = httpx.Client(transport=net.PinnedTransport(resolver=lambda h, p: ["10.0.0.5"]))
    with pytest.raises(net.UnsafeAddress):
        client.get("http://intranet.example/")


def test_safe_fetch_refuses_private_and_bad_schemes(rv_env):
    for url in ("http://127.0.0.1:9101/", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd",
                "ftp://example.com/", "http://user:pw@example.com/"):
        with pytest.raises(security.FetchError):
            security.safe_fetch(url, max_bytes=1000)
    with pytest.raises(security.FetchError):
        security.safe_fetch("http://example.com/", max_bytes=1000, allow_http=False)


def test_safe_fetch_rechecks_redirects(rv_env):
    def handler(request: httpx.Request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        return httpx.Response(200, text="ok")

    class Fake(httpx.MockTransport):
        def handle_request(self, request):
            net.resolve_checked(request.url.host, 80, resolver=lambda h, p: ["93.184.216.34"])
            return super().handle_request(request)

    with pytest.raises(security.FetchError):
        security.safe_fetch("http://site.example/start", max_bytes=1000, transport=Fake(handler))
    r = security.safe_fetch("http://site.example/ok", max_bytes=1000, transport=Fake(handler))
    assert r.status == 200 and r.text == "ok"
    with pytest.raises(security.FetchError):
        security.safe_fetch("http://site.example/ok", max_bytes=1, transport=Fake(handler))


def test_member_path_refuses_escapes(rv_env, make_user):
    u = make_user()
    base = storage.member_dir(u.id)
    assert storage.member_path(u.id, "reels/r1/cover.jpg") == base / "reels" / "r1" / "cover.jpg"
    for bad in ("../x", "/etc/passwd", "a/../../b", "", "a\\b", "~/x"):
        with pytest.raises(storage.UnsafePath):
            storage.member_path(u.id, bad)
    (base / "link").symlink_to("/etc")
    with pytest.raises(storage.UnsafePath):
        storage.member_path(u.id, "link/passwd")
    with pytest.raises(storage.UnsafePath):
        storage.member_dir("../usr_x")
    assert storage.files_url(u.id, "reels/r 1/c.jpg") == f"/files/{u.id}/reels/r%201/c.jpg"


def test_local_fs_check(tmp_path, monkeypatch):
    db.check_local_fs(tmp_path)  # ext4/tmpfs here: fine
    monkeypatch.setattr(db, "fs_type", lambda p: "nfs4")
    with pytest.raises(SystemExit) as e:
        db.check_local_fs(tmp_path)
    assert "network drive" in str(e.value)
    monkeypatch.setattr(db, "fs_type", lambda p: "fuse.sshfs")
    with pytest.raises(SystemExit):
        db.check_local_fs(tmp_path)
