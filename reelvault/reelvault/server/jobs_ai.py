"""AI job handlers for members on an API key (DESIGN-SAAS.md 3.13.1, 7.1.4, 7.2). WP5.

tag_reels, retag_topics, analyze_reel, name_formats, make_output, draft_profile, suggest_topics run here when the
job's executor is the server (ai_mode=api, or a tagging connection for bulk tags). Each handler:

    build_task (work.py) -> ai.structured(get_provider(conn), ...) -> save_result (work.py)

save_result runs as the structured() cross-check, so a failed check (a wrong pain, an unknown card, a reel id that
isn't in the batch) goes back to the AI once with the message, exactly like a schema error.
Errors -> job states: BadKey -> connection bad_key + job paused; QuotaExceeded -> paused; RateLimited -> retried
later; Refused / Truncated / twice-invalid -> failed with a plain message. At most 2 AI calls run at a time per
member (per worker process).
MCP and desktop jobs are NOT run here: they wait for the member's Claude (WP6 tools) or desktop app (api_ai.py).
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import select

from .. import ai
from .. import schema_members as S
from ..ai import base as aib
from . import db, jobs, security, work
from .jobs import Fail, Pause, Retry, handler, on_lease_expired

log = logging.getLogger("reelvault.jobs_ai")

PER_MEMBER = 2
_slots: dict[str, threading.BoundedSemaphore] = {}
_slots_lock = threading.Lock()


def _slot(user_id: str) -> threading.BoundedSemaphore:
    with _slots_lock:
        return _slots.setdefault(user_id, threading.BoundedSemaphore(PER_MEMBER))


def _set_conn_status(conn_id: str, status: str, message: str, secret: str | None) -> None:
    with db.tx() as c:
        c.execute(
            db.ai_connections.update()
            .where(db.ai_connections.c.id == conn_id)
            .values(
                status=status,
                last_error=security.redact(message, [secret] if secret else [])[:500],
                last_tested_at=db.utcnow(),
            )
        )


def _provider(user_id: str, kind: str):
    with db.read() as c:
        row = work.connection_for(c, user_id, kind)
    if row is None:
        raise Pause(work.NO_AI_MSG, code="no_ai")
    if row.status in ("bad_key", "quota") and row.last_error:
        raise Pause(row.last_error, code=row.status)
    try:
        return row, ai.get_provider(row, task=kind)
    except aib.BadKey as e:
        _set_conn_status(row.id, "bad_key", e.message, None)
        raise Pause(e.message, code="bad_key") from None
    except aib.AIError as e:
        raise Fail(e.message, code="ai_error") from None


def _map(e: aib.AIError, row, p) -> Exception:
    secret = getattr(p, "secret", None)
    if isinstance(e, aib.BadKey):
        _set_conn_status(row.id, "bad_key", e.message, secret)
        return Pause(e.message, code="bad_key")
    if isinstance(e, aib.QuotaExceeded):
        _set_conn_status(row.id, "quota", e.message, secret)
        return Pause(e.message, code="quota")
    if isinstance(e, aib.RateLimited):
        return Retry(max(15.0, min(3600.0, e.retry_after or 60.0)), e.message)
    if isinstance(e, aib.Refused):
        return Fail(e.message, code="refused")
    if isinstance(e, aib.Truncated):
        return Fail(
            "The AI's answer was too long twice. Try again with fewer reels or a shorter request.", code="truncated"
        )
    if e.message == "The AI's answer didn't fit the format twice.":
        log.warning(
            "AI answer invalid twice (%s, %s): %s",
            row.provider,
            getattr(p, "model", "?"),
            security.redact(e.detail or "", [secret] if secret else [])[:1500],
        )
        return Fail(
            "The AI's answer didn't fit the format twice. Press Retry, or pick a stronger model in Settings > AI.",
            code="invalid_output",
        )
    return Retry(120, e.message)


def _bump(row, usage: aib.Usage) -> None:
    if usage is None:
        return
    db.bump(
        db.ai_usage,
        {"conn_id": row.id, "month": db.utcnow().strftime("%Y-%m")},
        _set={"user_id": row.user_id},
        calls=1,
        tokens_in=int(usage.input or 0),
        tokens_out=int(usage.output or 0),
    )


def call_and_save(user_id: str, job, p, row) -> dict:
    """One AI call for one job (or one chunk) and its save. Returns save_result's dict."""
    task = work.build_task(user_id, job, provider=p)
    images = work.task_images(user_id, job, task["image_dest"], max_images=p.max_images) if p.vision else []
    if job.kind == "make_output":
        images = []
    model = S.model_for(task["schema_name"])
    made_by = {"via": "api", "provider": row.provider, "model": p.model}
    out: dict = {}

    def check(obj):
        out["res"] = work.save_result(user_id, job, obj.model_dump(), made_by, finish=False, vision=p.vision)

    with _slot(user_id):
        try:
            _, usage = ai.structured(
                p,
                task["system"],
                [task["prompt"], *images],
                model,
                task["effort"],
                max_output_tokens=task["max_output_tokens"],
                check=check,
            )
        except aib.AIError as e:
            _bump(row, getattr(e, "usage", None))
            raise _map(e, row, p) from None
    _bump(row, usage)
    if row.status != "ok":
        with db.tx() as c:
            c.execute(
                db.ai_connections.update().where(db.ai_connections.c.id == row.id).values(status="ok", last_error=None)
            )
    return out["res"]


def _final(job) -> bool:
    return job.attempts >= job.max_attempts


# ============================================================================================== handlers


@handler("tag_reels", job_class="ai")
def tag_reels(job, ctx):
    uid = job.user_id
    try:
        row, p = _provider(uid, "tag_reels")
    except Pause:
        with db.tx() as c:
            work.release_tag_batch(c, job)
        raise
    dest = work.dest_for(p)
    n = min(work.API_TAG_BATCH, p.max_images) if p.vision else work.API_TAG_BATCH
    with db.tx() as c:
        cur = c.execute(select(db.jobs).where(db.jobs.c.id == job.id)).first()
        ids = work.lease_tag_batch(c, uid, cur, dest, n=n)
        if not ids:
            work.release_dedupe(job.id, conn=c)
            work.ensure_tag_slot(uid, conn=c)
            return {"tagged": 0}
    job = jobs.get(job.id)
    ctx.progress(0, len(ids), f"Tagging {len(ids)} reels")
    try:
        res = call_and_save(uid, job, p, row)
    except Pause:
        with db.tx() as c:
            work.release_tag_batch(c, job)
        raise
    except Retry:
        if _final(job):
            with db.tx() as c:
                work.release_tag_batch(c, job, state="failed")
                work.release_dedupe(job.id, conn=c)
                work.ensure_tag_slot(uid, conn=c)
        raise
    except Fail:
        with db.tx() as c:
            work.release_tag_batch(c, job, state="failed")
            work.release_dedupe(job.id, conn=c)
            work.ensure_tag_slot(uid, conn=c)
        raise
    except Exception:
        if _final(job):
            with db.tx() as c:
                work.release_tag_batch(c, job, state="failed")
        raise
    return {"tagged": res.get("tagged", 0)}


@handler("retag_topics", job_class="ai")
def retag_topics(job, ctx):
    uid = job.user_id
    row, p = _provider(uid, "retag_topics")
    with db.tx() as c:
        cur = c.execute(select(db.jobs).where(db.jobs.c.id == job.id)).first()
        ids = work.lease_retag_batch(c, uid, cur)
    if not ids:
        return {"retagged": 0}
    job = jobs.get(job.id)
    res = call_and_save(uid, job, p, row)
    return {"retagged": res.get("retagged", 0)}


def _deep_failed(user_id: str, reel_id: str | None) -> None:
    if not reel_id:
        return
    with db.tx() as c:
        c.execute(
            db.reels.update().where(db.reels.c.id == reel_id, db.reels.c.user_id == user_id).values(deep_state="failed")
        )
    work.unblock_outputs(user_id, reel_id)


@handler("analyze_reel", job_class="ai")
def analyze_reel(job, ctx):
    uid = job.user_id
    rid = (job.payload or {}).get("reel_id")
    row, p = _provider(uid, "analyze_reel")
    ctx.progress(0, 1, "Reading the reel")
    try:
        call_and_save(uid, job, p, row)
    except Fail:
        _deep_failed(uid, rid)
        raise
    except Pause:
        raise
    except Exception:  # Retry or a crash: only the last attempt gives up on the reel
        if _final(job):
            _deep_failed(uid, rid)
        raise
    return {"ok": True}


@handler("name_formats", job_class="ai")
def name_formats(job, ctx):
    row, p = _provider(job.user_id, "name_formats")
    res = call_and_save(job.user_id, job, p, row)
    return {"named": res.get("named", 0)}


@handler("draft_profile", job_class="ai")
def draft_profile(job, ctx):
    uid = job.user_id
    row, p = _provider(uid, "draft_profile")
    ctx.progress(0, 1, "Reading your links")
    try:
        call_and_save(uid, job, p, row)
    except (Fail, Retry) as e:
        if isinstance(e, Fail) or _final(job):
            _reset_drafting(uid)
        raise
    return {"ok": True}


def _reset_drafting(user_id: str) -> None:
    p = db.profiles
    with db.tx() as c:
        prof = work.profile_row(c, user_id)
        if prof is not None and prof.status == "drafting":
            c.execute(
                p.update()
                .where(p.c.user_id == user_id, p.c.brand_id == "me")
                .values(status="draft" if any(k.startswith("q") for k in (prof.answers or {})) else "empty")
            )


@handler("suggest_topics", job_class="ai")
def suggest_topics(job, ctx):
    row, p = _provider(job.user_id, "suggest_topics")
    res = call_and_save(job.user_id, job, p, row)
    return {"count": res.get("count", 0)}


def _output_failed(user_id: str, job) -> None:
    oid = (job.payload or {}).get("output_id")
    if oid:
        with db.tx() as c:
            c.execute(
                db.outputs.update()
                .where(db.outputs.c.id == oid, db.outputs.c.user_id == user_id)
                .values(status="failed", updated_at=db.utcnow())
            )


@handler("make_output", job_class="ai")
def make_output(job, ctx):
    uid = job.user_id
    oid = (job.payload or {}).get("output_id")
    row, p = _provider(uid, "make_output")
    with db.tx() as c:
        c.execute(
            db.outputs.update()
            .where(db.outputs.c.id == oid, db.outputs.c.user_id == uid, db.outputs.c.status != "done")
            .values(status="working")
        )
    res: dict = {}
    for _ in range(12):
        job = jobs.get(job.id)
        try:
            res = call_and_save(uid, job, p, row)
        except Fail:
            _output_failed(uid, job)
            raise
        except work.Invalid as e:
            _output_failed(uid, job)
            raise Fail(e.message, code="invalid_output") from None
        except (Retry, Pause):
            raise
        except Exception:
            if _final(job):
                _output_failed(uid, job)
            raise
        ch = res.get("chunk") or {}
        ctx.progress(int(ch.get("index", 0)) + 1, int(ch.get("of", 1)), "Writing")
        if res.get("next_chunk") is None:
            break
        ctx.renew()
    return {"ok": True, "output_id": oid}


# ============================================================================================== lease expiry


@on_lease_expired("tag_reels")
def _tag_expired(conn, job):
    """An expired tag lease (server, MCP or desktop) puts its reels back in the queue (3.13.2)."""
    work.release_tag_batch(conn, job)
    payload = dict(job.payload or {})
    payload.pop("reel_ids", None)
    conn.execute(db.jobs.update().where(db.jobs.c.id == job.id).values(payload=payload))


@on_lease_expired("retag_topics")
def _retag_expired(conn, job):
    payload = dict(job.payload or {})
    payload.pop("reel_ids", None)
    conn.execute(db.jobs.update().where(db.jobs.c.id == job.id).values(payload=payload))
