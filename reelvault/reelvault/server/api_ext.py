"""/api/ext/* : pairing (extension + desktop), the extension's device API, the ZIP download (DESIGN-SAAS.md 4.6).

Session endpoints (web UI, cookie + CSRF):
    POST /api/ext/pair · GET /api/ext/status · POST /api/ext/confirm-account · DELETE /api/ext/device
    POST /api/ext/try-again (new: the "Try again" button after signed_out, 5.7) · GET /pair (tiny help page)
    GET /api/admin/instagram-health (new, admin only: the 5.9 "Instagram health" data)
Device endpoints (Authorization: Bearer rvd_..., CORS for chrome-extension://, no cookies):
    POST /api/ext/v2/pair/claim (no auth) · hello · jobs/lease · jobs/<id>/{progress,items,video,page,done,fail}
    ig-user · debug-sample

Other packages use:
    new_pairing_code(user_id, kind, *, conn=None) -> (code, expires_at)   # WP5's POST /api/ai/desktop/pair
    extension_zip() -> bytes                                             # WP1's /download/... route
    latest_version() -> str
The HTTP layer only parses, checks sizes and refuses cookie fields; jobs_ig does the work.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from . import db, jobs_ig, ratelimit, security
from .errors import ApiError, bad_input, not_found
from .settings import EXTENSION_MIN, get_settings

log = logging.getLogger("reelvault.api_ext")
router = APIRouter()

PAIR_TTL = timedelta(minutes=10)
PAIR_MAX_ATTEMPTS = 5
MAX_DESKTOPS = 3
ONLINE_S = 90
JSON_MAX = 2 * 1024 * 1024        # page_read JSON <= 2 MB; every other device JSON is far smaller
ITEMS_MAX = 8 * 1024 * 1024
VERSION_RE = re.compile(r"^\d{1,4}(\.\d{1,5}){0,3}$")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BAD_CODE = "That code is not right or it expired. Make a new link in ReelVault and paste it again."


# ============================================================================================== helpers

def _ext_dir() -> Path | None:
    for cand in (os.environ.get("RV_EXTENSION_DIR"), PROJECT_ROOT / "extension-members",
                 "/opt/reelvault/extension-members"):
        if cand and (Path(cand) / "manifest.json").is_file():
            return Path(cand)
    return None


def latest_version() -> str:
    d = _ext_dir()
    if d is not None:
        try:
            return str(json.loads((d / "manifest.json").read_text(encoding="utf-8")).get("version") or EXTENSION_MIN)
        except (OSError, ValueError):
            pass
    return EXTENSION_MIN


_builder = None


def _load_builder():
    """deploy/build_extension.py (WP1 + WP2) holds the one packer used by the store build and this download."""
    global _builder
    if _builder is not None:
        return _builder
    for cand in (os.environ.get("RV_DEPLOY_DIR"), PROJECT_ROOT / "deploy", "/opt/reelvault/deploy"):
        p = Path(cand) / "build_extension.py" if cand else None
        if p and p.is_file():
            spec = importlib.util.spec_from_file_location("rv_build_extension", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            _builder = mod
            return mod
    raise not_found("The extension download is not ready yet.")


_zip_cache: dict[tuple, bytes] = {}


def extension_zip() -> bytes:
    """The packed extension with config.js = [PUBLIC_URL origin], BUILD "zip" (5.2). Cached per version/origin."""
    origin = get_settings().public_origin
    d = _ext_dir()
    if d is None:
        raise not_found("The extension download is not ready yet.")
    stamp = max((p.stat().st_mtime for p in d.rglob("*") if p.is_file()), default=0)
    key = (origin, str(d), stamp)
    if key not in _zip_cache:
        b = _load_builder()
        try:
            data = b.build_zip([origin], build="zip", src=d)
        except b.BuildError as e:
            log.error("extension build failed: %s", e)
            raise ApiError(500, "server", "The extension download is broken. Tell the admin.") from None
        _zip_cache.clear()
        _zip_cache[key] = data
    return _zip_cache[key]


def new_pairing_code(user_id: str, kind: str, *, conn=None) -> tuple[str, datetime]:
    """Make a pairing code (8 chars Crockford base32, 10 minutes, single use, 5 tries). Stored as sha256."""
    if kind not in ("extension", "desktop"):
        raise ValueError(kind)
    code = security.new_code()
    exp = db.utcnow() + PAIR_TTL
    vals = dict(code_hash=security.code_hash(code), user_id=user_id, kind=kind, expires_at=exp, attempts=0)
    if conn is not None:
        conn.execute(db.pairing_codes.insert().values(**vals))
    else:
        with db.tx() as c:
            c.execute(db.pairing_codes.insert().values(**vals))
    return code, exp


def _active_extension(conn, user_id: str):
    d = db.devices
    return conn.execute(select(d).where(d.c.user_id == user_id, d.c.kind == "extension", d.c.revoked_at.is_(None))
                        .order_by(d.c.created_at.desc())).first()


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    s = max(0, int((db.utcnow() - dt).total_seconds()))
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 2 * 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


async def _json(request: Request, limit: int = JSON_MAX) -> dict:
    """Read a JSON object body (size-capped) and refuse Instagram cookie fields (11.4)."""
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > limit:
        raise ApiError(413, "too_large", "That upload is too big.")
    raw = await request.body()
    if len(raw) > limit:
        raise ApiError(413, "too_large", "That upload is too big.")
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise bad_input("The body must be JSON.") from None
    if not isinstance(body, dict):
        raise bad_input("The body must be a JSON object.")
    hit = jobs_ig.find_forbidden_keys(body)
    if hit:
        dev = getattr(request.state, "device", None)
        log.error("ALERT: extension upload contained a cookie field (%s) on %s", hit, request.url.path)
        await run_in_threadpool(db.audit, "ext_cookie_field", user_id=getattr(dev, "user_id", None),
                                detail={"path": request.url.path, "field": hit})
        raise bad_input("ReelVault never accepts Instagram cookies. Update the extension.")
    return body


# ============================================================================================== session endpoints

@router.post("/api/ext/pair")
def pair_start(user=Depends(security.current_user)):
    ratelimit.hit("ext_pair", user.id, 10, "hour", message="You made many links. Wait an hour, then try again.")
    code, exp = new_pairing_code(user.id, "extension")
    origin = get_settings().public_origin
    return {"code": code, "pair_link": f"{origin}/pair#{code}", "expires_at": db.iso(exp)}


@router.get("/api/ext/status")
def ext_status(user=Depends(security.current_user)):
    s = get_settings()
    latest = latest_version()
    out: dict = {"paired": False, "latest_version": latest, "min_version": EXTENSION_MIN,
                 "download_url": "/download/reelvault-members-extension.zip",
                 "budget": {"hour_used": 0, "hour_max": jobs_ig.PACING["hour_max"], "day_used": 0,
                            "day_max": jobs_ig.PACING["day_max"]}}
    store = os.environ.get("EXTENSION_STORE_URL", "").strip()
    if store.startswith("https://"):
        out["store_url"] = store
    with db.tx(immediate=False) as conn:
        dev = _active_extension(conn, user.id)
        if dev is None:
            return out
        bv = jobs_ig.budget_view(conn, dev)
    st = jobs_ig.device_state(dev)
    now = db.utcnow()
    online = bool(dev.last_seen_at and (now - dev.last_seen_at).total_seconds() < ONLINE_S)
    cd = st.get("cooldown_until") if jobs_ig.cooldown_active(st, now) else None
    device = {"id": dev.id, "name": dev.name, "version": dev.version, "ig_username": dev.ig_username,
              "ig_user_id": dev.ig_user_id, "ig_confirmed": jobs_ig.is_confirmed(dev),
              "signed_in": st.get("signed_in"), "last_seen_at": db.iso(dev.last_seen_at), "online": online,
              "last_seen_text": ("The extension is on." if online else
                                 f"Extension last seen {_ago(dev.last_seen_at)}: open Chrome to continue."),
              "update_available": jobs_ig.version_tuple(dev.version) < jobs_ig.version_tuple(latest),
              "update_needed": jobs_ig.version_tuple(dev.version) < jobs_ig.version_tuple(EXTENSION_MIN)}
    if cd:
        device["cooldown_until"] = cd
        device["cooldown_reason"] = st.get("cooldown_reason")
        device["cooldown_text"] = jobs_ig.MSG.get(st.get("cooldown_reason") or "", jobs_ig.MSG["http_error"])
    if device["update_available"]:
        device["update_text"] = ("Update available: download the new ZIP, extract it over the old folder, then press "
                                 "Reload on chrome://extensions.")
    out.update(paired=True, device=device,
               budget={"hour_used": bv["hour_used"], "hour_max": bv["hour_max"], "day_used": bv["day_used"],
                       "day_max": bv["day_max"], "cdn_day_used": bv["cdn_day_used"], "cdn_day_max": bv["cdn_day_max"]})
    if s.public_origin:
        out["download_url"] = "/download/reelvault-members-extension.zip"
    return out


@router.post("/api/ext/confirm-account")
async def confirm_account(request: Request, user=Depends(security.current_user)):
    body = await _json(request, 4096)
    uid = body.get("ig_user_id")
    if not (isinstance(uid, str) and jobs_ig.PK_RE.match(uid)):
        raise bad_input("Pick the Instagram account the extension shows.")

    def run():
        with db.tx() as conn:
            dev = _active_extension(conn, user.id)
            if dev is None:
                raise not_found("Connect the extension first.")
            if dev.ig_user_id != uid:
                raise bad_input("The extension now reads a different Instagram account. Open the extension and "
                                "check which account it shows.")
            conn.execute(db.devices.update().where(db.devices.c.id == dev.id).values(ig_confirmed_user_id=uid))
            db.audit("ig_account_confirmed", user_id=user.id, actor=user.id,
                     detail={"device": dev.id, "ig_username": dev.ig_username}, conn=conn)
        return {"ok": True}

    return await run_in_threadpool(run)


@router.delete("/api/ext/device")
def disconnect(request: Request, user=Depends(security.current_user)):
    now = db.utcnow()
    with db.tx() as conn:
        d = db.devices
        n = conn.execute(d.update().where(d.c.user_id == user.id, d.c.kind == "extension", d.c.revoked_at.is_(None))
                         .values(revoked_at=now)).rowcount
        if n:
            db.audit("device_revoked", user_id=user.id, actor=user.id, detail={"kind": "extension"},
                     ip=security.client_ip(request), conn=conn)
    return {"ok": True}


@router.post("/api/ext/try-again")
def try_again(user=Depends(security.current_user)):
    return jobs_ig.try_again(user.id)


@router.get("/api/admin/instagram-health")
def instagram_health(admin=Depends(security.current_admin)):
    """Admin > Instagram health (5.9): the key tree of the first answer per endpoint, the last 50 failure codes,
    recoveries used, pk conflicts and which fields Instagram sent. Metadata only: no member content."""
    return jobs_ig.ig_health()


@router.get("/pair", include_in_schema=False)
def pair_page():
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connect the extension · ReelVault</title>
</head><body style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 3rem auto; padding: 0 1rem;
line-height: 1.5"><h1 style="font-size: 1.4rem">Connect the ReelVault Members extension</h1>
<ol><li>Copy the link of this page from the address bar (it ends with your code).</li>
<li>Click the ReelVault Members icon in Chrome (the puzzle piece, then ReelVault Members).</li>
<li>Paste the link in the box and click <b>Next</b>.</li></ol>
<p>The code works for 10 minutes. Only paste links that you made yourself in ReelVault.</p>
</body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ============================================================================================== device: pairing

@router.post("/api/ext/v2/pair/claim")
async def pair_claim(request: Request):
    body = await _json(request, 4096)
    code = body.get("code")
    kind = body.get("kind", "extension")
    if not isinstance(code, str) or not (6 <= len(code) <= 20) or kind not in ("extension", "desktop"):
        raise bad_input(BAD_CODE)
    name = body.get("name") if isinstance(body.get("name"), str) else ""
    name = re.sub(r"[\x00-\x1f]", "", name).strip()[:60] or ("Chrome" if kind == "extension" else "Desktop app")
    version = body.get("version") if isinstance(body.get("version"), str) else ""
    if version and not VERSION_RE.match(version):
        raise bad_input("The extension version doesn't look right.")
    ip = security.client_ip(request)          # for the audit row
    client = ratelimit.client_key(request)    # for the limit: one IPv6 /64 = one client (WP1 rule)
    h = security.code_hash(code)

    def run():
        ratelimit.hit("pair_claim_ip", client, 20, "hour", message="Too many tries. Try again in an hour.")
        ratelimit.hit("pair_claim", "global", 300, "hour", message="Too many tries. Try again in an hour.")
        ratelimit.hit("pair_claim_code", h, PAIR_MAX_ATTEMPTS, "hour", message=BAD_CODE)
        now = db.utcnow()
        token, token_hash = security.new_token("rvd")
        dev_id = db.new_id("dev")
        with db.tx() as conn:
            pc = db.pairing_codes
            row = conn.execute(select(pc).where(pc.c.code_hash == h)).first()
            if row is not None:
                conn.execute(pc.update().where(pc.c.code_hash == h).values(attempts=pc.c.attempts + 1))
            if (row is None or row.used_at is not None or row.expires_at <= now or row.kind != kind
                    or row.attempts >= PAIR_MAX_ATTEMPTS):
                return None
            user = conn.execute(select(db.users).where(db.users.c.id == row.user_id)).first()
            if user is None or user.status != "active":
                return None
            conn.execute(pc.update().where(pc.c.code_hash == h).values(used_at=now))
            d = db.devices
            if kind == "extension":
                conn.execute(d.update().where(d.c.user_id == user.id, d.c.kind == "extension",
                                              d.c.revoked_at.is_(None)).values(revoked_at=now))
            else:
                live = conn.execute(select(d.c.id).where(d.c.user_id == user.id, d.c.kind == "desktop",
                                                         d.c.revoked_at.is_(None))
                                    .order_by(d.c.created_at.desc())).all()
                old = [x[0] for x in live[MAX_DESKTOPS - 1:]]
                if old:
                    conn.execute(d.update().where(d.c.id.in_(old)).values(revoked_at=now))
            conn.execute(d.insert().values(id=dev_id, user_id=user.id, kind=kind, name=name, token_hash=token_hash,
                                           version=version or None, state=dict(db.DEVICE_STATE_DEFAULT),
                                           created_at=now, last_seen_at=now))
            db.audit("device_paired", user_id=user.id, actor=user.id, detail={"device": dev_id, "kind": kind},
                     ip=ip, conn=conn)
            return {"token": token, "device_id": dev_id, "user": {"name": user.name}, "server_time": db.iso(now)}

    out = await run_in_threadpool(run)
    if out is None:
        raise ApiError(403, "forbidden", BAD_CODE)
    return out


# ============================================================================================== device: loop

def _pacing_reply(dev) -> dict:
    st = jobs_ig.device_state(dev)
    out = {"ok": True, "min_version": EXTENSION_MIN, "latest_version": latest_version(),
           "update_url": os.environ.get("EXTENSION_STORE_URL", "").strip()
           or f"{get_settings().public_origin}/download/reelvault-members-extension.zip",
           "pacing": jobs_ig.pacing(), "ig_confirmed": jobs_ig.is_confirmed(dev),
           "ig_username": dev.ig_username}
    if jobs_ig.cooldown_active(st):
        out["cooldown_until"] = st.get("cooldown_until")
        out["cooldown_reason"] = st.get("cooldown_reason")
    return out


@router.post("/api/ext/v2/hello")
async def hello(request: Request, dev=Depends(security.extension_device)):
    body = await _json(request, 16 * 1024)

    def run():
        with db.tx() as conn:
            d = conn.execute(select(db.devices).where(db.devices.c.id == dev.id)).first()
            vals = {"last_seen_at": db.utcnow()}
            v = body.get("version")
            if isinstance(v, str) and VERSION_RE.match(v):
                vals["version"] = v
            conn.execute(db.devices.update().where(db.devices.c.id == dev.id).values(**vals))
            jobs_ig.apply_ig_report(conn, d, body.get("ig"))
            d = conn.execute(select(db.devices).where(db.devices.c.id == dev.id)).first()
        out = _pacing_reply(d)
        out["has_work"] = jobs_ig.has_work(dev.user_id)
        return out

    return await run_in_threadpool(run)


@router.post("/api/ext/v2/jobs/lease")
async def lease(request: Request, dev=Depends(security.extension_device)):
    body = await _json(request, 16 * 1024)
    return await run_in_threadpool(jobs_ig.lease, dev, body.get("kinds"), body.get("budget"))


@router.post("/api/ext/v2/jobs/{job_id}/progress")
async def progress(job_id: str, request: Request, dev=Depends(security.extension_device)):
    body = await _json(request, 64 * 1024)
    return await run_in_threadpool(jobs_ig.progress, dev, job_id, body)


@router.post("/api/ext/v2/jobs/{job_id}/items")
async def items(job_id: str, request: Request, dev=Depends(security.extension_device)):
    body = await _json(request, ITEMS_MAX)
    return await run_in_threadpool(jobs_ig.ingest_items, dev, job_id, body)


@router.post("/api/ext/v2/jobs/{job_id}/page")
async def page(job_id: str, request: Request, dev=Depends(security.extension_device)):
    body = await _json(request, JSON_MAX)
    return await run_in_threadpool(jobs_ig.save_page, dev, job_id, body)


@router.post("/api/ext/v2/jobs/{job_id}/done")
async def done(job_id: str, request: Request, dev=Depends(security.extension_device)):
    body = await _json(request, 256 * 1024)
    return await run_in_threadpool(jobs_ig.finish, dev, job_id, body)


@router.post("/api/ext/v2/jobs/{job_id}/fail")
async def fail(job_id: str, request: Request, dev=Depends(security.extension_device)):
    body = await _json(request, 64 * 1024)
    return await run_in_threadpool(jobs_ig.fail, dev, job_id, body)


@router.post("/api/ext/v2/ig-user")
async def ig_user(request: Request, dev=Depends(security.extension_device)):
    body = await _json(request, 64 * 1024)
    return await run_in_threadpool(jobs_ig.save_member_bio, dev, body)


@router.post("/api/ext/v2/debug-sample")
async def debug_sample(request: Request, dev=Depends(security.extension_device)):
    body = await _json(request, 128 * 1024)
    return await run_in_threadpool(jobs_ig.save_debug_sample, dev, body)


# ============================================================================================== device: video upload

async def _spool(upload, limit: int, magic_ok) -> tuple[Path, int]:
    """Copy an UploadFile to a temp file in DATA_DIR/tmp, refusing more than `limit` bytes or a wrong file type."""
    from . import storage

    fd, name = tempfile.mkstemp(dir=storage.tmp_dir(), prefix="up_")
    path = Path(name)
    n = 0
    head = b""
    try:
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = await upload.read(1 << 20)
                if not chunk:
                    break
                if len(head) < 16:
                    head = (head + chunk)[:16]
                n += len(chunk)
                if n > limit:
                    raise ApiError(413, "too_large", "That video is bigger than 50 MB. ReelVault skips it.")
                f.write(chunk)
        if n == 0 or not magic_ok(head):
            raise bad_input("That file is not the expected type.")
        return path, n
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _is_mp4(head: bytes) -> bool:
    return len(head) >= 8 and head[4:8] == b"ftyp"


def _is_jpeg(head: bytes) -> bool:
    return head[:3] == b"\xff\xd8\xff"


@router.post("/api/ext/v2/jobs/{job_id}/video")
async def video(job_id: str, request: Request, dev=Depends(security.extension_device)):
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > jobs_ig.VIDEO_MAX + jobs_ig.COVER_MAX + 64 * 1024:
        raise ApiError(413, "too_large", "That video is bigger than 50 MB. ReelVault skips it.")
    try:
        form = await request.form(max_files=2, max_fields=4)
    except Exception:  # noqa: BLE001
        raise bad_input("The upload must be a form with reel_id and file.") from None
    try:
        for k in form.keys():
            if str(k).lower() in jobs_ig.FORBIDDEN_KEYS:
                raise bad_input("ReelVault never accepts Instagram cookies. Update the extension.")
        reel_id = form.get("reel_id")
        upload = form.get("file")
        cover = form.get("cover")
        if not isinstance(reel_id, str) or not hasattr(upload, "read"):
            raise bad_input("The upload must have reel_id and file.")
        await run_in_threadpool(jobs_ig.check_video_item, dev, job_id, reel_id)
        vpath, vsize = await _spool(upload, jobs_ig.VIDEO_MAX, _is_mp4)
        cpath, csize = None, 0
        try:
            if cover is not None and hasattr(cover, "read"):
                cpath, csize = await _spool(cover, jobs_ig.COVER_MAX, _is_jpeg)
            return await run_in_threadpool(jobs_ig.save_video, dev, job_id, reel_id, vpath, vsize, cpath, csize)
        finally:
            vpath.unlink(missing_ok=True)
            if cpath is not None:
                cpath.unlink(missing_ok=True)
    finally:
        await form.close()


__all__ = ["router", "extension_zip", "new_pairing_code", "latest_version"]
