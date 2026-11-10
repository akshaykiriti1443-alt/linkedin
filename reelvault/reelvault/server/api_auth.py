"""/api/auth/*, /api/me, /api/invite (DESIGN-SAAS.md 4.2, 11.1, 11.2). WP1."""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from . import db, ratelimit, security, storage
from .errors import ApiError, bad_input, forbidden, too_many

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
SKOOL_RE = re.compile(r"^(?:https?://)?(?:www\.)?skool\.com/@([A-Za-z0-9._\-]{1,80})/?(?:\?.*)?$")
WRONG_CODE = "That access code is not right. Ask in the community for the current one."
PENDING_COPY = ("Thanks! The community admin will approve you soon. You'll get an email if email is set up; "
                "otherwise try logging in later.")
_DUMMY_HASH: list[str] = []


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SignupIn(Body):
    name: str = ""
    email: str = ""
    password: str = ""
    skool_url: str | None = None
    code: str = ""


class LoginIn(Body):
    email: str = ""
    password: str = ""


class CodeIn(Body):
    code: str = ""


class PasswordIn(Body):
    current: str = ""
    new: str = ""


class ResetIn(Body):
    token: str = ""
    new: str = ""


class EmailIn(Body):
    email: str = ""


class PasswordOnly(Body):
    password: str = ""


def norm_email(e: str) -> str:
    return (e or "").strip().lower()


def valid_email(e: str) -> bool:
    return bool(EMAIL_RE.match(e)) and len(e) <= 254


def norm_skool_url(u: str | None) -> str | None:
    if not u or not u.strip():
        return None
    m = SKOOL_RE.match(u.strip())
    if not m:
        raise bad_input("Paste your Skool profile link, like https://www.skool.com/@your-name.")
    return f"https://www.skool.com/@{m.group(1)}"


# ============================================================================================== Me

def setup_state(conn, user) -> dict:
    prof = conn.execute(select(db.profiles.c.status).where(db.profiles.c.user_id == user.id,
                                                           db.profiles.c.brand_id == "me")).first()
    pstat = prof[0] if prof else "empty"
    pstat = {"approved": "approved", "draft": "draft", "drafting": "draft"}.get(pstat, "empty")
    dev = conn.execute(select(db.devices.c.state).where(db.devices.c.user_id == user.id,
                                                        db.devices.c.kind == "extension",
                                                        db.devices.c.revoked_at.is_(None))
                       .order_by(db.devices.c.created_at.desc())).first()
    ext = "none"
    if dev is not None:
        ext = "paired"
        cd = (dev[0] or {}).get("cooldown_until")
        if cd:
            try:
                from datetime import datetime

                if datetime.fromisoformat(str(cd).replace("Z", "+00:00")) > db.utcnow():
                    ext = "cooldown"
            except ValueError:
                pass
    comps = conn.execute(select(func.count()).select_from(db.accounts).where(
        db.accounts.c.user_id == user.id, db.accounts.c.role == "competitor")).scalar() or 0
    return {"profile": pstat, "ai": user.ai_mode is not None, "extension": ext, "competitors": int(comps)}


def me_json(user, csrf: str | None = None) -> dict:
    with db.tx(immediate=False) as conn:
        setup = setup_state(conn, user)
        settings = db.all_settings(conn=conn)
        out: dict[str, Any] = {
            "id": user.id, "name": user.name, "email": user.email, "role": user.role, "status": user.status,
            "ai_mode": user.ai_mode, "setup": setup, "csrf_token": csrf,
            "limits": {"max_accounts": settings["max_accounts_per_member"],
                       "pulls_per_day": settings["max_pulls_per_day"],
                       "outputs_per_day": settings["outputs_per_day"],
                       "media_per_day": settings["member_media_per_day"],
                       "storage_mb": settings["storage_cap_mb"]},
            "reconfirm_needed": security.reconfirm_due(user) if user.status == "active" else False,
        }
        if user.role == "admin":
            pending = conn.execute(select(func.count()).select_from(db.users)
                                   .where(db.users.c.status == "pending")).scalar() or 0
            out["admin"] = {"pending": int(pending), "code_banner": db.get_setting("internal.code_banner", conn=conn)}
    return out


# ============================================================================================== signup

def _signup_paused() -> int:
    """Seconds left of the global 15-minute signup pause (11.1), else 0."""
    until = db.get_setting("internal.signup_paused_until")
    if not until:
        return 0
    from datetime import datetime

    left = (datetime.fromisoformat(until.replace("Z", "+00:00")) - db.utcnow()).total_seconds()
    return int(left) if left > 0 else 0


def code_failed(ck: str) -> None:
    """Count a wrong code: 10 per IP per hour, 200 per hour server-wide (then a 15-minute pause)."""
    try:
        ratelimit.hit("global", "signup_code_fail", 200, "hour")
    except ratelimit.TooMany:
        db.set_setting("internal.signup_paused_until", db.iso(db.utcnow() + timedelta(minutes=15)))
    ratelimit.hit("ip_code_fail", ck, 10, "hour", message="Too many wrong codes. Try again in an hour.")


@router.post("/api/auth/signup", status_code=201)
def signup(body: SignupIn, request: Request):
    from . import api_admin

    ip = security.client_ip(request)
    ck = ratelimit.client_key(request)
    left = _signup_paused()
    if left:
        raise too_many("Too many tries. Try again in 15 minutes.", left)
    ratelimit.check("ip_code_fail", ck, 10, "hour", message="Too many wrong codes. Try again in an hour.")
    name = (body.name or "").strip()
    email = norm_email(body.email)
    if not name or len(name) > 80:
        raise bad_input("Type your name (up to 80 characters).")
    if not valid_email(email):
        raise bad_input("Type a real email address.")
    problem = security.password_problem(body.password)
    if problem:
        raise bad_input(problem)
    skool = norm_skool_url(body.skool_url)
    if not (body.code or "").strip():
        raise bad_input("Type the access code from the community.")

    with db.tx(immediate=False) as conn:
        inv = api_admin.find_invite(conn, body.code)
    if inv is None:
        code_failed(ck)
        raise forbidden(WRONG_CODE)
    if inv.kind == "community":
        ratelimit.hit("code_signup", inv.code_hash, 30, "hour",
                      message="Many people are signing up right now. Try again in an hour.")

    now = db.utcnow()
    pw_hash = security.hash_password(body.password)  # slow: outside the transaction
    value = csrf = None
    with db.tx() as conn:
        if conn.execute(select(db.users.c.id).where(db.users.c.email == email)).first():
            raise ApiError(409, "busy", "There is already an account with that email. Sign in instead.")
        allow = conn.execute(select(db.allowlist.c.email).where(db.allowlist.c.email == email)).first()
        if inv.kind == "single" or allow is not None:
            status = "active"
        else:
            status = "active" if db.get_setting("signup_mode", conn=conn) == "active" else "pending"
        used = conn.execute(db.invites.update().where(
            db.invites.c.id == inv.id, db.invites.c.revoked_at.is_(None) | (db.invites.c.expires_at > now),
            (db.invites.c.uses_left.is_(None)) | (db.invites.c.uses_left > 0)).values(
            used_count=db.invites.c.used_count + 1, uses_left=db.invites.c.uses_left - 1)).rowcount
        if not used:
            raise forbidden(WRONG_CODE)
        uid = db.new_id("usr")
        conn.execute(db.users.insert().values(
            id=uid, email=email, name=name, password_hash=pw_hash, skool_url=skool, status=status, role="member",
            settings=dict(db.USER_SETTINGS_DEFAULT), created_at=now, invite_id=inv.id, membership_checked_at=now,
            last_login_at=now if status == "active" else None))
        db.upsert(conn, db.profiles, {"user_id": uid, "brand_id": "me", "brand_label": "Me", "status": "empty",
                                      "answers": {}, "version": 0, "updated_at": now}, do_nothing=True)
        db.audit("signup", user_id=uid, actor=uid, detail={"status": status, "invite_kind": inv.kind}, ip=ip,
                 conn=conn)
        user = conn.execute(select(db.users).where(db.users.c.id == uid)).first()
        if status == "active":
            value, csrf = security.create_session(conn, uid, request)
    if status != "active":
        _notify_admin_pending(user)
        return JSONResponse({"user": None, "status": "pending", "message": PENDING_COPY}, status_code=201)
    resp = JSONResponse({"user": me_json(user, csrf), "status": "active"}, status_code=201)
    security.set_session_cookie(resp, value)
    return resp


def _notify_admin_pending(user) -> None:
    """One email per new pending signup to the admins (only when email is set up and notify_admin_email)."""
    from . import mailer

    try:
        mailer.pending_signup(user)
    except Exception:  # noqa: BLE001
        pass


# ============================================================================================== login / logout

def _dummy_hash() -> str:
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(security.hash_password("not-a-real-password-xx"))
    return _DUMMY_HASH[0]


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    ip = security.client_ip(request)
    ck = ratelimit.client_key(request)
    email = norm_email(body.email)
    msg = "Too many tries. Try again in 15 minutes."
    ratelimit.check("login_email", email, 5, "quarter", message=msg)
    ratelimit.check("login_ip", ck, 20, "quarter", message=msg)
    with db.tx(immediate=False) as conn:
        user = conn.execute(select(db.users).where(db.users.c.email == email)).first()
    ok = security.verify_password(user.password_hash if user else _dummy_hash(), body.password or "")
    if not user or not ok:
        ratelimit.hit("login_email", email, 10**9, "quarter")
        ratelimit.hit("login_ip", ck, 10**9, "quarter")
        db.audit("login_failed", user_id=user.id if user else None, actor="system", detail={}, ip=ip)
        raise ApiError(401, "not_signed_in", "Email or password is wrong.")
    if user.status == "pending":
        raise forbidden("Your account is waiting for approval.")
    if user.status == "disabled":
        if user.disabled_reason == "inactive":
            days = db.get_setting("expire_inactive_days")
            raise forbidden(f"Your account was turned off after {days} days without use. Message the admin.")
        raise forbidden("Your account is turned off. Message the community admin.")
    new_hash = security.hash_password(body.password) if security.needs_rehash(user.password_hash) else None
    with db.tx() as conn:
        vals: dict = {"last_login_at": db.utcnow(), "last_seen_at": db.utcnow()}
        if new_hash:
            vals["password_hash"] = new_hash
        conn.execute(db.users.update().where(db.users.c.id == user.id).values(**vals))
        value, csrf = security.create_session(conn, user.id, request)
        db.audit("login", user_id=user.id, actor=user.id, ip=ip, conn=conn)
        user = conn.execute(select(db.users).where(db.users.c.id == user.id)).first()
    resp = JSONResponse({"user": me_json(user, csrf)})
    security.set_session_cookie(resp, value)
    return resp


@router.post("/api/auth/logout")
def logout(request: Request):
    sess, _user = security.load_session(request)
    if sess is not None and security.consteq(request.headers.get("x-csrf-token"), sess.csrf_token):
        with db.tx() as conn:
            conn.execute(db.sessions.delete().where(db.sessions.c.id_hash == sess.id_hash))
    resp = JSONResponse({"ok": True})
    security.clear_session_cookie(resp)
    return resp


@router.get("/api/me")
def get_me(request: Request, user=Depends(security.current_user)):
    return me_json(user, request.state.session.csrf_token)


@router.post("/api/auth/reconfirm")
def reconfirm(body: CodeIn, request: Request, user=Depends(security.current_user)):
    from . import api_admin

    ip = security.client_ip(request)
    ck = ratelimit.client_key(request)
    ratelimit.check("ip_code_fail", ck, 10, "hour", message="Too many wrong codes. Try again in an hour.")
    with db.tx(immediate=False) as conn:
        inv = api_admin.find_invite(conn, body.code)
    if inv is None or inv.kind != "community":
        code_failed(ck)
        raise forbidden(WRONG_CODE)
    with db.tx() as conn:
        conn.execute(db.users.update().where(db.users.c.id == user.id).values(membership_checked_at=db.utcnow()))
    return {"ok": True}


@router.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request, user=Depends(security.current_user)):
    ratelimit.check("pw_change", user.id, 5, "quarter")
    if not security.verify_password(user.password_hash, body.current or ""):
        ratelimit.hit("pw_change", user.id, 10**9, "quarter")
        raise bad_input("Your current password is wrong.")
    problem = security.password_problem(body.new)
    if problem:
        raise bad_input(problem)
    h = security.hash_password(body.new)
    with db.tx() as conn:
        conn.execute(db.users.update().where(db.users.c.id == user.id).values(password_hash=h))
        security.end_sessions(conn, user.id, except_hash=request.state.session.id_hash)
        db.audit("password_changed", user_id=user.id, actor=user.id, ip=security.client_ip(request), conn=conn)
    return {"ok": True}


@router.post("/api/auth/reset")
def reset_password(body: ResetIn, request: Request):
    ip = security.client_ip(request)
    ck = ratelimit.client_key(request)
    ratelimit.check("reset_ip", ck, 20, "quarter")
    problem = security.password_problem(body.new)
    if problem:
        raise bad_input(problem)
    th = security.sha256_hex(body.token or "")
    h = security.hash_password(body.new)
    with db.tx(immediate=False) as conn:
        r = conn.execute(select(db.reset_links).where(db.reset_links.c.token_hash == th)).first()
    if r is None or r.used_at is not None or r.expires_at <= db.utcnow():
        ratelimit.hit("reset_ip", ck, 10**9, "quarter")
        raise bad_input("This reset link is not valid any more. Ask the admin for a new one.")
    with db.tx() as conn:
        if conn.execute(db.reset_links.update().where(db.reset_links.c.token_hash == th,
                                                      db.reset_links.c.used_at.is_(None))
                        .values(used_at=db.utcnow())).rowcount == 0:
            raise bad_input("This reset link is not valid any more. Ask the admin for a new one.")
        conn.execute(db.users.update().where(db.users.c.id == r.user_id).values(password_hash=h))
        security.end_sessions(conn, r.user_id)
        db.audit("password_reset", user_id=r.user_id, actor="self", ip=ip, conn=conn)
    return {"ok": True}


@router.post("/api/auth/reset-request")
def reset_request(body: EmailIn, request: Request):
    ip = security.client_ip(request)
    ck = ratelimit.client_key(request)
    ratelimit.hit("reset_req_ip", ck, 20, "hour")
    email = norm_email(body.email)
    from . import mailer

    if not (mailer.configured() and valid_email(email)):
        return {"ok": True}
    try:  # 3 emails per address per hour, 300 per hour server-wide; the answer is always the same
        ratelimit.hit_all(request, [("reset_req_email", ratelimit.key(email), 3),
                                    ("reset_req_global", "all", 300)], "hour")
    except ratelimit.TooMany:
        return {"ok": True}
    with db.tx(immediate=False) as conn:
        u = conn.execute(select(db.users).where(db.users.c.email == email)).first()
    if u is None or u.status != "active":
        return {"ok": True}
    tok, h = security.new_token("rst")
    with db.tx() as conn:
        conn.execute(db.reset_links.insert().values(token_hash=h, user_id=u.id, created_by="self",
                                                    expires_at=db.utcnow() + timedelta(hours=24)))
        db.audit("reset_requested", user_id=u.id, actor="self", ip=ip, conn=conn)
    mailer.reset_link(u, f"{security.get_settings().public_url}/#reset/{tok}")
    return {"ok": True}


@router.get("/api/invite/{code}")
def check_invite(code: str, request: Request):
    from . import api_admin

    ip = security.client_ip(request)
    ck = ratelimit.client_key(request)
    ratelimit.hit("invite_check_ip", ck, 60, "hour")
    with db.tx(immediate=False) as conn:
        inv = api_admin.find_invite(conn, code)
    if inv is None:
        return {"valid": False}
    out: dict = {"valid": True}
    if inv.kind == "single" and inv.label:
        out["label"] = inv.label
    return out


# ============================================================================================== support access (11.5)

class SupportIn(Body):
    days: int = 7


def support_state(conn, user_id: str) -> dict:
    a, al = db.admin_access, db.audit_log
    now = db.utcnow()
    live = conn.execute(select(a).where(a.c.user_id == user_id, a.c.revoked_at.is_(None), a.c.expires_at > now)
                        .order_by(a.c.expires_at.desc())).first()
    views = conn.execute(select(al.c.at).where(al.c.user_id == user_id, al.c.action == "admin_viewed")
                         .order_by(al.c.at.desc()).limit(10)).all()
    return {"active": live is not None, "granted_at": db.iso(live.granted_at) if live else None,
            "expires_at": db.iso(live.expires_at) if live else None,
            "admin_views": [db.iso(v[0]) for v in views],
            "last_viewed_at": db.iso(views[0][0]) if views else None}


def _set_support_until(conn, user, until) -> None:
    cur = conn.execute(select(db.users.c.settings).where(db.users.c.id == user.id)).scalar() or {}
    conn.execute(db.users.update().where(db.users.c.id == user.id).values(
        settings={**dict(db.USER_SETTINGS_DEFAULT), **cur, "support_access_until": db.iso(until) if until else None}))


@router.get("/api/me/support-access")
def get_support_access(user=Depends(security.current_user)):
    """Settings > Account: is the admin allowed to look at my reels and outputs, and when did they look."""
    with db.tx(immediate=False) as conn:
        return support_state(conn, user.id)


@router.post("/api/me/support-access")
def grant_support_access(body: SupportIn, request: Request, user=Depends(security.current_user)):
    """'Let the admin see my reels and outputs for 7 days' (1 to 7 days; replaces a live grant)."""
    days = int(body.days)
    if not 1 <= days <= 7:
        raise bad_input("Pick 1 to 7 days.")
    now = db.utcnow()
    until = now + timedelta(days=days)
    a = db.admin_access
    with db.tx() as conn:
        conn.execute(a.update().where(a.c.user_id == user.id, a.c.revoked_at.is_(None)).values(revoked_at=now))
        conn.execute(a.insert().values(user_id=user.id, granted_at=now, expires_at=until))
        _set_support_until(conn, user, until)
        db.audit("support_access_on", user_id=user.id, actor=user.id, detail={"days": days},
                 ip=security.client_ip(request), conn=conn)
        return support_state(conn, user.id)


@router.delete("/api/me/support-access")
def revoke_support_access(request: Request, user=Depends(security.current_user)):
    a = db.admin_access
    with db.tx() as conn:
        conn.execute(a.update().where(a.c.user_id == user.id, a.c.revoked_at.is_(None))
                     .values(revoked_at=db.utcnow()))
        _set_support_until(conn, user, None)
        db.audit("support_access_off", user_id=user.id, actor=user.id, ip=security.client_ip(request), conn=conn)
        return support_state(conn, user.id)


# ============================================================================================== delete / export

@router.delete("/api/me")
def delete_me(body: PasswordOnly, request: Request, user=Depends(security.current_user)):
    if not security.verify_password(user.password_hash, body.password or ""):
        raise bad_input("Your password is wrong.")
    if user.role == "admin":
        with db.tx(immediate=False) as conn:
            admins = conn.execute(select(func.count()).select_from(db.users).where(
                db.users.c.role == "admin", db.users.c.status == "active")).scalar()
        if admins <= 1:
            raise bad_input("You are the only admin. Make someone else admin first.")
    delete_user(user.id, actor=user.id, ip=security.client_ip(request))
    resp = JSONResponse({"ok": True})
    security.clear_session_cookie(resp)
    return resp


def delete_user(user_id: str, *, actor: str, ip: str | None = None) -> None:
    """Delete the member, every row (FK cascade) and their files."""
    with db.tx() as conn:
        db.audit("delete_account", user_id=None, actor=actor, detail={"user_id": user_id}, ip=ip, conn=conn)
        conn.execute(db.users.delete().where(db.users.c.id == user_id))
    storage.delete_member_files(user_id)


EXPORT_TABLES = ("profiles", "profile_sources", "topics", "formats", "accounts", "pulls", "reels", "analyses",
                 "outputs", "insight_snapshots")


def _jsonable(v):
    if hasattr(v, "isoformat"):
        return db.iso(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None
    return v


@router.get("/api/me/export")
def export_me(request: Request, user=Depends(security.current_user)):
    ratelimit.hit("export", user.id, 5, "hour")
    buf = io.BytesIO()
    with db.tx(immediate=False) as conn, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        me = {k: _jsonable(v) for k, v in user._mapping.items() if k not in ("password_hash",)}
        zf.writestr("me.json", json.dumps(me, indent=2, ensure_ascii=False))
        for name in EXPORT_TABLES:
            t = db.TABLES[name]
            rows = [{k: _jsonable(v) for k, v in r._mapping.items()} for r in conn.execute(db.scoped(conn, t, user.id))]
            zf.writestr(f"{name}.json", json.dumps(rows, indent=2, ensure_ascii=False))
            if name == "outputs":
                for r in rows:
                    if r.get("markdown"):
                        zf.writestr(f"outputs/{r['id']}.md", r["markdown"])
    db.audit("export", user_id=user.id, actor=user.id, ip=security.client_ip(request))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="reelvault-export.zip"'})
