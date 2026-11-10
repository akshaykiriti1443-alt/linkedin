"""/api/admin/*, /api/hooks/skool-* (DESIGN-SAAS.md 4.3, 11.1, 11.5) + invites, code rotation, inactivity expiry.
WP1. Role admin only (else 403).

Other packages plug into two admin views without editing this file:
    from reelvault.server.api_admin import register_support_mirror, register_community_formats
    register_support_mirror("reels", fn)          # GET /api/admin/support/<uid>/reels -> fn(user_id, query) -> dict
    register_community_formats(fn)                # GET /api/admin/community-formats -> fn() -> {"month","formats"}
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
from datetime import timedelta
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select

from . import db, ratelimit, security, storage
from .errors import ApiError, bad_input, forbidden, not_found
from .settings import get_settings

log = logging.getLogger("reelvault.admin")
router = APIRouter()

SUPPORT_MIRRORS: dict[str, Callable[[str, dict], dict]] = {}
_COMMUNITY: list[Callable[[], dict]] = []


def register_support_mirror(name: str, fn: Callable[[str, dict], dict]) -> None:
    SUPPORT_MIRRORS[name] = fn


def register_community_formats(fn: Callable[[], dict]) -> None:
    _COMMUNITY[:] = [fn]


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ============================================================================================== invites

def find_invite(conn, code: str):
    """The invite row a typed code opens, or None. Community codes keep working until expires_at after a
    rotation (48 h grace); a manual new code ends the old one at once."""
    if not code or len(code) > 64:
        return None
    h = security.code_hash(code)
    now = db.utcnow()
    i = db.invites
    row = conn.execute(select(i).where(i.c.code_hash == h)).first()
    if row is None:
        return None
    if row.uses_left is not None and row.uses_left <= 0:
        return None
    if row.revoked_at is None:
        return row if (row.expires_at is None or row.expires_at > now) else None
    if row.kind == "community" and row.expires_at is not None and row.expires_at > now:
        return row
    return None


def current_community(conn):
    i = db.invites
    return conn.execute(select(i).where(i.c.kind == "community", i.c.revoked_at.is_(None))
                        .order_by(i.c.created_at.desc())).first()


def create_community_code(conn, *, code: str | None = None, grace_hours: int = 0, created_by: str | None = None,
                          banner: bool = True) -> str:
    """Make a new community code; the old one stops at once (grace_hours=0) or after grace_hours."""
    now = db.utcnow()
    i = db.invites
    code = code or security.new_code()
    conn.execute(i.update().where(i.c.kind == "community", i.c.revoked_at.is_(None)).values(
        revoked_at=now, expires_at=now + timedelta(hours=grace_hours)))
    conn.execute(i.insert().values(id=db.new_id("inv"), kind="community", code_hash=security.code_hash(code),
                                   code_hint=security.norm_code(code)[-3:], label="Community code", uses_left=None,
                                   used_count=0, created_by=created_by, created_at=now))
    if banner:
        db.set_setting("internal.code_banner", {"code": code, "created_at": db.iso(now)}, conn=conn)
    db.audit("invite_rotated", actor=created_by or "system", detail={"grace_hours": grace_hours}, conn=conn)
    return code


def bootstrap() -> None:
    """First boot: create the admin from ADMIN_EMAIL/ADMIN_PASSWORD when no admin exists; make sure a
    community code exists (shown to the admin in a banner until 'I posted it')."""
    s = get_settings()
    u = db.users
    with db.tx(immediate=False) as conn:
        has_admin = conn.execute(select(u.c.id).where(u.c.role == "admin")).first() is not None
        has_code = current_community(conn) is not None
    if not has_admin and s.admin_email and s.admin_password:
        h = security.hash_password(s.admin_password)
        with db.tx() as conn:
            if conn.execute(select(u.c.id).where(u.c.role == "admin")).first() is None:
                ex = conn.execute(select(u.c.id).where(u.c.email == s.admin_email)).first()
                now = db.utcnow()
                if ex:
                    conn.execute(u.update().where(u.c.id == ex[0]).values(role="admin", status="active"))
                else:
                    uid = db.new_id("usr")
                    conn.execute(u.insert().values(id=uid, email=s.admin_email, name="Admin", password_hash=h,
                                                   status="active", role="admin", created_at=now,
                                                   membership_checked_at=now,
                                                   settings=dict(db.USER_SETTINGS_DEFAULT)))
                    db.upsert(conn, db.profiles, {"user_id": uid, "brand_id": "me", "status": "empty",
                                                  "answers": {}, "updated_at": now}, do_nothing=True)
                log.info("admin created")
    if not has_code:
        with db.tx() as conn:
            if current_community(conn) is None:
                create_community_code(conn)


def rotate_code_if_due() -> dict:
    days = int(db.get_setting("community_code_days") or 0)
    with db.tx() as conn:
        cur = current_community(conn)
        if cur is None:
            create_community_code(conn)
            return {"rotated": True}
        if days > 0 and cur.created_at + timedelta(days=days) <= db.utcnow():
            create_community_code(conn, grace_hours=48)
            return {"rotated": True}
    return {"rotated": False}


def expire_members() -> dict:
    """Disable members with no use for expire_inactive_days (reason 'inactive')."""
    days = int(db.get_setting("expire_inactive_days") or 0)
    if days <= 0:
        return {"disabled": 0}
    cutoff = db.utcnow() - timedelta(days=days)
    u = db.users
    last = func.coalesce(u.c.last_seen_at, u.c.last_login_at, u.c.created_at)
    with db.tx() as conn:
        ids = [r[0] for r in conn.execute(select(u.c.id).where(u.c.status == "active", u.c.role == "member",
                                                                 last < cutoff))]
        for uid in ids:
            disable_user(conn, uid, "inactive", actor="system")
    return {"disabled": len(ids)}


# ============================================================================================== users

def disable_user(conn, user_id: str, reason: str, *, actor: str) -> None:
    conn.execute(db.users.update().where(db.users.c.id == user_id).values(status="disabled",
                                                                         disabled_reason=reason))
    security.revoke_everything(conn, user_id)
    db.audit("user_disabled", user_id=user_id, actor=actor, detail={"reason": reason}, conn=conn)


def approve_user(conn, user_id: str, *, actor: str) -> bool:
    n = conn.execute(db.users.update().where(db.users.c.id == user_id, db.users.c.status == "pending").values(
        status="active", membership_checked_at=db.utcnow())).rowcount
    if n:
        db.audit("user_approved", user_id=user_id, actor=actor, conn=conn)
        from . import mailer

        if mailer.configured():
            u = conn.execute(select(db.users).where(db.users.c.id == user_id)).first()
            mailer.youre_in(u)  # background send; the text does not depend on this transaction
    return n > 0


def _user_row_json(conn, r) -> dict:
    acc = conn.execute(select(func.count()).select_from(db.accounts).where(db.accounts.c.user_id == r.id)).scalar()
    reels = conn.execute(select(func.count()).select_from(db.reels).where(db.reels.c.user_id == r.id)).scalar()
    return {"id": r.id, "name": r.name, "email": r.email, "skool_url": r.skool_url, "status": r.status,
            "role": r.role, "created_at": db.iso(r.created_at), "last_login_at": db.iso(r.last_login_at),
            "last_seen_at": db.iso(r.last_seen_at), "disabled_reason": r.disabled_reason, "accounts": int(acc or 0),
            "reels": int(reels or 0), "storage_mb": round((r.storage_bytes or 0) / 1024 / 1024, 1),
            "ai_mode": r.ai_mode, "support_access": support_grant_live(conn, r.id)}


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
    except (ValueError, UnicodeDecodeError):
        raise bad_input("That list position is not valid. Start again from the top.") from None


def _limit(limit: int | None) -> int:
    return max(1, min(200, int(limit or 50)))


def _get_user(conn, user_id: str):
    r = conn.execute(select(db.users).where(db.users.c.id == user_id)).first()
    if r is None:
        raise not_found("We couldn't find that member.")
    return r


@router.get("/api/admin/users")
def list_users(status: str | None = None, limit: int = 50, cursor: str | None = None, q: str | None = None,
               admin=Depends(security.current_admin)):
    u = db.users
    lim, off = _limit(limit), _offset(cursor)
    stmt = select(u)
    if status:
        stmt = stmt.where(u.c.status == status)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(u.c.email).like(like), func.lower(u.c.name).like(like)))
    stmt = stmt.order_by(u.c.created_at.desc(), u.c.id).offset(off).limit(lim + 1)
    with db.tx(immediate=False) as conn:
        rows = conn.execute(stmt).all()
        out = [_user_row_json(conn, r) for r in rows[:lim]]
    return {"users": out, "next_cursor": _cursor(off + lim) if len(rows) > lim else None}


class UserPatch(Body):
    status: str | None = None
    role: str | None = None


@router.patch("/api/admin/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, request: Request, admin=Depends(security.current_admin)):
    if body.status is not None and body.status not in ("active", "disabled"):
        raise bad_input("Status must be active or disabled.")
    if body.role is not None and body.role not in ("member", "admin"):
        raise bad_input("Role must be member or admin.")
    if user_id == admin.id and (body.status == "disabled" or body.role == "member"):
        raise bad_input("You can't turn off or demote your own account.")
    with db.tx() as conn:
        r = _get_user(conn, user_id)
        if body.role and body.role != r.role:
            conn.execute(db.users.update().where(db.users.c.id == user_id).values(role=body.role))
            db.audit("role_changed", user_id=user_id, actor=admin.id, detail={"role": body.role}, conn=conn)
        if body.status == "disabled" and r.status != "disabled":
            disable_user(conn, user_id, "admin", actor=admin.id)
        elif body.status == "active" and r.status != "active":
            conn.execute(db.users.update().where(db.users.c.id == user_id).values(
                status="active", disabled_reason=None, membership_checked_at=db.utcnow(), last_seen_at=db.utcnow()))
            db.audit("user_enabled", user_id=user_id, actor=admin.id, conn=conn)
        out = _user_row_json(conn, _get_user(conn, user_id))
    return {"user": out}


@router.post("/api/admin/users/{user_id}/approve")
def approve(user_id: str, admin=Depends(security.current_admin)):
    with db.tx() as conn:
        r = _get_user(conn, user_id)
        if r.status != "pending":
            raise bad_input("This member is not waiting for approval.")
        approve_user(conn, user_id, actor=admin.id)
        out = _user_row_json(conn, _get_user(conn, user_id))
    return {"user": out}


@router.post("/api/admin/users/{user_id}/reject")
def reject(user_id: str, request: Request, admin=Depends(security.current_admin)):
    """Reject a pending signup: the account and its (empty) data are deleted, so the email can sign up again
    later with a new code or invite."""
    from .api_auth import delete_user as _delete

    with db.tx(immediate=False) as conn:
        r = _get_user(conn, user_id)
    if r.status != "pending":
        raise bad_input("Only members waiting for approval can be rejected. Turn this member off instead.")
    db.audit("user_rejected", actor=admin.id, detail={"email_domain": r.email.split("@")[-1]},
             ip=security.client_ip(request))
    _delete(user_id, actor=admin.id, ip=security.client_ip(request))
    return {"ok": True}


class ConfirmIn(Body):
    confirm: str = ""


@router.delete("/api/admin/users/{user_id}")
def delete_user(user_id: str, body: ConfirmIn, request: Request, admin=Depends(security.current_admin)):
    from .api_auth import delete_user as _delete

    if user_id == admin.id:
        raise bad_input("You can't delete your own account here.")
    with db.tx(immediate=False) as conn:
        r = _get_user(conn, user_id)
    if (body.confirm or "").strip().lower() != r.email:
        raise bad_input("Type the member's email to confirm.")
    _delete(user_id, actor=admin.id, ip=security.client_ip(request))
    return {"ok": True}


@router.post("/api/admin/users/{user_id}/reset-link")
def reset_link(user_id: str, admin=Depends(security.current_admin)):
    tok, h = security.new_token("rst")
    exp = db.utcnow() + timedelta(hours=24)
    with db.tx() as conn:
        _get_user(conn, user_id)
        conn.execute(db.reset_links.insert().values(token_hash=h, user_id=user_id, created_by=admin.id,
                                                    expires_at=exp))
        db.audit("reset_link_made", user_id=user_id, actor=admin.id, conn=conn)
    return {"url": f"{get_settings().public_url}/#reset/{tok}", "expires_at": db.iso(exp)}


# ============================================================================================== invites API

def _invite_json(r) -> dict:
    return {"id": r.id, "label": r.label, "uses_left": r.uses_left, "expires_at": db.iso(r.expires_at),
            "used_count": r.used_count, "revoked_at": db.iso(r.revoked_at), "code_hint": r.code_hint,
            "created_at": db.iso(r.created_at)}


@router.get("/api/admin/invites")
def list_invites(admin=Depends(security.current_admin)):
    i = db.invites
    with db.tx(immediate=False) as conn:
        com = current_community(conn)
        singles = conn.execute(select(i).where(i.c.kind == "single").order_by(i.c.created_at.desc())
                               .limit(200)).all()
        banner = db.get_setting("internal.code_banner", conn=conn)
    out: dict[str, Any] = {
        "community": ({"id": com.id, "code_hint": com.code_hint, "created_at": db.iso(com.created_at),
                       "used_count": com.used_count} if com else None),
        "single": [_invite_json(r) for r in singles],
        "signup_url": f"{get_settings().public_url}/#join",
    }
    if banner:
        out["new_code"] = banner
    return out


class CommunityIn(Body):
    code: str | None = None


@router.post("/api/admin/invites/community")
def new_community_code(body: CommunityIn, admin=Depends(security.current_admin)):
    code = (body.code or "").strip() or None
    if code is not None:
        n = security.norm_code(code)
        if not (6 <= len(n) <= 32) or not n.isalnum():
            raise bad_input("Use 6 to 32 letters and numbers for the code.")
    with db.tx() as conn:
        code = create_community_code(conn, code=code, created_by=admin.id)
    return {"code": code, "signup_url": f"{get_settings().public_url}/#join"}


@router.post("/api/admin/invites/community/ack")
def ack_code_banner(admin=Depends(security.current_admin)):
    """'I posted it': hide the new-code banner (the code itself is never stored after this)."""
    with db.tx() as conn:
        conn.execute(db.app_settings.delete().where(db.app_settings.c.key == "internal.code_banner"))
    return {"ok": True}


class SingleIn(Body):
    label: str = ""
    uses: int = 1
    expires_days: int = 14


@router.post("/api/admin/invites", status_code=201)
def new_single_invite(body: SingleIn, admin=Depends(security.current_admin)):
    if not (1 <= int(body.uses) <= 100):
        raise bad_input("Uses must be between 1 and 100.")
    if not (1 <= int(body.expires_days) <= 365):
        raise bad_input("Expiry must be between 1 and 365 days.")
    code = security.new_code(groups=3)
    iid = db.new_id("inv")
    now = db.utcnow()
    with db.tx() as conn:
        conn.execute(db.invites.insert().values(
            id=iid, kind="single", code_hash=security.code_hash(code), code_hint=security.norm_code(code)[-3:],
            label=(body.label or "")[:80], uses_left=int(body.uses), used_count=0,
            expires_at=now + timedelta(days=int(body.expires_days)), created_by=admin.id, created_at=now))
        db.audit("invite_created", actor=admin.id, detail={"id": iid}, conn=conn)
    return {"id": iid, "url": f"{get_settings().public_url}/#join/{code}", "code": code}


@router.delete("/api/admin/invites/{invite_id}")
def revoke_invite(invite_id: str, admin=Depends(security.current_admin)):
    i = db.invites
    with db.tx() as conn:
        r = conn.execute(select(i).where(i.c.id == invite_id)).first()
        if r is None:
            raise not_found("We couldn't find that invite.")
        if r.kind == "community":
            raise bad_input("Make a new community code instead. The old one stops working at once.")
        conn.execute(i.update().where(i.c.id == invite_id).values(revoked_at=db.utcnow()))
    return {"ok": True}


# ============================================================================================== settings

SETTING_CHOICES = {"signup_mode": ("pending", "active")}
# Upper bounds that catch typos (a missing comma, an extra zero) before they reach the workers.
SETTING_MAX = {"community_code_days": 365, "expire_inactive_days": 3650, "reconfirm_days": 3650,
               "max_accounts_per_member": 100, "max_pulls_per_day": 100, "outputs_per_day": 1000,
               "member_media_per_day": 10_000, "storage_cap_mb": 1_000_000, "video_keep_days": 3650,
               "baseline_sample_per_account": 500, "insight_window_days": 3650, "recent_window_days": 3650}


def _check_setting(key: str, value: Any) -> Any:
    default = db.APP_SETTINGS_DEFAULTS[key]
    if key in SETTING_CHOICES:
        if value not in SETTING_CHOICES[key]:
            raise bad_input(f"{key} must be one of: {', '.join(SETTING_CHOICES[key])}.")
        return value
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise bad_input(f"{key} must be true or false.")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise bad_input(f"{key} must be a whole number, 0 or more.")
        if value > SETTING_MAX.get(key, 10_000_000):
            raise bad_input(f"{key} can be at most {SETTING_MAX.get(key, 10_000_000):,}.")
        return value
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise bad_input(f"{key} must be an object.")
        return value
    return value


@router.get("/api/admin/settings")
def get_settings_api(admin=Depends(security.current_admin)):
    return db.all_settings()


@router.patch("/api/admin/settings")
def patch_settings(body: dict, admin=Depends(security.current_admin)):
    unknown = [k for k in body if k not in db.APP_SETTINGS_DEFAULTS]
    if unknown:
        raise bad_input(f"Unknown setting: {', '.join(unknown)}.")
    clean = {k: _check_setting(k, v) for k, v in body.items()}
    with db.tx() as conn:
        for k, v in clean.items():
            db.set_setting(k, v, conn=conn)
        db.audit("settings_changed", actor=admin.id, detail={"keys": sorted(clean)}, conn=conn)
        return db.all_settings(conn=conn)


# ============================================================================================== allowlist

class EmailsIn(Body):
    emails: list[str] = []


def _allowlist(conn) -> list[str]:
    return [r[0] for r in conn.execute(select(db.allowlist.c.email).order_by(db.allowlist.c.email))]


def add_to_allowlist(conn, emails: list[str], source: str, *, actor: str) -> int:
    from .api_auth import norm_email, valid_email

    n = 0
    for e in {norm_email(x) for x in emails}:
        if not valid_email(e):
            continue
        db.upsert(conn, db.allowlist, {"email": e, "source": source, "added_at": db.utcnow()}, do_nothing=True)
        u = conn.execute(select(db.users.c.id).where(db.users.c.email == e, db.users.c.status == "pending")).first()
        if u:
            approve_user(conn, u[0], actor=actor)
        n += 1
    return n


@router.get("/api/admin/allowlist")
def get_allowlist(admin=Depends(security.current_admin)):
    with db.tx(immediate=False) as conn:
        return {"emails": _allowlist(conn)}


@router.post("/api/admin/allowlist")
def post_allowlist(body: EmailsIn, admin=Depends(security.current_admin)):
    if len(body.emails) > 5000:
        raise bad_input("Add at most 5,000 emails at a time.")
    with db.tx() as conn:
        add_to_allowlist(conn, body.emails, "admin", actor=admin.id)
        return {"emails": _allowlist(conn)}


@router.delete("/api/admin/allowlist/{email}")
def delete_allowlist(email: str, admin=Depends(security.current_admin)):
    with db.tx() as conn:
        conn.execute(db.allowlist.delete().where(db.allowlist.c.email == email.strip().lower()))
        return {"emails": _allowlist(conn)}


# ============================================================================================== stats

@router.get("/api/admin/stats")
def stats(admin=Depends(security.current_admin)):
    from . import budget

    u, j = db.users, db.jobs
    now = db.utcnow()
    with db.tx(immediate=False) as conn:
        members = conn.execute(select(func.count()).select_from(u).where(u.c.status != "pending")).scalar()
        pending = conn.execute(select(func.count()).select_from(u).where(u.c.status == "pending")).scalar()
        active7 = conn.execute(select(func.count()).select_from(u).where(
            u.c.last_seen_at >= now - timedelta(days=7))).scalar()
        reels = conn.execute(select(func.count()).select_from(db.reels)).scalar()
        storage_b = conn.execute(select(func.coalesce(func.sum(u.c.storage_bytes), 0))).scalar()
        failed = conn.execute(select(func.count()).select_from(j).where(
            j.c.state == "failed", j.c.finished_at >= now - timedelta(hours=24))).scalar()
    return {"members": int(members or 0), "pending": int(pending or 0), "active_7d": int(active7 or 0),
            "reels": int(reels or 0), "storage_mb": round(int(storage_b or 0) / 1024 / 1024, 1),
            "jobs_failed_24h": int(failed or 0), "disk_free_mb": storage.disk_free_mb(), "load": budget.load(),
            "last_backup": db.get_setting("internal.last_backup"), "worker": db.get_setting("internal.worker_heartbeat")}


# ============================================================================================== sync members

SYNC_TTL = timedelta(hours=2)


def _emails_from_csv(data: bytes) -> list[str]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    col = next((n for n, h in enumerate(header) if h in ("email", "e-mail", "email address")), None)
    if col is None:
        raise bad_input("We couldn't find an Email column in that file. Use the member export from Skool.")
    return [r[col] for r in rows[1:] if len(r) > col and r[col].strip()]


@router.post("/api/admin/sync-members")
async def sync_members(request: Request, admin=Depends(security.current_admin)):
    import anyio

    from .api_auth import norm_email, valid_email

    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise bad_input("Attach the Skool member export file.")
        data = await f.read()
        if len(data) > 10 * 1024 * 1024:
            raise ApiError(413, "too_large", "That file is too big. The limit is 10 MB.")
        emails = _emails_from_csv(data)
    else:
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise bad_input("Send a file or a list of emails.") from None
        emails = list((body or {}).get("emails") or [])
    found = {norm_email(e) for e in emails if valid_email(norm_email(e))}
    if not found:
        raise bad_input("We found no email addresses in that list.")

    def build() -> dict:
        u = db.users
        with db.tx() as conn:
            rows = conn.execute(select(u.c.id, u.c.name, u.c.email, u.c.last_seen_at, u.c.status, u.c.role)).all()
            ours = {r.email for r in rows}
            not_in = [{"id": r.id, "name": r.name, "email": r.email, "last_seen_at": db.iso(r.last_seen_at)}
                      for r in rows if r.email not in found and r.status != "disabled" and r.role != "admin"]
            pid = db.new_id("snap").replace("snap_", "sync_")
            db.set_setting(f"internal.sync.{pid}", {"emails": sorted(found), "created_at": db.iso(db.utcnow()),
                                                    "by": admin.id}, conn=conn)
        return {"preview_id": pid, "in_both": len(found & ours), "not_in_skool": not_in,
                "new_in_skool": len(found - ours)}

    return await anyio.to_thread.run_sync(build)


class SyncApply(Body):
    disable_ids: list[str] = []
    allowlist_new: bool = False


@router.post("/api/admin/sync-members/{preview_id}/apply")
def sync_apply(preview_id: str, body: SyncApply, admin=Depends(security.current_admin)):
    from datetime import datetime

    key = f"internal.sync.{preview_id}"
    with db.tx() as conn:
        prev = db.get_setting(key, conn=conn)
        if not prev or datetime.fromisoformat(prev["created_at"].replace("Z", "+00:00")) + SYNC_TTL < db.utcnow():
            raise not_found("This check is too old. Upload the member list again.")
        found = set(prev["emails"])
        u = db.users
        disabled = 0
        for uid in set(body.disable_ids):
            r = conn.execute(select(u).where(u.c.id == uid)).first()
            if r is None or r.role == "admin" or r.email in found or r.status == "disabled":
                continue
            disable_user(conn, uid, "left_community", actor=admin.id)
            disabled += 1
        now = db.utcnow()
        conn.execute(u.update().where(u.c.email.in_(found)).values(membership_checked_at=now))
        allowlisted = 0
        if body.allowlist_new:
            ours = {r[0] for r in conn.execute(select(u.c.email))}
            allowlisted = add_to_allowlist(conn, sorted(found - ours), "skool_sync", actor=admin.id)
        conn.execute(db.app_settings.delete().where(db.app_settings.c.key == key))
    return {"disabled": disabled, "allowlisted": allowlisted}


# ============================================================================================== Zapier hooks

def _hook_check(request: Request) -> None:
    secret = get_settings().skool_hook_secret
    if not secret:
        raise not_found()
    ratelimit.hit_all(request, [("hook_ip", None, 600), ("hook_global", "all", 5000)], "hour")
    if not security.consteq(request.headers.get("x-hook-secret"), secret):
        raise forbidden("Wrong hook secret.")


class HookIn(Body):
    email: str = ""
    first_name: str | None = None
    last_name: str | None = None
    answers: Any = None


@router.post("/api/hooks/skool-allowlist")
def hook_allowlist(body: HookIn, request: Request):
    _hook_check(request)
    from .api_auth import norm_email, valid_email

    e = norm_email(body.email)
    if not valid_email(e):
        raise bad_input("Send a real email address.")
    with db.tx() as conn:
        add_to_allowlist(conn, [e], "zapier", actor="zapier")
    return {"ok": True}


@router.post("/api/hooks/skool-removed")
def hook_removed(body: HookIn, request: Request):
    _hook_check(request)
    from .api_auth import norm_email

    e = norm_email(body.email)
    with db.tx() as conn:
        conn.execute(db.allowlist.delete().where(db.allowlist.c.email == e))
        r = conn.execute(select(db.users).where(db.users.c.email == e)).first()
        if r is not None and r.role != "admin" and r.status != "disabled":
            disable_user(conn, r.id, "left_community", actor="zapier")
    return {"ok": True}


# ============================================================================================== support + community

def support_grant_live(conn, user_id: str) -> bool:
    a = db.admin_access
    now = db.utcnow()
    return conn.execute(select(a.c.user_id).where(a.c.user_id == user_id, a.c.revoked_at.is_(None),
                                                  a.c.expires_at > now)).first() is not None


@router.get("/api/admin/support/{user_id}/{view:path}")
def support_view(user_id: str, view: str, request: Request, admin=Depends(security.current_admin)):
    with db.tx(immediate=False) as conn:
        live = support_grant_live(conn, user_id)
    if not live:
        raise not_found("This member has not turned on support access.")
    name = view.strip("/").split("/")[0]
    fn = SUPPORT_MIRRORS.get(name)
    if fn is None:
        raise not_found()
    db.audit("admin_viewed", user_id=user_id, actor=admin.id, detail={"view": name},
             ip=security.client_ip(request))
    return fn(user_id, dict(request.query_params))


COMMUNITY_MIN_MEMBERS = 5


@router.get("/api/admin/community-formats")
def community_formats(admin=Depends(security.current_admin)):
    if not db.get_setting("community_stats"):
        raise not_found("Community stats are off. Turn them on in Admin > Settings.")
    out = {"month": db.utcnow().strftime("%Y-%m"), "formats": []}
    if _COMMUNITY:
        out.update(_COMMUNITY[0]() or {})
    # 11.5 privacy floor, enforced here whatever the provider returns: only formats used by 5+ members, and only
    # the anonymous fields (no names, handles, reel ids or captions).
    keep = ("vf", "label", "members_using", "lift_median")
    out["formats"] = [{k: f.get(k) for k in keep} for f in out.get("formats") or []
                      if isinstance(f, dict) and int(f.get("members_using") or 0) >= COMMUNITY_MIN_MEMBERS]
    return {"month": out["month"], "formats": out["formats"]}


@router.get("/api/admin/copy")
def get_copy(admin=Depends(security.current_admin)):
    return db.get_setting("copy_overrides") or {}


@router.put("/api/admin/copy")
def put_copy(body: dict, admin=Depends(security.current_admin)):
    if len(json.dumps(body)) > 100_000:
        raise bad_input("That copy is too long.")
    for k, v in body.items():
        if not isinstance(k, str) or not isinstance(v, (str, list)):
            raise bad_input("Each copy block must be text or a list of lines.")
    db.set_setting("copy_overrides", body)
    return body

