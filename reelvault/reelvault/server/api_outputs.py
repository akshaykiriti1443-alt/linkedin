"""/api/outputs/* (DESIGN-SAAS.md 4.10). WP5.

POST /api/outputs · POST /api/outputs/quick · GET /api/outputs · GET/PATCH/DELETE /api/outputs/<id>
POST /api/outputs/<id>/redo · GET /api/outputs/<id>/download?format=md|txt|csv|print
Also registers the admin support mirror "outputs" (read-only, only while the member's support grant is live).
"""

from __future__ import annotations

import base64
import json
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from . import db, jobs, outputs, security, work
from .errors import ApiError, bad_input, not_found, read_body

router = APIRouter()


async def _json(request: Request, limit: int = 512 * 1024) -> dict:
    raw = await read_body(request, limit)
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise bad_input("The body must be JSON.") from None
    if not isinstance(body, dict):
        raise bad_input("The body must be a JSON object.")
    return body


def _err(e: work.NotReady) -> ApiError:
    return ApiError(e.status, e.code, e.message)


def _job(c, job_id: str | None):
    if not job_id:
        return None
    return c.execute(select(db.jobs).where(db.jobs.c.id == job_id)).first()


def _created(user_id: str, out_id: str, job_id: str) -> dict:
    with db.read() as c:
        row = db.get_owned(c, db.outputs, user_id, out_id)
        job = _job(c, job_id)
    res = {"output": outputs.summary(row, job), "job": jobs.to_api(job) if job is not None else None}
    if job is not None and job.executor == "mcp":
        res["claude_prompt"] = f"Write my ReelVault output {out_id}"
    return res


@router.post("/api/outputs", status_code=201)
async def create(request: Request, user=Depends(security.current_user)):
    body = await _json(request)

    def run():
        kind = body.get("kind")
        opts = body.get("options") if isinstance(body.get("options"), dict) else {}
        try:
            oid, jid = work.create_output(
                user.id, kind, opts, purpose=body.get("purpose"), brand_id=body.get("brand_id") or "me"
            )
        except work.NotReady as e:
            raise _err(e) from None
        return _created(user.id, oid, jid)

    return await run_in_threadpool(run)


@router.post("/api/outputs/quick", status_code=201)
async def quick(request: Request, user=Depends(security.current_user)):
    body = await _json(request)

    def run():
        refs = {k: body.get(k) for k in ("reel_id", "format_id", "vf", "report_id") if isinstance(body.get(k), str)}
        try:
            oid, jid = work.quick_output(user.id, body.get("action") or "", **refs)
        except work.NotReady as e:
            raise _err(e) from None
        return _created(user.id, oid, jid)

    return await run_in_threadpool(run)


def list_outputs(user_id: str, query: dict) -> dict:
    o = db.outputs
    try:
        limit = min(200, max(1, int(query.get("limit") or 50)))
    except ValueError:
        raise bad_input("limit must be a number.") from None
    q = select(o).where(o.c.user_id == user_id)
    kind = query.get("kind")
    if kind:
        if kind not in outputs.KINDS:
            raise bad_input("Unknown kind.")
        q = q.where(o.c.kind == kind)
    purpose = query.get("purpose")
    if purpose:
        q = q.where(o.c.purpose == purpose)
    if query.get("starred") in ("1", "true"):
        q = q.where(o.c.starred.is_(True))
    cur = query.get("cursor")
    if cur:
        try:
            ts, oid = base64.urlsafe_b64decode(cur.encode()).decode().split("|", 1)
        except Exception:  # noqa: BLE001
            raise bad_input("That page link is not right.") from None
        q = q.where((o.c.created_at < ts) | ((o.c.created_at == ts) & (o.c.id < oid)))
    with db.read() as c:
        rows = c.execute(q.order_by(o.c.created_at.desc(), o.c.id.desc()).limit(limit + 1)).all()
        jobs_by = {
            j.id: j for j in c.execute(select(db.jobs).where(db.jobs.c.id.in_([r.job_id for r in rows if r.job_id])))
        }
    nxt = None
    if len(rows) > limit:
        last = rows[limit - 1]
        nxt = base64.urlsafe_b64encode(f"{last.created_at.isoformat()}|{last.id}".encode()).decode()
        rows = rows[:limit]
    return {"outputs": [outputs.summary(r, jobs_by.get(r.job_id)) for r in rows], "next_cursor": nxt}


@router.get("/api/outputs")
def get_list(request: Request, user=Depends(security.current_user)):
    return list_outputs(user.id, dict(request.query_params))


def _full(user_id: str, out_id: str) -> dict:
    with db.read() as c:
        row = db.get_owned(c, db.outputs, user_id, out_id)
        if row is None:
            raise not_found()
        job = _job(c, row.job_id)
        nums = outputs.render_numbers(c, user_id, row, row.data or {}) if row.data else {}
    out = outputs.summary(row, job)
    inputs = dict(row.inputs or {})
    out.update(
        {
            "options": row.options,
            "inputs": inputs,
            "data": row.data,
            "markdown": row.markdown,
            "made_by": row.made_by,
            "numbers": nums,
        }
    )
    if job is not None and job.executor == "mcp" and row.status != "done":
        out["claude_prompt"] = f"Write my ReelVault output {row.id}"
    return out


@router.get("/api/outputs/{out_id}")
def get_one(out_id: str, user=Depends(security.current_user)):
    return {"output": _full(user.id, out_id)}


@router.patch("/api/outputs/{out_id}")
async def patch(out_id: str, request: Request, user=Depends(security.current_user)):
    def own():
        with db.read() as c:
            if db.get_owned(c, db.outputs, user.id, out_id) is None:
                raise not_found()

    await run_in_threadpool(own)
    body = await _json(request, 2 * 1024 * 1024)

    def run():
        vals = {}
        if "title" in body:
            t = body.get("title")
            if not isinstance(t, str) or not t.strip() or len(t) > 200:
                raise bad_input("Give it a title of 200 characters or less.")
            vals["title"] = re.sub(r"[\x00-\x1f]", " ", t).strip()
        if "starred" in body:
            vals["starred"] = bool(body.get("starred"))
        if "data" in body:
            if not isinstance(body["data"], dict):
                raise bad_input("data must be an object.")
            try:
                outputs.edit(user.id, out_id, body["data"])
            except work.NotReady as e:
                raise _err(e) from None
        if vals:
            vals["updated_at"] = db.utcnow()
            with db.tx() as c:
                c.execute(
                    db.outputs.update().where(db.outputs.c.id == out_id, db.outputs.c.user_id == user.id).values(**vals)
                )
        return {"output": _full(user.id, out_id)}

    return await run_in_threadpool(run)


@router.post("/api/outputs/{out_id}/redo", status_code=201)
async def redo(out_id: str, request: Request, user=Depends(security.current_user)):
    def own():
        with db.read() as c:
            if db.get_owned(c, db.outputs, user.id, out_id) is None:
                raise not_found()

    await run_in_threadpool(own)
    body = await _json(request)

    def run():
        notes = body.get("notes") if isinstance(body.get("notes"), str) else None
        try:
            oid, jid = outputs.redo(user.id, out_id, notes)
        except work.NotReady as e:
            raise _err(e) from None
        return _created(user.id, oid, jid)

    return await run_in_threadpool(run)


@router.delete("/api/outputs/{out_id}")
def delete(out_id: str, user=Depends(security.current_user)):
    with db.tx() as c:
        row = db.get_owned(c, db.outputs, user.id, out_id)
        if row is None:
            raise not_found()
        if row.job_id:
            c.execute(
                db.jobs.update()
                .where(
                    db.jobs.c.id == row.job_id, db.jobs.c.user_id == user.id, db.jobs.c.state.in_(db.ACTIVE_JOB_STATES)
                )
                .values(
                    state="canceled",
                    finished_at=db.utcnow(),
                    lease_owner=None,
                    lease_until=None,
                    dedupe_done=db.jobs.c.dedupe_key,
                    dedupe_key=None,
                )
            )
        db.delete_owned(c, db.outputs, user.id, out_id)
    return {"ok": True}


def _filename(row, ext: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", row.title or row.kind).strip("-")[:60] or row.kind
    return f"{base}.{ext}"


@router.get("/api/outputs/{out_id}/download")
def download(out_id: str, request: Request, user=Depends(security.current_user)):
    fmt = request.query_params.get("format") or "md"
    if fmt not in ("md", "txt", "csv", "print"):
        raise bad_input("format must be md, txt, csv or print.")
    with db.read() as c:
        row = db.get_owned(c, db.outputs, user.id, out_id)
        if row is None:
            raise not_found()
        if row.status != "done" or not row.data:
            raise ApiError(409, "busy", "This output is not finished yet.")
        nums = outputs.render_numbers(c, user.id, row, row.data)
    md = row.markdown or outputs.render(row.kind, row.data, nums)
    if fmt == "print":
        return HTMLResponse(
            outputs.render_print(row.title, md),
            headers={"Content-Security-Policy": outputs.print_csp(), "Cache-Control": "no-store"},
        )
    if fmt == "md":
        body, mime = md, "text/markdown; charset=utf-8"
    elif fmt == "txt":
        body, mime = outputs.render_text(row.kind, row.data, nums), "text/plain; charset=utf-8"
    else:
        body, mime = outputs.render_csv(row.kind, row.data, nums), "text/csv; charset=utf-8"
    return Response(
        content=body.encode("utf-8"),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{_filename(row, fmt)}"', "Cache-Control": "no-store"},
    )


def _support_outputs(user_id: str, query: dict) -> dict:
    """GET /api/admin/support/<uid>/outputs[/<id>] (read-only, audited by WP1's route)."""
    return list_outputs(user_id, query)


try:
    from .api_admin import register_support_mirror

    register_support_mirror("outputs", _support_outputs)
except Exception:  # noqa: BLE001 - admin module is optional in a partial install
    pass
