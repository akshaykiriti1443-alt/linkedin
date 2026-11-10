"""/files/*, /download/*, /healthz, /api/version, /api/jobs*, /api/home (DESIGN-SAAS.md 4.11). WP1.

GET /api/jobs/<id> lives here (4.8 lists it in the competitors table; one route only, registered by WP1).
GET /api/home calls optional hooks so WP4/WP7 can fill it without editing this file:
    reelvault.server.api_reels.home_parts(user_id) -> {"new_outliers", "cards", "first_look", "formats"}
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy import text

from . import db, jobs, security, storage
from .errors import ApiError, bad_input, not_found
from .settings import EXTENSION_MIN, VERSION

log = logging.getLogger("reelvault.files")
router = APIRouter()


@router.get("/healthz")
def healthz():
    ok_db = "ok"
    try:
        with db.tx(immediate=False) as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        ok_db = "error"
    beat = db.get_setting("internal.worker_heartbeat") if ok_db == "ok" else None
    worker = None
    if beat and beat.get("at"):
        at = datetime.fromisoformat(beat["at"].replace("Z", "+00:00"))
        worker = {"last_seen_at": beat["at"], "ok": db.utcnow() - at < timedelta(minutes=3)}
    return {"ok": ok_db == "ok", "version": VERSION, "db": ok_db, "disk_free_mb": storage.disk_free_mb(),
            "worker": worker}


@router.get("/api/version")
def version():
    return {"version": VERSION, "extension_min": EXTENSION_MIN}


@router.get("/files/{user_id}/{rel:path}")
def member_file(user_id: str, rel: str, user=Depends(security.current_user)):
    if user_id != user.id:
        raise not_found()
    try:
        p = storage.member_path(user_id, rel)
    except storage.UnsafePath:
        raise not_found() from None
    if not p.is_file():
        raise not_found()
    return FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})


def load_extension_builder():
    """deploy/build_extension.py as a module (stdlib only), or None. Looked up at $RV_BUILD_EXTENSION, next to the
    project (dev checkout) and at /opt/reelvault/deploy (the Docker image copies it there)."""
    import importlib.util
    import os
    from pathlib import Path

    cands = [os.environ.get("RV_BUILD_EXTENSION"),
             Path(__file__).resolve().parents[2] / "deploy" / "build_extension.py",
             "/opt/reelvault/deploy/build_extension.py"]
    for c in cands:
        if c and Path(c).is_file():
            spec = importlib.util.spec_from_file_location("rv_build_extension", c)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    return None


def default_extension_zip() -> bytes | None:
    """The 'zip' build for this server's PUBLIC_URL, or None when the extension folder is not shipped."""
    from .settings import get_settings

    mod = load_extension_builder()
    if mod is None:
        return None
    try:
        return mod.build_zip([get_settings().public_url], build="zip")
    except mod.BuildError as e:
        log.warning("extension build failed: %s", e)
        return None


@router.get("/download/reelvault-members-extension.zip")
def extension_zip():
    """WP2 may provide api_ext.extension_zip() -> bytes; otherwise deploy/build_extension.py packs
    extension-members/ with config.js written from PUBLIC_URL at request time (5.2)."""
    from fastapi.responses import Response

    from . import api_ext

    fn = getattr(api_ext, "extension_zip", None)
    data = fn() if fn is not None else default_extension_zip()
    if not data:
        raise not_found("The extension download is not ready yet.")
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="reelvault-members-extension.zip"',
                             "Cache-Control": "no-store"})


# ============================================================================================== jobs API

@router.get("/api/jobs")
def list_jobs(active: int = 0, kind: str | None = None, limit: int = 50, user=Depends(security.current_user)):
    j = db.jobs
    lim = max(1, min(200, int(limit)))
    q = db.scoped(None, j, user.id)
    if active:
        q = q.where(j.c.state.in_(db.ACTIVE_JOB_STATES))
    if kind:
        q = q.where(j.c.kind == kind)
    with db.tx(immediate=False) as conn:
        rows = conn.execute(q.order_by(j.c.created_at.desc()).limit(lim)).all()
    return {"jobs": [jobs.to_api(r) for r in rows]}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, user=Depends(security.current_user)):
    row = jobs.get(job_id, user.id)
    if row is None:
        raise not_found()
    return jobs.to_api(row, include_result=True)


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user=Depends(security.current_user)):
    row = jobs.cancel(job_id, user.id)
    if row is None:
        raise not_found()
    return jobs.to_api(row)


@router.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, user=Depends(security.current_user)):
    row = jobs.get(job_id, user.id)
    if row is None:
        raise not_found()
    if row.state not in ("failed", "paused", "canceled"):
        raise bad_input("Only stopped steps can be tried again.")
    return jobs.to_api(jobs.retry(job_id, user.id))


# ============================================================================================== home

def _next_step(setup: dict) -> dict:
    if setup["profile"] == "empty" and setup["competitors"] == 0:
        return {"label": "Start your setup", "href": "#onboarding/1"}
    if setup["competitors"] == 0:
        return {"label": "Add your first competitor", "href": "#competitors"}
    if setup["extension"] == "none":
        return {"label": "Connect the Chrome extension", "href": "#settings/extension"}
    if not setup["ai"]:
        return {"label": "Choose how ReelVault uses AI", "href": "#settings/ai"}
    if setup["profile"] != "approved":
        return {"label": "Approve your profile", "href": "#settings/profile"}
    return {"label": "See what works", "href": "#working"}


@router.get("/api/home")
def home(user=Depends(security.current_user)):
    from . import api_reels
    from .api_auth import setup_state

    j = db.jobs
    with db.tx(immediate=False) as conn:
        setup = setup_state(conn, user)
        rows = conn.execute(db.scoped(conn, j, user.id).where(j.c.state.in_(db.ACTIVE_JOB_STATES))
                            .order_by(j.c.created_at.desc()).limit(50)).all()
    out = {"setup": setup, "jobs": [jobs.to_api(r) for r in rows], "new_outliers": [], "cards": [],
           "first_look": [], "formats": [], "next_step": _next_step(setup)}
    parts = getattr(api_reels, "home_parts", None)
    if parts is not None:
        try:
            out.update(parts(user.id) or {})
        except ApiError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("home_parts failed")
    return out

