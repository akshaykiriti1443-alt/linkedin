"""Fix round 2 (security and deploy findings): password resets sign out Claude and devices, login lockout per client,
allowlist needs a proven email, DCR per-IP limit, CIMD fetch guard, refresh-token grace, safe restore and backups."""
from __future__ import annotations

import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from reelvault.server import db, oauth, security, storage

PW = "plum-river-velvet-42"


@pytest.fixture
def ip(monkeypatch):
    """Pretend each request comes from the address in the X-Test-IP header."""
    real = security.client_ip

    def fake(request):
        return request.headers.get("x-test-ip") or real(request)

    monkeypatch.setattr(security, "client_ip", fake)
    return lambda a: {"X-Test-IP": a}


def _token_family(user_id: str) -> dict:
    cid = db.new_id("dcr")
    with db.tx() as conn:
        conn.execute(db.oauth_clients.insert().values(client_id=cid, kind="dcr", metadata={"client_name": "t"},
                                                      redirect_uris=["http://localhost:7777/callback"],
                                                      created_at=db.utcnow()))
        out = oauth._issue(conn, client_id=cid, user_id=user_id, family_id="fam_" + cid,  # noqa: SLF001
                           resource=oauth.resource_url())
    out["client_id"] = cid
    return out


# ============================================================================================== password reset / change

def test_reset_signs_out_claude_and_devices(client, member, admin_client, make_device):
    tok = _token_family(member.id)
    did, h = make_device(member)
    _d2, _h2 = make_device(member, kind="desktop")
    assert oauth.verify_access(tok["access_token"]) is not None
    assert client.post("/api/ext/v2/hello", json={"version": "2.0.0"}, headers=h).status_code == 200
    link = admin_client.post(f"/api/admin/users/{member.id}/reset-link", json={}).json()
    token = link["url"].split("#reset/")[1]
    r = client.post("/api/auth/reset", json={"token": token, "new": "a-brand-new-pass-77"}).json()
    assert r["ok"] and r["message"] == "We signed out Claude and your devices. Connect them again."
    assert oauth.verify_access(tok["access_token"]) is None
    with pytest.raises(oauth.OAuthError):
        oauth.grant_refresh({"refresh_token": tok["refresh_token"], "client_id": tok["client_id"]})
    assert client.post("/api/ext/v2/hello", json={"version": "2.0.0"}, headers=h).status_code == 401
    with db.tx(immediate=False) as conn:
        live = conn.execute(select(db.devices.c.id).where(db.devices.c.user_id == member.id,
                                                          db.devices.c.revoked_at.is_(None))).all()
        aud = conn.execute(select(db.audit_log.c.detail).where(db.audit_log.c.action == "password_reset")).first()
    assert live == [] and aud.detail["signed_out_apps"] == {"devices": 2, "tokens": 2}


def test_password_change_signs_out_apps_unless_unticked(member_client, member):
    tok = _token_family(member.id)
    csrf = member_client.get("/api/me").json()["csrf_token"]
    new = "a-brand-new-pass-88"
    r = member_client.post("/api/auth/password", json={"current": member.password, "new": new,
                                                      "sign_out_everywhere": False}, headers={"X-CSRF-Token": csrf})
    assert r.json()["ok"] and oauth.verify_access(tok["access_token"]) is not None
    r = member_client.post("/api/auth/password", json={"current": new, "new": "a-third-new-pass-99"},
                           headers={"X-CSRF-Token": csrf})
    assert r.json()["signed_out_apps"] is True and oauth.verify_access(tok["access_token"]) is None


# ============================================================================================== login lockout

def test_nobody_can_lock_a_member_out_from_another_address(client, member, ip):
    bad = {"email": member.email, "password": "wrong-password-1"}
    codes = [client.post("/api/auth/login", json=bad, headers=ip(f"203.0.113.{i}")).status_code for i in range(1, 9)]
    assert codes == [401] * 8                      # 8 addresses, each under its own 5
    ok = client.post("/api/auth/login", json={"email": member.email, "password": member.password},
                     headers=ip("203.0.113.50"))
    assert ok.status_code == 200
    # the strict limit still holds per (email, address)
    codes = [client.post("/api/auth/login", json=bad, headers=ip("198.51.100.9")).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]
    assert client.post("/api/auth/login", json={"email": member.email, "password": member.password},
                       headers=ip("198.51.100.9")).status_code == 429


def test_email_wide_limit_spares_known_addresses(client, member, ip, monkeypatch):
    from reelvault.server import api_auth

    monkeypatch.setattr(api_auth, "LOGIN_PER_EMAIL", 6)
    good = {"email": member.email, "password": member.password}
    assert client.post("/api/auth/login", json=good, headers=ip("192.0.2.10")).status_code == 200   # known now
    bad = {"email": member.email, "password": "wrong-password-1"}
    for i in range(7):
        client.post("/api/auth/login", json=bad, headers=ip(f"203.0.113.{100 + i}"))
    assert client.post("/api/auth/login", json=good, headers=ip("203.0.113.200")).status_code == 429
    assert client.post("/api/auth/login", json=good, headers=ip("192.0.2.10")).status_code == 200


# ============================================================================================== allowlist

def test_allowlisted_signup_must_prove_the_email(client, admin_client, community_code, monkeypatch):
    from reelvault.server import mailer

    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda to, subject, text: sent.append((to, subject, text)) or True)
    admin_client.post("/api/admin/allowlist", json={"emails": ["real@skool-member.example"]})
    r = client.post("/api/auth/signup", json={"name": "Real", "email": "real@skool-member.example", "password": PW,
                                              "code": community_code})
    assert r.status_code == 201 and r.json()["status"] == "pending" and r.json()["check_email"] is True
    assert "set-cookie" not in r.headers
    to, _subj, text = next(x for x in sent if x[0] == "real@skool-member.example")
    token = text.split("#verify/")[1].split()[0]
    # the email link is not a password reset link
    assert client.post("/api/auth/reset", json={"token": token, "new": "a-brand-new-pass-77"}).status_code == 400
    assert client.post("/api/auth/verify-email", json={"token": token}).json() == {"ok": True, "status": "active"}
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 400
    assert client.post("/api/auth/login", json={"email": "real@skool-member.example", "password": PW}).status_code == 200


def test_squatted_allowlist_signup_can_be_rejected(client, admin_client, community_code):
    admin_client.post("/api/admin/allowlist", json={"emails": ["victim@skool-member.example"]})
    body = {"name": "X", "email": "victim@skool-member.example", "password": PW, "code": community_code}
    assert client.post("/api/auth/signup", json=body).json()["status"] == "pending"
    u = admin_client.get("/api/admin/users?status=pending").json()["users"][0]
    assert u["on_allowlist"] is True
    assert admin_client.post(f"/api/admin/users/{u['id']}/reject", json={}).json() == {"ok": True}
    assert client.post("/api/auth/signup", json=body).status_code == 201


# ============================================================================================== OAuth

def test_dcr_per_ip_limit_ignores_the_name(client, ip):
    body = {"redirect_uris": ["http://localhost:7777/callback"]}
    codes = [client.post("/oauth/register", json=dict(body, client_name=f"x{i}"), headers=ip("203.0.113.5")).status_code
             for i in range(oauth.REGISTER_PER_IP_HOUR + 2)]
    assert codes.count(201) == oauth.REGISTER_PER_IP_HOUR and codes[-1] == 429
    r = client.post("/oauth/register", json=dict(body, client_name="Claude Code"), headers=ip("203.0.113.9"))
    assert r.status_code == 201


def test_never_used_dcr_clients_go_after_a_day():
    cid = db.new_id("dcr")
    with db.tx() as conn:
        conn.execute(db.oauth_clients.insert().values(client_id=cid, kind="dcr", metadata={}, redirect_uris=[],
                                                      created_at=db.utcnow() - timedelta(hours=25)))
    oauth.cleanup()
    with db.tx(immediate=False) as conn:
        assert conn.execute(select(db.oauth_clients).where(db.oauth_clients.c.client_id == cid)).first() is None


AUTH_Q = ("/oauth/authorize?response_type=code&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
          "&code_challenge=" + "A" * 43 + "&code_challenge_method=S256&state=s&client_id=")


def test_slow_cimd_hosts_cannot_hold_the_server(client, monkeypatch):
    oauth.reset_cimd_guard()
    calls = []

    def slow(url):
        calls.append(url)
        time.sleep(2.0)
        raise oauth.OAuthError("invalid_client", "slow", redirectable=False, member=oauth.MEMBER_CIMD)

    monkeypatch.setattr(oauth, "_fetch_cimd", slow)
    monkeypatch.setattr(oauth, "CIMD_WAIT", 0.3)
    t = time.monotonic()
    r = client.get(AUTH_Q + "https%3A%2F%2Ftarpit.example%2Fa%2Fmeta")
    assert r.status_code == 400 and time.monotonic() - t < 1.5          # the request does not wait for the host
    t = time.monotonic()
    r = client.get(AUTH_Q + "https%3A%2F%2Ftarpit.example%2Fa%2Fmeta")
    assert r.status_code == 400 and time.monotonic() - t < 0.5 and len(calls) == 1   # negative cache
    # all fetch slots busy: a new URL is refused at once, without a fetch
    import threading

    monkeypatch.setattr(oauth, "_CIMD_SLOTS", threading.BoundedSemaphore(1))
    client.get(AUTH_Q + "https%3A%2F%2Ftarpit.example%2Fb%2Fmeta")
    r = client.get(AUTH_Q + "https%3A%2F%2Ftarpit.example%2Fc%2Fmeta")
    assert r.status_code == 503 and "Too many apps" in r.text and len(calls) == 2
    oauth.reset_cimd_guard()


def test_cimd_fetches_are_rate_limited_per_client(client, monkeypatch):
    oauth.reset_cimd_guard()
    calls = []

    def fail(url):
        calls.append(url)
        raise oauth.OAuthError("invalid_client", "nope", redirectable=False, member=oauth.MEMBER_CIMD)

    monkeypatch.setattr(oauth, "_fetch_cimd", fail)
    monkeypatch.setattr(oauth, "CIMD_FETCH_PER_KEY_HOUR", 3)
    codes = [client.get(AUTH_Q + f"https%3A%2F%2Fx{i}.example%2Fmeta").status_code for i in range(5)]
    assert codes == [400, 400, 400, 503, 503] and len(calls) == 3
    oauth.reset_cimd_guard()


def test_refresh_retry_within_grace_keeps_the_first_answer(member):
    t = _token_family(member.id)
    form = {"refresh_token": t["refresh_token"], "client_id": t["client_id"]}
    first = oauth.grant_refresh(dict(form))
    again = oauth.grant_refresh(dict(form))              # a retry after a lost answer
    assert again["access_token"] != first["access_token"]
    assert oauth.verify_access(first["access_token"]) and oauth.verify_access(again["access_token"])
    nxt = oauth.grant_refresh({"refresh_token": first["refresh_token"], "client_id": t["client_id"]})
    assert oauth.verify_access(nxt["access_token"])
    # reuse of a rotated token after its successor was used: the family is revoked
    with pytest.raises(oauth.OAuthError):
        oauth.grant_refresh(dict(form))
    assert oauth.verify_access(nxt["access_token"]) is None


def test_refresh_reuse_after_the_grace_window_revokes(member):
    t = _token_family(member.id)
    form = {"refresh_token": t["refresh_token"], "client_id": t["client_id"]}
    first = oauth.grant_refresh(dict(form))
    with db.tx() as conn:
        conn.execute(db.oauth_tokens.update().where(
            db.oauth_tokens.c.token_hash == security.sha256_hex(t["refresh_token"]))
            .values(revoked_at=db.utcnow() - timedelta(minutes=5)))
    with pytest.raises(oauth.OAuthError):
        oauth.grant_refresh(dict(form))
    assert oauth.verify_access(first["access_token"]) is None


def test_consent_page_says_claude_can_start_pulls():
    import inspect

    src = inspect.getsource(oauth._consent_html)  # noqa: SLF001
    assert "ask your Chrome extension to read their reels" in src and "daily" in src


# ============================================================================================== backups and restore

def test_restore_refuses_while_the_database_is_open(rv_env, monkeypatch):
    storage.backup()
    b = sorted(storage.backups_dir().glob("db-*.sqlite"))[-1]
    monkeypatch.setattr(storage, "_pids_with_open", lambda paths: [4242])
    with pytest.raises(SystemExit) as e:
        storage.restore_db(b)
    assert "RV_RESTORE_FROM" in str(e.value)


def test_restore_carries_todays_daily_marks(rv_env, monkeypatch):
    monkeypatch.setattr(storage, "_web_answers", lambda: False)
    db.init()
    old = storage.backup()                     # yesterday's file stand-in: no daily marks in it
    b = storage.backups_dir() / old["db"]
    older = storage.backups_dir() / "db-2000-01-01.sqlite"
    b.rename(older)
    from reelvault.server import jobs

    jobs.schedule_daily()                      # today's daily jobs ran on the live database
    db._engine.dispose()  # noqa: SLF001
    storage.restore_db(older)
    db.init()
    today = db.utcnow().strftime("%Y-%m-%d")
    with db.tx(immediate=False) as conn:
        marks = conn.execute(select(db.usage_counters.c.key).where(
            db.usage_counters.c.window == today, db.usage_counters.c.key.like("daily:%"))).scalars().all()
    assert "daily:backup" in marks
    assert jobs.schedule_daily() == []         # nothing runs twice, so db-<today> is not overwritten


def test_manual_backup_refreshes_this_weeks_files(rv_env, make_user):
    u = make_user()
    d = storage.member_dir(u.id)
    (d / "a.jpg").write_bytes(b"a")
    first = storage.backup(force_files=True)
    (d / "b-new.jpg").write_bytes(b"b")
    assert storage._main(["backup"]) == 0  # noqa: SLF001 - the CLI (backup.sh) forces the files tar
    import tarfile

    tar = storage.backups_dir() / first["files"]
    if tar.name.endswith(".gz"):
        with tarfile.open(tar) as tf:
            names = tf.getnames()
    else:
        import subprocess

        names = subprocess.run(["tar", "--zstd", "-tf", str(tar)], capture_output=True, text=True).stdout.split()
    assert any(n.endswith("b-new.jpg") for n in names)


def test_empty_users_folder_makes_no_weekly_tar(rv_env):
    out = storage.backup()
    assert "files" not in out and not list(storage.backups_dir().glob("users-*"))


def test_restore_on_start_runs_once(rv_env, monkeypatch):
    monkeypatch.setattr(storage, "_web_answers", lambda: False)
    out = storage.backup()
    db._engine.dispose()  # noqa: SLF001
    monkeypatch.setenv("RV_RESTORE_FROM", out["db"])
    assert storage.restore_on_start() == out["db"]
    assert storage.restore_on_start() is None            # marker: a restart does not restore again
    monkeypatch.setenv("RV_RESTORE_FROM", "../../etc/passwd")
    assert storage.restore_on_start() is None


def test_drop_root_is_a_no_op_outside_the_container_layout(rv_env):
    storage.drop_root("reelvault.server.storage", ["list"])   # returns: data dir is not owned by uid 10001 here


def test_admin_stats_reports_a_failed_backup(admin_client):
    jid = db.new_id("job")
    with db.tx() as conn:
        conn.execute(db.jobs.insert().values(id=jid, user_id=None, kind="backup", state="failed", payload={},
                                             executor="server", job_class="light", priority=90,
                                             created_at=db.utcnow(), finished_at=db.utcnow(),
                                             error_message="attempt to write a readonly database"))
    d = admin_client.get("/api/admin/stats").json()
    assert d["backup_failed"] and "readonly" in d["backup_failed"]["message"]
