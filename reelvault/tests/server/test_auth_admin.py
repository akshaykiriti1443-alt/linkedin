"""Auth and admin API (DESIGN-SAAS.md 4.2, 4.3, 11.1; 12.2 test_auth_admin.py)."""
from datetime import timedelta

from sqlalchemy import select

from reelvault.server import api_admin, db, jobs, ratelimit

PW = "plum-river-velvet-42"


def signup(client, code, email="new@example.com", **kw):
    body = {"name": "New Person", "email": email, "password": PW, "skool_url": "https://www.skool.com/@new-person",
            "code": code}
    body.update(kw)
    return client.post("/api/auth/signup", json=body)


def test_signup_with_community_code_is_pending_by_default(client, community_code, admin_client):
    r = signup(client, community_code)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending" and "approve" in r.json()["message"]
    assert "set-cookie" not in r.headers
    r = client.post("/api/auth/login", json={"email": "new@example.com", "password": PW})
    assert r.status_code == 403 and r.json()["error"] == "Your account is waiting for approval."
    # admin sees the badge and approves
    me = admin_client.get("/api/me").json()
    assert me["admin"]["pending"] == 1
    users = admin_client.get("/api/admin/users?status=pending").json()["users"]
    assert [u["email"] for u in users] == ["new@example.com"]
    assert admin_client.post(f"/api/admin/users/{users[0]['id']}/approve", json={}).json()["user"]["status"] == "active"
    r = client.post("/api/auth/login", json={"email": "New@Example.com ", "password": PW})
    assert r.status_code == 200 and r.json()["user"]["status"] == "active"
    assert client.get("/api/me").json()["csrf_token"]


def test_signup_validation_and_wrong_code(client, community_code):
    assert signup(client, community_code, password="short").status_code == 400
    assert signup(client, community_code, email="nope").status_code == 400
    assert signup(client, community_code, skool_url="https://evil.example/@x").status_code == 400
    r = signup(client, "WRONG-CODE")
    assert r.status_code == 403
    assert r.json()["error"] == "That access code is not right. Ask in the community for the current one."
    assert ratelimit.count("global", "signup_code_fail", "hour") == 1


def test_wrong_codes_are_limited_per_ip(client, community_code):
    for _ in range(10):
        assert signup(client, "WRONG-CODE").status_code == 403
    r = signup(client, community_code)
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_single_invite_and_allowlist_go_straight_to_active(client, admin_client, community_code):
    inv = admin_client.post("/api/admin/invites", json={"label": "For Sam", "uses": 1, "expires_days": 14}).json()
    assert inv["url"].endswith(inv["code"])
    assert client.get(f"/api/invite/{inv['code']}").json() == {"valid": True, "label": "For Sam"}
    r = signup(client, inv["code"], email="sam@example.com")
    assert r.status_code == 201 and r.json()["status"] == "active" and r.json()["user"]["csrf_token"]
    assert "rv_session=" in r.headers["set-cookie"]
    # single use: used up
    assert signup(client, inv["code"], email="sam2@example.com").status_code == 403
    assert client.get(f"/api/invite/{inv['code']}").json() == {"valid": False}

    admin_client.post("/api/admin/allowlist", json={"emails": ["Ally@Example.com"]})
    r = signup(client, community_code, email="ally@example.com")
    assert r.json()["status"] == "active"
    # duplicate email
    assert signup(client, community_code, email="ally@example.com").status_code == 409


def test_allowlisting_approves_a_pending_member(client, admin_client, community_code):
    signup(client, community_code, email="late@example.com")
    admin_client.post("/api/admin/allowlist", json={"emails": ["late@example.com"]})
    with db.tx(immediate=False) as conn:
        assert conn.execute(select(db.users.c.status).where(db.users.c.email == "late@example.com")).scalar() == "active"


def test_login_messages_and_limits(client, member):
    r = client.post("/api/auth/login", json={"email": member.email, "password": "wrong-password"})
    assert r.status_code == 401 and r.json()["error"] == "Email or password is wrong."
    r = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"})
    assert r.status_code == 401 and r.json()["error"] == "Email or password is wrong."
    for _ in range(4):
        client.post("/api/auth/login", json={"email": member.email, "password": "wrong-password"})
    r = client.post("/api/auth/login", json={"email": member.email, "password": member.password})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_concurrent_wrong_logins_are_throttled(app, member):
    """Fix round 1: the attempt is counted atomically before the password check, so a burst of parallel guesses
    gets 429 after 5, not 30 password checks."""
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from reelvault.server.testing import BASE_URL

    def guess(i):
        c = TestClient(app, base_url=BASE_URL)  # no lifespan: one MCP session manager per app only
        return c.post("/api/auth/login", json={"email": member.email, "password": f"wrong-{i}"}).status_code

    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(guess, range(30)))
    assert codes.count(401) <= 5 and codes.count(429) >= 25, codes


def test_right_logins_do_not_use_up_the_limit(client, member):
    for _ in range(8):
        assert client.post("/api/auth/login", json={"email": member.email, "password": member.password}).status_code == 200
    assert ratelimit.count("login_email", member.email, "quarter") == 0
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": member.email, "password": "nope-nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": member.email, "password": member.password}).status_code == 429


def test_logout_password_change_and_reset_link(client, member, admin_client):
    r = client.post("/api/auth/login", json={"email": member.email, "password": member.password})
    csrf = r.json()["user"]["csrf_token"]
    r = client.post("/api/auth/password", json={"current": member.password, "new": PW},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert client.post("/api/auth/logout", json={}, headers={"X-CSRF-Token": csrf}).json() == {"ok": True}
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/login", json={"email": member.email, "password": PW}).status_code == 200

    link = admin_client.post(f"/api/admin/users/{member.id}/reset-link", json={}).json()
    token = link["url"].split("#reset/")[1]
    assert client.post("/api/auth/reset", json={"token": token, "new": "a-brand-new-pass-77"}).json() == {"ok": True}
    assert client.get("/api/me").status_code == 401  # all sessions ended
    assert client.post("/api/auth/reset", json={"token": token, "new": "again-new-pass-88"}).status_code == 400
    assert client.post("/api/auth/login", json={"email": member.email,
                                                "password": "a-brand-new-pass-77"}).status_code == 200


def test_disable_revokes_sessions_devices_tokens_and_jobs(member_client, member, admin_client, make_device):
    did, hdr = make_device(member)
    jid = jobs.enqueue(member.id, "score_account", {"account_id": "acc_x"})
    with db.tx() as conn:
        conn.execute(db.oauth_tokens.insert().values(token_hash="h1", kind="access", client_id="c", user_id=member.id,
                                                     family_id="f", expires_at=db.utcnow() + timedelta(hours=1)))
    assert member_client.get("/api/me").status_code == 200
    r = admin_client.patch(f"/api/admin/users/{member.id}", json={"status": "disabled"})
    assert r.status_code == 200 and r.json()["user"]["status"] == "disabled"
    assert member_client.get("/api/me").status_code == 401
    with db.tx(immediate=False) as conn:
        assert conn.execute(select(db.devices.c.revoked_at).where(db.devices.c.id == did)).scalar() is not None
        assert conn.execute(select(db.oauth_tokens.c.revoked_at)).scalar() is not None
        assert conn.execute(select(db.jobs.c.state).where(db.jobs.c.id == jid)).scalar() == "canceled"
    r = admin_client.post("/api/auth/login", json={"email": member.email, "password": member.password})
    assert r.status_code == 403 and "turned off" in r.json()["error"]


def test_admin_only(member_client):
    for path in ("/api/admin/users", "/api/admin/settings", "/api/admin/stats", "/api/admin/invites"):
        assert member_client.get(path).status_code == 403


def test_admin_cannot_lock_themselves_out(admin_client, admin):
    assert admin_client.patch(f"/api/admin/users/{admin.id}", json={"status": "disabled"}).status_code == 400
    assert admin_client.patch(f"/api/admin/users/{admin.id}", json={"role": "member"}).status_code == 400


def test_settings_get_patch_validate(admin_client):
    s = admin_client.get("/api/admin/settings").json()
    assert s["signup_mode"] == "pending" and s["max_pulls_per_day"] == 5
    assert not any(k.startswith("internal.") for k in s)
    r = admin_client.patch("/api/admin/settings", json={"signup_mode": "active", "max_pulls_per_day": 3})
    assert r.json()["signup_mode"] == "active" and r.json()["max_pulls_per_day"] == 3
    assert admin_client.patch("/api/admin/settings", json={"signup_mode": "open"}).status_code == 400
    assert admin_client.patch("/api/admin/settings", json={"max_pulls_per_day": -1}).status_code == 400
    assert admin_client.patch("/api/admin/settings", json={"nope": 1}).status_code == 400


def test_signup_mode_active(client, admin_client, community_code):
    admin_client.patch("/api/admin/settings", json={"signup_mode": "active"})
    assert signup(client, community_code).json()["status"] == "active"


def test_community_code_rotation_with_48h_grace(client, admin_client, community_code):
    with db.tx() as conn:
        conn.execute(db.invites.update().where(db.invites.c.revoked_at.is_(None))
                     .values(created_at=db.utcnow() - timedelta(days=31)))
    assert jobs.HANDLERS["rotate_code"].fn(None, None) == {"rotated": True}
    banner = admin_client.get("/api/admin/invites").json()["new_code"]
    assert banner["code"] != community_code
    # old code still works for 48 h, new code works too
    assert signup(client, community_code, email="a1@example.com").status_code == 201
    assert signup(client, banner["code"], email="a2@example.com").status_code == 201
    with db.tx() as conn:
        conn.execute(db.invites.update().where(db.invites.c.revoked_at.is_not(None))
                     .values(expires_at=db.utcnow() - timedelta(seconds=1)))
    assert signup(client, community_code, email="a3@example.com").status_code == 403
    assert admin_client.post("/api/admin/invites/community/ack", json={}).json() == {"ok": True}
    assert "new_code" not in admin_client.get("/api/admin/invites").json()


def test_manual_new_code_stops_the_old_one_at_once(client, admin_client, community_code):
    r = admin_client.post("/api/admin/invites/community", json={"code": "summer-2026"})
    assert r.json()["code"] == "summer-2026"
    assert signup(client, community_code).status_code == 403
    assert signup(client, "SUMMER 2026").status_code == 201


def test_sync_members_preview_and_apply(admin_client, make_user):
    stay = make_user("stay@example.com")
    gone = make_user("gone@example.com")
    csv_bytes = b"Name,Email,Joined\nStay,stay@example.com,2026\nNewbie,newbie@example.com,2026\n"
    r = admin_client.post("/api/admin/sync-members", files={"file": ("members.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["in_both"] == 1 and p["new_in_skool"] == 1
    assert [x["email"] for x in p["not_in_skool"]] == ["gone@example.com"]
    r = admin_client.post(f"/api/admin/sync-members/{p['preview_id']}/apply",
                          json={"disable_ids": [gone.id, stay.id], "allowlist_new": True})
    assert r.json() == {"disabled": 1, "allowlisted": 1}
    with db.tx(immediate=False) as conn:
        u = {r.email: r for r in conn.execute(select(db.users))}
        assert u["gone@example.com"].disabled_reason == "left_community"
        assert u["stay@example.com"].status == "active"
        assert conn.execute(select(db.allowlist.c.email)).scalars().all() == ["newbie@example.com"]
    bad = admin_client.post("/api/admin/sync-members", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert bad.status_code == 400


def test_zapier_hooks(client, monkeypatch, make_user, community_code):
    from reelvault.server import settings

    assert client.post("/api/hooks/skool-allowlist", json={"email": "z@example.com"}).status_code == 404
    monkeypatch.setenv("SKOOL_HOOK_SECRET", "hook-secret-for-tests")
    settings.reload()
    h = {"X-Hook-Secret": "hook-secret-for-tests"}
    assert client.post("/api/hooks/skool-allowlist", json={"email": "z@example.com"}).status_code == 403
    assert client.post("/api/hooks/skool-allowlist", json={"email": "Z@example.com"}, headers=h).json() == {"ok": True}
    assert signup(client, community_code, email="z@example.com").json()["status"] == "active"
    left = make_user("left@example.com")
    assert client.post("/api/hooks/skool-removed", json={"email": "left@example.com"}, headers=h).json() == {"ok": True}
    with db.tx(immediate=False) as conn:
        r = conn.execute(select(db.users).where(db.users.c.id == left.id)).first()
    assert r.status == "disabled" and r.disabled_reason == "left_community"


def test_reconfirm_gate(member_client, member, admin_client, community_code):
    admin_client.patch("/api/admin/settings", json={"reconfirm_days": 90})
    with db.tx() as conn:
        conn.execute(db.users.update().where(db.users.c.id == member.id)
                     .values(membership_checked_at=db.utcnow() - timedelta(days=91)))
    me = member_client.get("/api/me")
    assert me.status_code == 200 and me.json()["reconfirm_needed"] is True
    r = member_client.get("/api/jobs")
    assert r.status_code == 403 and r.json()["code"] == "reconfirm_needed"
    assert member_client.post("/api/auth/reconfirm", json={"code": "WRONG"}).status_code == 403
    assert member_client.post("/api/auth/reconfirm", json={"code": community_code}).json() == {"ok": True}
    assert member_client.get("/api/jobs").status_code == 200
    assert admin_client.get("/api/admin/users").status_code == 200  # admins are never gated


def test_inactivity_expiry(client, make_user):
    old = make_user("old@example.com")
    fresh = make_user("fresh@example.com")
    with db.tx() as conn:
        conn.execute(db.users.update().where(db.users.c.id == old.id).values(
            last_seen_at=db.utcnow() - timedelta(days=121), last_login_at=None))
    assert api_admin.expire_members() == {"disabled": 1}
    r = client.post("/api/auth/login", json={"email": old.email, "password": old.password})
    assert r.status_code == 403 and "120 days" in r.json()["error"]
    assert client.post("/api/auth/login", json={"email": fresh.email, "password": fresh.password}).status_code == 200


def test_delete_me_and_export(member_client, member):
    zip_r = member_client.get("/api/me/export")
    assert zip_r.status_code == 200 and zip_r.headers["content-type"] == "application/zip"
    import io
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(zip_r.content)).namelist()
    assert "me.json" in names and "outputs.json" in names
    assert b"password_hash" not in zipfile.ZipFile(io.BytesIO(zip_r.content)).read("me.json")
    assert member_client.request("DELETE", "/api/me", json={"password": "wrong-one-xx"}).status_code == 400
    assert member_client.request("DELETE", "/api/me", json={"password": member.password}).json() == {"ok": True}
    with db.tx(immediate=False) as conn:
        assert conn.execute(select(db.users).where(db.users.c.id == member.id)).first() is None
        assert conn.execute(select(db.profiles).where(db.profiles.c.user_id == member.id)).first() is None


def test_admin_stats_and_delete_user(admin_client, make_user):
    u = make_user("bye@example.com")
    s = admin_client.get("/api/admin/stats").json()
    assert {"members", "pending", "active_7d", "reels", "storage_mb", "jobs_failed_24h", "disk_free_mb",
            "load"} <= set(s)
    assert admin_client.request("DELETE", f"/api/admin/users/{u.id}", json={"confirm": "x"}).status_code == 400
    assert admin_client.request("DELETE", f"/api/admin/users/{u.id}",
                                json={"confirm": "bye@example.com"}).json() == {"ok": True}
    assert admin_client.patch(f"/api/admin/users/{u.id}", json={"status": "active"}).status_code == 404


def test_support_view_needs_member_grant(admin_client, member):
    api_admin.register_support_mirror("reels", lambda uid, q: {"reels": [], "for": uid})
    assert admin_client.get(f"/api/admin/support/{member.id}/reels").status_code == 404
    with db.tx() as conn:
        conn.execute(db.admin_access.insert().values(user_id=member.id, granted_at=db.utcnow(),
                                                     expires_at=db.utcnow() + timedelta(days=7)))
    assert admin_client.get(f"/api/admin/support/{member.id}/reels").json() == {"reels": [], "for": member.id}
    with db.tx(immediate=False) as conn:
        assert conn.execute(select(db.audit_log.c.action).where(db.audit_log.c.action == "admin_viewed")).first()
    api_admin.SUPPORT_MIRRORS.pop("reels", None)


def test_first_boot_creates_admin_and_code(rv_env, monkeypatch):
    from reelvault.server import settings
    from reelvault.server.app import create_app

    monkeypatch.setenv("ADMIN_EMAIL", "Owner@Example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-pass-12345")
    settings.reload()
    create_app("web")
    create_app("web")  # idempotent
    with db.tx(immediate=False) as conn:
        admins = conn.execute(select(db.users).where(db.users.c.role == "admin")).all()
        assert [a.email for a in admins] == ["owner@example.com"]
        assert api_admin.current_community(conn) is not None
