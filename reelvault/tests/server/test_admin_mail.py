"""WP1 second pass: email (11.1, 4.2 reset-request), admin approve/reject, support access (11.5), community floor."""
import pytest
from sqlalchemy import func, select

from reelvault.server import api_admin, db, mailer, settings

PW = "plum-river-velvet-42"


@pytest.fixture
def outbox(rv_env, monkeypatch):
    sent = []
    monkeypatch.setenv("SMTP_URL", "smtp://mailer:s3cret-pass@smtp.example.com:587")
    settings.reload()
    monkeypatch.setattr(mailer, "BACKGROUND", False)
    monkeypatch.setattr(mailer, "_deliver", lambda msg: sent.append(msg))
    return sent


def signup(client, code, email="new@example.com"):
    return client.post("/api/auth/signup", json={"name": "New Person", "email": email, "password": PW,
                                                  "skool_url": "https://www.skool.com/@new-person", "code": code})


def test_no_email_without_settings(rv_env, monkeypatch):
    calls = []
    monkeypatch.setattr(mailer, "_deliver", lambda m: calls.append(m))
    assert mailer.configured() is False
    assert mailer.send("a@example.com", "x", "y") is False
    assert calls == []


def test_pending_signup_emails_every_admin_once(client, outbox, admin, make_user, community_code):
    make_user("admin2@example.com", role="admin")
    make_user("off-admin@example.com", role="admin", status="disabled")
    assert signup(client, community_code).json()["status"] == "pending"
    to = sorted(m["To"] for m in outbox)
    assert to == ["admin2@example.com", "admin@example.com"]
    body = outbox[0].get_content()
    assert "new@example.com" in body and "https://www.skool.com/@new-person" in body and "#admin/members" in body
    assert outbox[0]["Subject"] == "New member waiting: New Person"
    assert outbox[0]["From"] == "ReelVault <no-reply@localhost>"


def test_pending_notice_respects_the_setting(client, outbox, admin, community_code):
    db.set_setting("notify_admin_email", False)
    signup(client, community_code)
    assert outbox == []


def test_approve_sends_youre_in(client, outbox, admin_client, community_code):
    signup(client, community_code)
    outbox.clear()
    uid = admin_client.get("/api/admin/users?status=pending").json()["users"][0]["id"]
    assert admin_client.post(f"/api/admin/users/{uid}/approve", json={}).status_code == 200
    assert [m["To"] for m in outbox] == ["new@example.com"]
    assert "You're in" in outbox[0]["Subject"] and "http://localhost/" in outbox[0].get_content()


def test_reject_deletes_only_pending_signups(client, admin_client, community_code, member):
    signup(client, community_code)
    uid = admin_client.get("/api/admin/users?status=pending").json()["users"][0]["id"]
    assert admin_client.post(f"/api/admin/users/{member.id}/reject", json={}).status_code == 400
    assert admin_client.post(f"/api/admin/users/{uid}/reject", json={}).json() == {"ok": True}
    with db.tx(immediate=False) as conn:
        assert conn.execute(select(db.users).where(db.users.c.id == uid)).first() is None
    assert admin_client.post(f"/api/admin/users/{uid}/reject", json={}).status_code == 404
    assert signup(client, community_code).status_code == 201  # the same email can try again later


def test_reset_request_emails_a_working_link(client, outbox, member):
    r = client.post("/api/auth/reset-request", json={"email": member.email.upper()})
    assert r.json() == {"ok": True}
    assert len(outbox) == 1 and outbox[0]["To"] == member.email
    text = outbox[0].get_content()
    link = next(w for w in text.split() if "#reset/" in w)
    token = link.split("#reset/")[1]
    assert client.post("/api/auth/reset", json={"token": token, "new": "brand-new-pass-99"}).json() == {"ok": True}
    assert client.post("/api/auth/login", json={"email": member.email, "password": "brand-new-pass-99"}).status_code == 200
    with db.tx(immediate=False) as conn:
        row = conn.execute(select(db.reset_links).where(db.reset_links.c.user_id == member.id)).first()
    assert row.created_by == "self" and row.used_at is not None


def test_reset_request_never_tells_who_exists_and_is_limited(client, outbox, member):
    assert client.post("/api/auth/reset-request", json={"email": "ghost@example.com"}).json() == {"ok": True}
    assert outbox == []
    for _ in range(5):
        assert client.post("/api/auth/reset-request", json={"email": member.email}).json() == {"ok": True}
    assert len(outbox) == 3  # 3 per address per hour, same answer every time


def test_mail_errors_are_kept_without_secrets(rv_env, monkeypatch):
    monkeypatch.setenv("SMTP_URL", "smtp://mailer:s3cret-pass@smtp.example.com:587")
    settings.reload()
    monkeypatch.setattr(mailer, "BACKGROUND", False)

    def boom(msg):
        raise RuntimeError("auth failed for mailer:s3cret-pass")

    monkeypatch.setattr(mailer, "_deliver", boom)
    assert mailer.send("a@example.com", "Hi", "text") is False
    err = db.get_setting("internal.mail_last_error")
    assert "s3cret-pass" not in err["error"] and "[redacted]" in err["error"]


def test_smtp_url_from_query_and_mail_from(rv_env, monkeypatch):
    monkeypatch.setenv("SMTP_URL", "smtps://u:p@smtp.example.com:465?from=Team%20RV%20%3Cteam%40example.com%3E")
    settings.reload()
    assert mailer.from_address() == "Team RV <team@example.com>"
    monkeypatch.setenv("SMTP_URL", "")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("MAIL_FROM", "RV <rv@example.com>")
    settings.reload()
    assert mailer.configured() and mailer.from_address() == "RV <rv@example.com>"


# ============================================================================================== support access

def test_support_access_grant_view_audit_and_revoke(member_client, admin_client, member):
    api_admin.register_support_mirror("reels", lambda uid, q: {"reels": [], "for": uid})
    assert member_client.get("/api/me/support-access").json()["active"] is False
    assert admin_client.get(f"/api/admin/support/{member.id}/reels").status_code == 404
    assert member_client.post("/api/me/support-access", json={"days": 9}).status_code == 400
    st = member_client.post("/api/me/support-access", json={"days": 7}).json()
    assert st["active"] is True and st["expires_at"].endswith("Z")
    users = admin_client.get("/api/admin/users").json()["users"]
    assert next(u for u in users if u["id"] == member.id)["support_access"] is True
    assert admin_client.get(f"/api/admin/support/{member.id}/reels").json() == {"reels": [], "for": member.id}
    st = member_client.get("/api/me/support-access").json()
    assert st["last_viewed_at"] and len(st["admin_views"]) == 1          # "The admin looked at your account on ..."
    with db.tx(immediate=False) as conn:
        s = conn.execute(select(db.users.c.settings).where(db.users.c.id == member.id)).scalar()
    assert s["support_access_until"] == st["expires_at"]
    assert member_client.delete("/api/me/support-access").json()["active"] is False
    assert admin_client.get(f"/api/admin/support/{member.id}/reels").status_code == 404


def test_community_formats_enforce_the_privacy_floor(admin_client):
    assert admin_client.get("/api/admin/community-formats").status_code == 404  # off by default
    db.set_setting("community_stats", True)
    api_admin.register_community_formats(lambda: {"month": "2026-11", "formats": [
        {"vf": "VF01", "label": "Talking head", "members_using": 7, "lift_median": 2.1, "handles": ["leak"]},
        {"vf": "VF02", "label": "Green screen", "members_using": 4, "lift_median": 3.0}]})
    try:
        out = admin_client.get("/api/admin/community-formats").json()
    finally:
        api_admin._COMMUNITY.clear()  # noqa: SLF001
    assert out == {"month": "2026-11", "formats": [
        {"vf": "VF01", "label": "Talking head", "members_using": 7, "lift_median": 2.1}]}


def test_hooks_are_limited_and_need_the_secret(client, monkeypatch):
    monkeypatch.setenv("SKOOL_HOOK_SECRET", "hook-secret-xyz")
    settings.reload()
    assert client.post("/api/hooks/skool-allowlist", json={"email": "z@example.com"}).status_code == 403
    r = client.post("/api/hooks/skool-allowlist", json={"email": "z@example.com"},
                    headers={"X-Hook-Secret": "hook-secret-xyz"})
    assert r.json() == {"ok": True}
    with db.tx(immediate=False) as conn:
        assert conn.execute(select(func.count()).select_from(db.allowlist)).scalar() == 1


# ============================================================================================== real SMTP round trip

def _fake_smtp_server(port: int, got: list):
    import socket
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        f = conn.makefile("rwb")

        def say(line):
            f.write(line.encode() + b"\r\n")
            f.flush()

        say("220 fake ESMTP")
        data_mode, buf = False, []
        for raw in f:
            line = raw.decode().rstrip("\r\n")
            if data_mode:
                if line == ".":
                    got.append("\n".join(buf))
                    data_mode = False
                    say("250 queued")
                else:
                    buf.append(line)
                continue
            cmd = line.split(" ")[0].upper()
            if cmd == "EHLO":
                f.write(b"250-fake\r\n250 AUTH PLAIN LOGIN\r\n")
                f.flush()
            elif cmd == "AUTH":
                got.append("AUTH " + line.split(" ", 2)[-1])
                say("235 ok")
            elif cmd in ("MAIL", "RCPT"):
                say("250 ok")
            elif cmd == "DATA":
                data_mode = True
                say("354 go")
            elif cmd == "QUIT":
                say("221 bye")
                break
            else:
                say("250 ok")
        conn.close()
        srv.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    return th


def test_smtp_delivery_end_to_end(rv_env, monkeypatch):
    import base64

    got: list = []
    th = _fake_smtp_server(9122, got)
    monkeypatch.setenv("SMTP_URL", "smtp://mail%40example.com:pa%24%24word@127.0.0.1:9122")
    settings.reload()
    monkeypatch.setattr(mailer, "BACKGROUND", False)
    assert mailer.send("to@example.com", "Hello there", "Line one.\nLine two.") is True
    th.join(timeout=5)
    auth = base64.b64decode(got[0].split(" ")[-1]).split(b"\0")
    assert auth[1:] == [b"mail@example.com", b"pa$$word"]  # URL-decoded user and password
    msg = got[1]
    assert "Subject: Hello there" in msg and "To: to@example.com" in msg and "Line two." in msg


def test_settings_have_typo_guards(admin_client):
    r = admin_client.patch("/api/admin/settings", json={"max_pulls_per_day": 5000})
    assert r.status_code == 400 and "at most 100" in r.json()["error"]
    assert admin_client.patch("/api/admin/settings", json={"storage_cap_mb": 8000}).json()["storage_cap_mb"] == 8000
