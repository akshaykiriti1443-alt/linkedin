"""Outputs: reel script packs, Meta ads, 30-day plans and competitor reports (DESIGN-SAAS.md 8). WP5.

    create(user_id, kind, options, *, purpose=None, brand_id="me") -> (output_id, job_id)
    quick(user_id, action, *, reel_id=None, format_id=None, vf=None, report_id=None) -> (output_id, job_id)   (8.0)
    unblock(user_id, reel_id) -> int                  outputs waiting for a source reel's deep read
    build_brief(user_id, job_row, *, provider_name) -> AiTask for the output's next chunk
    save_chunk(user_id, job_row, output, made_by, *, usage=None) -> {"ok", "output_id", "url", "next_chunk"}
    report_numbers(user_id, account_ids) -> dict      numbers for the competitor report (WP4, with a fallback)
    render(kind, data, numbers) -> markdown ; render_text / render_csv / render_print(...)

Chunks (one AI call each, so free models with small output caps work): reel_script = one script per call, meta_ads
= one ad set per call, content_plan = pillars + week 1, then one week per call (5 chunks), competitor_report = 1.
The numbers in a report are never written by the AI.
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select

from .. import schema_members as S
from .. import taxonomy
from ..ai import untrusted
from . import db, ratelimit
from .work import Invalid, NotReady

log = logging.getLogger("reelvault.outputs")

KINDS = ("reel_script", "meta_ads", "content_plan", "competitor_report")
KIND_LABELS = {
    "reel_script": "Reel scripts",
    "meta_ads": "Meta ads",
    "content_plan": "30-day plan",
    "competitor_report": "Competitor report",
}
PURPOSES = ("remake", "format", "hook", "learn")
OUTLIER_BANDS = ("outlier", "big", "breakout")
SAVES_LINE = (
    "Instagram hides saves, and usually shares, for accounts you don't own. We rank on views, likes and comments."
)
WAITING_DEEP = "Waiting for the breakdown of the source reel."
PLAN_DAYS = 30


# ============================================================================================== request


class BasedOn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reel_ids: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    sub_formats: list[str] = Field(default_factory=list)
    hooks_from: list[str] = Field(default_factory=list)
    auto: bool = True


class OutputOptions(BaseModel):
    """OutputRequest.options (4.10). Member text is capped; unknown keys are ignored."""

    model_config = ConfigDict(extra="ignore")
    count: int = 3
    based_on: BasedOn = Field(default_factory=BasedOn)
    pain: str | None = None
    topic: str | None = None
    goal: str | None = None
    length: str | None = None
    offer: str | None = None
    start_date: str | None = None
    posts_per_week: int | None = None
    include_other_types: bool = False
    accounts: list[str] | None = None
    notes: str | None = None
    redo_of: str | None = None

    @field_validator("topic")
    @classmethod
    def _topic(cls, v):
        if v and len(v) > 500:
            raise ValueError("Keep the topic under 500 characters.")
        return v

    @field_validator("notes")
    @classmethod
    def _notes(cls, v):
        if v and len(v) > 1000:
            raise ValueError("Keep the notes under 1,000 characters.")
        return v

    @field_validator("goal")
    @classmethod
    def _goal(cls, v):
        if v and v not in ("leads", "followers", "sales", "community"):
            raise ValueError("goal must be leads, followers, sales or community.")
        return v

    @field_validator("length")
    @classmethod
    def _length(cls, v):
        if v and str(v) not in ("15", "30", "45", "60"):
            raise ValueError("length must be 15, 30, 45 or 60 seconds.")
        return str(v) if v else v

    @field_validator("start_date")
    @classmethod
    def _date(cls, v):
        if v:
            date.fromisoformat(v)
        return v


def parse_options(kind: str, options: dict | None) -> OutputOptions:
    try:
        o = OutputOptions.model_validate(options or {})
    except ValidationError as e:
        first = e.errors()[0]
        raise NotReady(str(first.get("msg", "Check the form.")).removeprefix("Value error, ")) from None
    if kind == "reel_script":
        o.count = min(5, max(1, o.count))
    elif kind == "meta_ads":
        o.count = min(3, max(1, o.count if (options or {}).get("count") else 1))
    else:
        o.count = 1
    if o.posts_per_week is not None:
        o.posts_per_week = min(7, max(1, int(o.posts_per_week)))
    return o


# ============================================================================================== reads


def _out(conn, user_id: str, out_id: str):
    return db.get_owned(conn, db.outputs, user_id, out_id)


def _deep(conn, user_id: str, reel_ids: list[str]) -> dict:
    a = db.analyses
    return {
        r.reel_id: dict(r.data or {})
        for r in conn.execute(
            select(a.c.reel_id, a.c.data).where(
                a.c.user_id == user_id, a.c.level == "deep", a.c.reel_id.in_(list(reel_ids))
            )
        )
    }


def _quick(conn, user_id: str, reel_ids: list[str]) -> dict:
    a = db.analyses
    return {
        r.reel_id: dict(r.data or {})
        for r in conn.execute(
            select(a.c.reel_id, a.c.data).where(
                a.c.user_id == user_id, a.c.level == "quick", a.c.reel_id.in_(list(reel_ids))
            )
        )
    }


def _snapshot(conn, user_id: str):
    s = db.insight_snapshots
    return conn.execute(select(s).where(s.c.user_id == user_id).order_by(s.c.computed_at.desc()).limit(1)).first()


def _can_make(vf: str, caps: dict | None) -> dict:
    from .. import insights

    try:
        return insights.can_make(vf, caps)
    except NotImplementedError:
        need = [n for n in taxonomy.needs(vf) if caps and caps.get(n) is False]
        return {
            "ok": not need,
            "reason": ("Needs " + ", ".join(taxonomy.need_label(n) for n in need) + ".") if need else None,
        }


def _member_reels(conn, user_id: str, ids: list[str]) -> list[str]:
    r = db.reels
    have = {x[0] for x in conn.execute(select(r.c.id).where(r.c.user_id == user_id, r.c.id.in_(list(ids))))}
    return [i for i in dict.fromkeys(ids) if i in have]


def _best_reels(
    conn, user_id: str, *, vf: str | None = None, n: int = 5, deep_first: bool = True, caps: dict | None = None
) -> list[str]:
    """Top competitor outliers (deep reads first, can-make first)."""
    r, a, acc = db.reels, db.analyses, db.accounts
    q = (
        select(r.c.id, r.c.score, a.c.level, a.c.visual_format)
        .select_from(
            r.join(a, (a.c.reel_id == r.c.id) & (a.c.user_id == r.c.user_id), isouter=True).join(
                acc, acc.c.id == r.c.account_id, isouter=True
            )
        )
        .where(r.c.user_id == user_id, (acc.c.role.is_(None)) | (acc.c.role == "competitor"), r.c.score.is_not(None))
    )
    if vf:
        q = q.where(a.c.visual_format == vf)
    rows = conn.execute(q.order_by(r.c.score.desc().nulls_last()).limit(200)).all()
    best: dict[str, tuple] = {}
    for row in rows:
        key = best.get(row.id)
        deep = row.level == "deep"
        ok = _can_make(row.visual_format, caps)["ok"] if caps else True
        rank = (0 if ok else 1, 0 if (deep or not deep_first) else 1, -(row.score or 0))
        if key is None or rank < key:
            best[row.id] = rank
    return [rid for rid, _ in sorted(best.items(), key=lambda kv: kv[1])][:n]


def _format_examples(conn, user_id: str, fmt_id: str) -> list[str]:
    f = db.get_owned(conn, db.formats, user_id, fmt_id)
    return list(f.example_reels or [])[:3] if f is not None else []


def _profile_ok(conn, user_id: str):
    from .work import profile_row

    p = profile_row(conn, user_id)
    if p is None or p.status != "approved" or not p.digest:
        raise NotReady("Approve your profile first.")
    return p


def default_posts_per_week(d: dict | None) -> int:
    caps = (d or {}).get("capabilities") or {}
    try:
        h = float(caps.get("hours_per_week") or 0)
    except (TypeError, ValueError):
        h = 0
    n = 2 if h < 3 else 3 if h <= 6 else 5
    if caps.get("editor"):
        n += 1
    return min(7, n)


def _title(kind: str, o: OutputOptions, conn, user_id: str, sources: list[str]) -> str:
    if kind == "reel_script":
        about = o.topic or ""
        if not about and o.based_on.formats:
            about = taxonomy.label(o.based_on.formats[0])
        if not about and o.based_on.sub_formats:
            f = db.get_owned(conn, db.formats, user_id, o.based_on.sub_formats[0])
            about = (f.name or taxonomy.label(f.vf)) if f is not None else ""
        n = o.count
        head = f"{n} reel script{'s' if n != 1 else ''}"
        return (f"{head}: {about}" if about else head)[:120]
    if kind == "meta_ads":
        return (f"Meta ads: {o.offer}" if o.offer else "Meta ads for your offer")[:120]
    if kind == "content_plan":
        return f"30-day plan from {o.start_date}" if o.start_date else "30-day content plan"
    return "Competitor report"


# ============================================================================================== create


def _sources_for(conn, user_id: str, kind: str, o: OutputOptions, d: dict | None) -> list[str]:
    caps = (d or {}).get("capabilities")
    b = o.based_on
    ids = _member_reels(conn, user_id, list(b.reel_ids) + list(b.hooks_from))
    for fid in b.sub_formats:
        ids += [i for i in _member_reels(conn, user_id, _format_examples(conn, user_id, fid)) if i not in ids]
    for vf in b.formats:
        ids += [i for i in _best_reels(conn, user_id, vf=vf, n=3, caps=None) if i not in ids]
    if not ids and kind in ("reel_script", "meta_ads") and b.auto:
        ids = _best_reels(conn, user_id, n=max(3, o.count), caps=caps)
    return ids[:10]


def create(
    user_id: str, kind: str, options: dict | None, *, purpose: str | None = None, brand_id: str = "me"
) -> tuple[str, str]:
    from .work import enqueue_ai, queue_deep

    if kind not in KINDS:
        raise NotReady("Pick one of: reel scripts, Meta ads, 30-day plan, competitor report.")
    if brand_id != "me":
        raise NotReady("Only 'Me' is available for now.")
    if purpose is not None and purpose not in PURPOSES:
        raise NotReady("purpose must be remake, format, hook or learn.")
    o = parse_options(kind, options)
    with db.read() as c:
        prof = _profile_ok(c, user_id)
        if kind == "reel_script" and not _sources_for(c, user_id, kind, o, dict(prof.digest or {})):
            raise NotReady("Pull a competitor's reels first. Scripts build on reels that already work.")
    limit = int(db.get_setting("outputs_per_day") or 40)
    ratelimit.hit(
        user_id,
        "outputs",
        limit,
        "day",
        code="limit_reached",
        message=f"You've made {limit} outputs today. You can make more tomorrow.",
    )
    numbers = None
    if kind == "competitor_report":
        numbers = report_numbers(user_id, o.accounts)
        if not numbers.get("accounts"):
            raise NotReady("Add a competitor and pull their reels first.")
    need_deep: list[str] = []
    with db.tx() as c:
        d = dict(prof.digest or {})
        sources = _sources_for(c, user_id, kind, o, d)
        if kind in ("reel_script",) and not sources:
            raise NotReady("Pull a competitor's reels first. Scripts build on reels that already work.")
        if o.start_date is None and kind == "content_plan":
            o.start_date = (db.utcnow().date() + timedelta(days=(7 - db.utcnow().weekday()) % 7 or 7)).isoformat()
        if kind == "content_plan" and not o.posts_per_week:
            o.posts_per_week = default_posts_per_week(d)
        explicit = _member_reels(c, user_id, list(o.based_on.reel_ids) + list(o.based_on.hooks_from))
        deep_have = _deep(c, user_id, explicit)
        r = db.reels
        states = {x.id: x.deep_state for x in c.execute(select(r.c.id, r.c.deep_state).where(r.c.id.in_(explicit)))}
        need_deep = [i for i in explicit if i not in deep_have and states.get(i) != "failed"]
        snap = _snapshot(c, user_id)
        chunks = {"reel_script": o.count, "meta_ads": o.count, "content_plan": 5, "competitor_report": 1}[kind]
        inputs = {
            "reel_ids": sources,
            "snapshot_id": snap.id if snap is not None else None,
            "profile_version": prof.version,
            "chunks": chunks,
            "chunks_done": 0,
        }
        if numbers is not None:
            inputs["numbers"] = numbers
        if need_deep:
            inputs["waiting_on"] = need_deep
        out_id = db.new_id("out")
        now = db.utcnow()
        c.execute(
            db.outputs.insert().values(
                id=out_id,
                user_id=user_id,
                brand_id=brand_id,
                kind=kind,
                purpose=purpose,
                title=_title(kind, o, c, user_id, sources),
                status="queued",
                options=o.model_dump(),
                inputs=inputs,
                data=None,
                schema_version=S.SCHEMA_VERSION,
                made_by={},
                created_at=now,
                updated_at=now,
            )
        )
        if need_deep:
            jid = enqueue_ai(
                user_id,
                "make_output",
                {"output_id": out_id},
                priority=10,
                dedupe_key=f"make_output:{out_id}",
                conn=c,
                state="paused",
                message=WAITING_DEEP,
                error_code="waiting_deep",
            )
        else:
            jid = enqueue_ai(
                user_id, "make_output", {"output_id": out_id}, priority=10, dedupe_key=f"make_output:{out_id}", conn=c
            )
        row = c.execute(select(db.jobs.c.executor, db.jobs.c.error_code).where(db.jobs.c.id == jid)).first()
        status = "waiting" if row.executor in ("mcp", "desktop") or row.error_code == "waiting_deep" else "queued"
        c.execute(db.outputs.update().where(db.outputs.c.id == out_id).values(status=status, job_id=jid))
    if need_deep:
        queue_deep(user_id, need_deep)
        unblock(user_id, need_deep[0])  # deep reads may already be done (or failed) by now
    return out_id, jid


def quick(
    user_id: str,
    action: str,
    *,
    reel_id: str | None = None,
    format_id: str | None = None,
    vf: str | None = None,
    report_id: str | None = None,
) -> tuple[str, str]:
    """8.0: Remake this · Use this format · Borrow this hook · Start these 3 scripts."""
    with db.read() as c:
        if action in ("remake", "borrow_hook"):
            reel = db.get_owned(c, db.reels, user_id, reel_id or "") if reel_id else None
            if reel is None:
                raise NotReady("That reel is not in your ReelVault.", "not_found", 404)
            a = db.analyses
            fmt = c.execute(select(a.c.format_id).where(a.c.reel_id == reel.id, a.c.format_id.is_not(None))).first()
            if action == "remake":
                opts = {
                    "count": 1,
                    "based_on": {"reel_ids": [reel.id], "sub_formats": [fmt[0]] if fmt else [], "auto": False},
                }
                return create(user_id, "reel_script", opts, purpose="remake")
            opts = {"count": 3, "based_on": {"hooks_from": [reel.id], "auto": False}}
            return create(user_id, "reel_script", opts, purpose="hook")
        if action == "use_format":
            if format_id:
                if db.get_owned(c, db.formats, user_id, format_id) is None:
                    raise NotReady("That format is not in your ReelVault.", "not_found", 404)
                opts = {"count": 3, "based_on": {"sub_formats": [format_id], "auto": False}}
            elif vf and vf in taxonomy.VF_IDS:
                opts = {"count": 3, "based_on": {"formats": [vf], "auto": False}}
            else:
                raise NotReady("Pick a format first.")
            return create(user_id, "reel_script", opts, purpose="format")
        if action == "start_top3":
            rep = db.get_owned(c, db.outputs, user_id, report_id or "") if report_id else None
            if rep is None or rep.kind != "competitor_report" or not rep.data:
                raise NotReady("Open a finished competitor report first.", "not_found", 404)
            ids = [it.get("reel_id") for it in (rep.data.get("remake_queue") or [])][:3]
            ids = _member_reels(c, user_id, [i for i in ids if i])
            if not ids:
                raise NotReady("This report has no reels to remake.")
            opts = {"count": len(ids), "based_on": {"reel_ids": ids, "auto": False}}
            return create(user_id, "reel_script", opts, purpose="remake")
    raise NotReady("action must be remake, use_format, borrow_hook or start_top3.")


def unblock(user_id: str, reel_id: str) -> int:
    """After a deep read is saved (or fails): outputs whose source reels are all read start their job."""
    from .work import ai_executor

    j, o, r = db.jobs, db.outputs, db.reels
    n = 0
    with db.tx() as c:
        rows = c.execute(
            select(j).where(
                j.c.user_id == user_id,
                j.c.kind == "make_output",
                j.c.state == "paused",
                j.c.error_code == "waiting_deep",
            )
        ).all()
        for job in rows:
            out = _out(c, user_id, (job.payload or {}).get("output_id", ""))
            if out is None:
                continue
            wait = list((out.inputs or {}).get("waiting_on") or [])
            left = [
                x.id
                for x in c.execute(select(r.c.id).where(r.c.id.in_(wait), r.c.deep_state.notin_(("done", "failed"))))
            ]
            if left:
                continue
            ex = ai_executor(user_id, "make_output", conn=c)
            if ex is None:
                c.execute(
                    j.update()
                    .where(j.c.id == job.id)
                    .values(error_code="no_ai", error_message="Choose how ReelVault uses AI in Settings > AI.")
                )
                continue
            c.execute(
                j.update()
                .where(j.c.id == job.id, j.c.state == "paused")
                .values(
                    state="queued" if ex == "server" else "waiting",
                    executor=ex,
                    job_class="ai" if ex == "server" else "external",
                    error_code=None,
                    error_message=None,
                    progress={
                        "done": 0,
                        "total": 0,
                        "message": "Writing"
                        if ex == "server"
                        else ("Waiting for Claude" if ex == "mcp" else "Waiting for the desktop app"),
                    },
                )
            )
            db.upsert(
                c,
                db.job_turns,
                {
                    "user_id": user_id,
                    "job_class": "ai" if ex == "server" else "external",
                    "last_served_at": db.utcnow().replace(year=2000, month=1, day=1),
                },
                do_nothing=True,
            )
            c.execute(
                o.update()
                .where(o.c.id == out.id)
                .values(status="waiting" if ex != "server" else "queued", updated_at=db.utcnow())
            )
            n += 1
    return n


# ============================================================================================== report numbers


def report_numbers(user_id: str, account_ids: list[str] | None) -> dict:
    """Numbers for a competitor report (8.4). The account table, remake candidates and pain coverage come from the
    stored rows; the insight parts (cards, formats, grid, combos, signatures, music, you vs them, data note,
    rising/fading) come from WP4's jobs_score.report_numbers (computed fresh for the chosen accounts) when it
    answers, else from the latest insight snapshot. The AI never writes these."""
    nums = _fallback_numbers(user_id, account_ids)
    try:
        from . import jobs_score

        fresh = jobs_score.report_numbers(user_id, account_ids)
    except NotImplementedError:
        fresh = None
    except Exception:  # noqa: BLE001
        log.exception("jobs_score.report_numbers failed; using the latest snapshot")
        fresh = None
    if isinstance(fresh, dict):
        for k in ("cards", "formats", "grid", "combos", "music", "you_vs_them"):
            if fresh.get(k):
                nums[k] = fresh[k]
        if fresh.get("per_competitor") or fresh.get("signatures"):
            nums["signatures"] = fresh.get("per_competitor") or fresh.get("signatures")
        if isinstance(fresh.get("data_note"), dict):
            nums["data_note"] = {**fresh["data_note"], "saves_line": fresh["data_note"].get("saves_line", SAVES_LINE)}
        if isinstance(fresh.get("rows"), dict):
            nums["recent"] = _recent(fresh["rows"])
        for k in ("remake_candidates", "topic_coverage"):
            if fresh.get(k):
                nums[k] = fresh[k]
        if isinstance(fresh.get("accounts"), list) and fresh["accounts"] and isinstance(fresh["accounts"][0], dict):
            nums["accounts"] = fresh["accounts"]
    return nums


def _recent(rows: dict) -> dict:
    rising, fading = [], []
    for dim, lst in (rows or {}).items():
        for row in lst or []:
            if row.get("trend") == "rising":
                rising.append(dict(row, dimension=dim))
            elif row.get("trend") == "fading":
                fading.append(dict(row, dimension=dim))
    return {
        "window_days": int(db.get_setting("recent_window_days") or 90),
        "rising": rising[:10],
        "fading": fading[:10],
    }


def _thumb(user_id: str, reel) -> str | None:
    from . import storage

    return storage.files_url(user_id, reel.cover_path) if reel.cover_path else None


def _fallback_numbers(user_id: str, account_ids: list[str] | None) -> dict:
    acc, r, a = db.accounts, db.reels, db.analyses
    from .work import digest

    with db.read() as c:
        q = select(acc).where(acc.c.user_id == user_id, acc.c.role == "competitor")
        if account_ids:
            q = q.where(acc.c.id.in_(account_ids))
        accounts = c.execute(q).all()
        ids = [x.id for x in accounts]
        d = digest(c, user_id) or {}
        caps = d.get("capabilities")
        out_accounts = []
        for x in accounts:
            reels = c.execute(select(r).where(r.c.user_id == user_id, r.c.account_id == x.id)).all()
            scored = [y for y in reels if y.score is not None]
            outl = [y for y in scored if y.band in OUTLIER_BANDS]
            best = max(scored, key=lambda y: y.score, default=None)
            out_accounts.append(
                {
                    "handle": x.handle,
                    "followers": x.followers,
                    "reels_scored": len(scored),
                    "normal_views": x.baseline_views,
                    "outliers": len(outl),
                    "best_reel": {
                        "id": best.id,
                        "score": round(best.score, 1),
                        "url": best.url,
                        "thumb": _thumb(user_id, best),
                    }
                    if best
                    else None,
                }
            )
        snap = _snapshot(c, user_id)
        sd = dict(snap.data or {}) if snap is not None else {}
        cutoff = db.utcnow() - timedelta(days=int(db.get_setting("insight_window_days", conn=c) or 365))
        cand_rows = c.execute(
            select(r, a.c.data.label("deep"), a.c.visual_format, a.c.format_id)
            .select_from(r.join(a, (a.c.reel_id == r.c.id) & (a.c.level == "deep")))
            .where(r.c.user_id == user_id, r.c.account_id.in_(ids), r.c.band.in_(OUTLIER_BANDS))
            .order_by(r.c.taken_at.desc().nulls_last())
            .limit(60)
        ).all()
        handles = {x.id: x.handle for x in accounts}
        cands = []
        for y in cand_rows:
            if y.taken_at is not None and y.taken_at < cutoff:
                continue
            cm = _can_make(y.visual_format, caps)
            cands.append(
                {
                    "reel_id": y.id,
                    "handle": handles.get(y.account_id),
                    "url": y.url,
                    "thumb": _thumb(user_id, y),
                    "score": round(y.score or 0, 1),
                    "trend": None,
                    "format_id": y.format_id,
                    "vf": y.visual_format,
                    "can_make": cm,
                    "weaknesses": list((y.deep or {}).get("weaknesses") or []),
                }
            )
        cands.sort(key=lambda z: (0 if z["can_make"]["ok"] else 1, -z["score"]))
        topics = c.execute(
            select(db.topics).where(db.topics.c.user_id == user_id, db.topics.c.status == "active")
        ).all()
        cover = []
        for pain in d.get("top_pains") or []:
            words = {w for w in re.findall(r"[a-z]{4,}", pain.lower())}
            hit = [
                t.slug for t in topics if words & set(re.findall(r"[a-z]{4,}", f"{t.label} {t.description}".lower()))
            ]
            n = 0
            if hit:
                for row in c.execute(select(a.c.topics).where(a.c.user_id == user_id, a.c.level == "quick")):
                    if set(row.topics or []) & set(hit):
                        n += 1
            cover.append({"pain": pain, "covered_by_topics": hit, "competitor_reels": n})
    note = dict(sd.get("data_note") or {})
    note.setdefault("saves_line", SAVES_LINE)
    return {
        "accounts": out_accounts,
        "cards": sd.get("cards") or [],
        "formats": sd.get("formats") or [],
        "grid": sd.get("grid") or {},
        "combos": sd.get("combos") or [],
        "signatures": sd.get("per_competitor") or [],
        "recent": _recent(sd.get("rows") or {}),
        "remake_candidates": cands[:15],
        "topic_coverage": {"pains": cover},
        "music": sd.get("music") or [],
        "you_vs_them": sd.get("you_vs_them"),
        "data_note": note,
    }


def card_values(numbers: dict) -> set[str]:
    """Every '<dimension>:<value>' a report Finding may cite."""
    vals: set[str] = set()
    for card in numbers.get("cards") or []:
        if card.get("dimension") and card.get("value") is not None:
            vals.add(f"{card['dimension']}:{card['value']}")
    for dim, g in (numbers.get("grid") or {}).items():
        if isinstance(g, dict):
            for lst in g.values():
                for v in lst or []:
                    vals.add(f"{dim}:{v}")
    for f in numbers.get("formats") or []:
        if f.get("id"):
            vals.add(f"sub_format:{f['id']}")
    for key in ("rising", "fading"):
        for row in (numbers.get("recent") or {}).get(key) or []:
            if row.get("dimension") and row.get("value") is not None:
                vals.add(f"{row['dimension']}:{row['value']}")
    return vals


# ============================================================================================== briefs


def _insights_text(conn, user_id: str, caps: dict | None, *, limit: int = 6) -> tuple[str, set[str]]:
    """Top WorkingCards and FormatCards (can-make first) as text, plus the card refs an Upgrade may cite."""
    snap = _snapshot(conn, user_id)
    if snap is None:
        return "(no insights yet: rely on the source reels)", set()
    sd = snap.data or {}
    cards = list(sd.get("cards") or [])
    cards.sort(key=lambda x: (0 if (x.get("can_make") or {}).get("ok", True) else 1, -(x.get("lift") or 0)))
    lines, refs = [], set()
    for cd in cards[:limit]:
        ref = f"card:{cd.get('dimension')}:{cd.get('value')}"
        refs.add(ref)
        cm = cd.get("can_make") or {}
        lines.append(
            f"- {ref} · {cd.get('title')} · {cd.get('line')}"
            + ("" if cm.get("ok", True) else f" · member can't make it: {cm.get('reason')}")
        )
    fmts = list(sd.get("formats") or [])[:3]
    for fc in fmts:
        lines.append(f"- format {fc.get('id')} · {fc.get('name')} ({fc.get('vf')}) · {fc.get('line')}")
    music = sd.get("music") or []
    if music:
        tracks = "; ".join(
            f"{m.get('title')}"
            + (f" by {m.get('artist')}" if m.get("artist") else "")
            + f" ({m.get('type')}, {m.get('outliers', 0)} outliers)"
            for m in music[:8]
        )
        lines.append("Music the outliers used: " + untrusted.wrap(tracks, "music", 1200))
    return "\n".join(lines) or "(no strong patterns yet)", refs


def _source_block(conn, user_id: str, rid: str) -> str:
    r = db.get_owned(conn, db.reels, user_id, rid)
    if r is None:
        return ""
    h = conn.execute(select(db.accounts.c.handle).where(db.accounts.c.id == r.account_id)).first()
    deep = _deep(conn, user_id, [rid]).get(rid)
    quick = _quick(conn, user_id, [rid]).get(rid) or {}
    head = (
        f"=== Source reel {rid} (@{h[0] if h else '?'}) · {r.url} · "
        + (f"{r.score:.1f}x this creator's normal" if r.score else "not scored")
        + " ==="
    )
    if deep:
        keep = {
            k: deep.get(k)
            for k in (
                "title",
                "summary",
                "format",
                "skeleton",
                "sound",
                "edit",
                "why_it_worked",
                "what_to_steal",
                "what_to_avoid",
                "fill_in_template",
                "hook_formula",
            )
        }
        weak = deep.get("weaknesses") or []
        wl = "\n".join(
            f"- {rid}:{w.get('id')} {w.get('kind')}: {w.get('note')}"
            + (f" (at {w.get('at_s')} s)" if w.get("at_s") is not None else "")
            for w in weak
        )
        return (
            head
            + "\nBreakdown: "
            + untrusted.wrap(json.dumps(keep, ensure_ascii=False), "analysis", 9000, reel=rid)
            + "\nWeakness ids you may fix (copy as '<reel_id>:W<n>'):\n"
            + (untrusted.wrap(wl, "analysis", 1500, reel=rid) if wl else "(none)")
        )
    q = {
        k: quick.get(k)
        for k in (
            "visual_format",
            "visual_subject",
            "recipe",
            "sound_primary",
            "hook_type",
            "hook_line",
            "cta_type",
            "one_line",
        )
    }
    return (
        head
        + "\nQuick tags only (no deep read; upgrades may cite '"
        + rid
        + ":general'): "
        + untrusted.wrap(json.dumps(q, ensure_ascii=False), "quick_tags", 2500, reel=rid)
        + "\nCaption: "
        + untrusted.wrap(r.caption, "caption", 600, reel=rid)
    )


def _chunk_sources(out, idx: int) -> list[str]:
    srcs = list((out.inputs or {}).get("reel_ids") or [])
    if not srcs:
        return []
    o = out.options or {}
    b = o.get("based_on") or {}
    if (
        out.kind == "reel_script"
        and (b.get("reel_ids") or b.get("hooks_from"))
        and len(srcs) >= (out.inputs or {}).get("chunks", 1)
    ):
        return [srcs[idx % len(srcs)]]
    return srcs[:3] if idx == 0 else (srcs[idx % len(srcs) :] + srcs[: idx % len(srcs)])[:3]


def _pain_for(out, d: dict, idx: int) -> str | None:
    pains = d.get("top_pains") or []
    want = (out.options or {}).get("pain")
    if want:
        return want
    return pains[idx % len(pains)] if pains else None


def _prior_lines(out) -> str:
    data = out.data or {}
    hooks = []
    for s in data.get("scripts") or []:
        hooks.append(f"- {s.get('title')}: {((s.get('hook') or {}).get('spoken') or '')[:120]}")
    for a in data.get("ads") or []:
        hooks.append(f"- {a.get('angle')}")
    for w in data.get("weeks") or []:
        for dd in w.get("days") or []:
            if dd.get("post_type") != "rest":
                hooks.append(f"- {dd.get('date')}: {dd.get('hook')}")
    return "\n".join(hooks)


def _plan_lists(conn, user_id: str, caps: dict | None) -> str:
    f, r, a = db.formats, db.reels, db.analyses
    lines = ["Format cards you may build on (use 'fmt:<id>'):"]
    fmts = conn.execute(select(f).where(f.c.user_id == user_id).order_by(f.c.computed_at.desc()).limit(20)).all()
    for fm in fmts:
        cm = _can_make(fm.vf, caps)
        lift = (fm.stats or {}).get("lift")
        lines.append(
            f"- fmt:{fm.id} · {fm.name or taxonomy.label(fm.vf)} ({fm.vf})"
            + (f" · {lift:.1f}x more likely to be an outlier" if isinstance(lift, (int, float)) else "")
            + ("" if cm["ok"] else f" · member can't make it: {cm['reason']}")
        )
    if not fmts:
        lines.append("(none yet)")
    lines.append("Outlier reels you may build on (use 'reel:<id>'):")
    rows = conn.execute(
        select(r.c.id, r.c.score, a.c.visual_format, a.c.hook_type, a.c.topics)
        .select_from(r.join(a, (a.c.reel_id == r.c.id) & (a.c.level == "quick")))
        .where(r.c.user_id == user_id, r.c.band.in_(OUTLIER_BANDS))
        .order_by(r.c.score.desc().nulls_last())
        .limit(25)
    ).all()
    for x in rows:
        cm = _can_make(x.visual_format, caps)
        lines.append(
            f"- reel:{x.id} · {x.visual_format} {taxonomy.label(x.visual_format)} · hook {x.hook_type} · "
            f"topics {', '.join(x.topics or []) or 'unsorted'} · {x.score:.1f}x"
            + ("" if cm["ok"] else " · member can't make this format")
        )
    if not rows:
        lines.append("(none yet)")
    return "\n".join(lines)


def _week_dates(start: str, idx: int) -> list[str]:
    d0 = date.fromisoformat(start) + timedelta(days=7 * idx)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(7)]


def build_brief(user_id: str, job_row, *, provider_name: str = "anthropic") -> dict:
    from .work import _task, active_topics, digest, profile_text, render_prompt, topics_text

    out_id = (job_row.payload or {}).get("output_id")
    with db.read() as c:
        out = _out(c, user_id, out_id or "")
        if out is None:
            raise Invalid("This output is gone.")
        d = digest(c, user_id) or {}
        caps = d.get("capabilities")
        o = out.options or {}
        inputs = out.inputs or {}
        idx = int(inputs.get("chunks_done") or 0)
        total = int(inputs.get("chunks") or 1)
        redo = o.get("redo_notes") or ""
        member = []
        for k, label in (
            ("topic", "Topic"),
            ("goal", "Goal"),
            ("length", "Length (seconds)"),
            ("offer", "Offer"),
            ("notes", "Notes"),
        ):
            if o.get(k):
                member.append(f"- {label}: {untrusted.escape(str(o[k]))}")
        if redo:
            member.append(f"- Change from the last version: {untrusted.escape(redo)}")
        member_t = "The member asked for:\n" + "\n".join(member) if member else ""
        prior = _prior_lines(out)
        insights_t, _ = _insights_text(c, user_id, caps)
        prof_t = profile_text(d)
        if out.kind == "reel_script":
            srcs = _chunk_sources(out, idx)
            pain = _pain_for(out, d, idx)
            b = o.get("based_on") or {}
            mode = (
                "Borrow the HOOK pattern of the source reel (same hook type and structure) on the member's own topic."
                if b.get("hooks_from")
                else "Remake the source reel better for the member's customer."
                if out.purpose == "remake"
                else "Use the source reels' format."
            )
            fmt_note = ""
            for fid in b.get("sub_formats") or []:
                fm = db.get_owned(c, db.formats, user_id, fid)
                if fm is not None and fm.guide:
                    fmt_note += f"\nSub-format {fm.id} ({fm.name}): set sub_format to '{fm.id}'.\nGuide: " + json.dumps(
                        fm.guide, ensure_ascii=False
                    )
            data = "\n\n".join(
                x
                for x in [
                    f"Write script {idx + 1} of {total}. {mode}",
                    f"Use this customer pain: {untrusted.escape(pain)}" if pain else "",
                    member_t,
                    fmt_note.strip(),
                    "\n\n".join(_source_block(c, user_id, s) for s in srcs),
                    "Topics: " + topics_text(active_topics(c, user_id)).replace("\n", "; "),
                    ("Scripts already written (don't repeat their hooks):\n" + untrusted.escape(prior))
                    if prior
                    else "",
                    "Return a ReelScriptPack with exactly 1 script in scripts.",
                ]
                if x
            )
            system, prompt = render_prompt(
                "out_reel_script",
                {
                    "count_line": "1 reel script",
                    "profile": prof_t,
                    "insights": insights_t,
                    "data": data,
                    "field_guide": S.field_guide(S.ReelScriptPack),
                },
            )
        elif out.kind == "meta_ads":
            srcs = _chunk_sources(out, idx)
            offer = o.get("offer") or d.get("offer") or ""
            data = "\n\n".join(
                x
                for x in [
                    f"Write ad set {idx + 1} of {total}. Offer: {untrusted.escape(offer)}",
                    member_t,
                    "\n\n".join(_source_block(c, user_id, s) for s in srcs),
                    ("Ad sets already written (use a different angle):\n" + untrusted.escape(prior)) if prior else "",
                    "Return a MetaAdPack with exactly 1 ad in ads.",
                ]
                if x
            )
            system, prompt = render_prompt(
                "out_meta_ads",
                {"profile": prof_t, "insights": insights_t, "data": data, "field_guide": S.field_guide(S.MetaAdPack)},
            )
        elif out.kind == "content_plan":
            start = o.get("start_date")
            dates = _week_dates(start, idx)
            ppw = int(o.get("posts_per_week") or default_posts_per_week(d))
            other = bool(o.get("include_other_types"))
            effort = (
                "at most 1 deep day a week (no editor)."
                if not caps or not caps.get("editor")
                else "mix quick, medium and deep days."
            )
            what = (
                "Chunk 0: give the pillars, week 1, batching tips and what to measure."
                if idx == 0
                else f"Chunk {idx}: give week {idx + 1} only."
            )
            data = "\n\n".join(
                x
                for x in [
                    f"{what} Week {idx + 1} dates: {', '.join(dates)} (days past day {PLAN_DAYS} are dropped).",
                    member_t,
                    _plan_lists(c, user_id, caps),
                    "Topics: " + topics_text(active_topics(c, user_id)).replace("\n", "; "),
                    ("Pillars already set: " + json.dumps((out.data or {}).get("pillars") or [], ensure_ascii=False))
                    if idx
                    else "",
                    ("Hooks already planned (never repeat them):\n" + untrusted.escape(prior)) if prior else "",
                    f"Return a ContentPlanChunk with chunk_index {idx} and exactly 1 week (week {idx + 1}).",
                ]
                if x
            )
            system, prompt = render_prompt(
                "out_content_plan",
                {
                    "profile": prof_t,
                    "insights": insights_t,
                    "data": data,
                    "posts_per_week": str(ppw),
                    "post_types_line": (
                        "Post types: reel, carousel, story, live or rest."
                        if other
                        else "Post types: only 'reel' or 'rest' (the data is reels only)."
                    ),
                    "effort_line": effort,
                    "field_guide": S.field_guide(S.ContentPlanChunk),
                },
            )
        else:
            nums = inputs.get("numbers") or {}
            slim = dict(nums)
            slim["remake_candidates"] = [
                {k: v for k, v in x.items() if k != "thumb"} for x in nums.get("remake_candidates") or []
            ]
            allowed = sorted(card_values(nums))
            data = "\n\n".join(
                x
                for x in [
                    member_t,
                    "The numbers: "
                    + untrusted.wrap(json.dumps(slim, ensure_ascii=False, default=str), "report_numbers", 30000),
                    "card_value must be one of: "
                    + (", ".join(allowed) if allowed else "(none: return [] for findings)"),
                    "Customer pains (copy exactly): " + untrusted.escape(" | ".join(d.get("top_pains") or [])),
                    "Return the CompetitorReport.",
                ]
                if x
            )
            system, prompt = render_prompt(
                "out_competitor_report",
                {"profile": prof_t, "data": data, "field_guide": S.field_guide(S.CompetitorReport)},
            )
    model = S.CHUNK_MODELS[out.kind]
    return _task(
        "make_output",
        system,
        prompt,
        [],
        "paid_api",
        model.__name__,
        provider_name,
        extra={"output_id": out.id, "kind": out.kind, "chunk": {"index": idx, "of": total}},
    )


# ============================================================================================== saving


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower()).strip(" .")


def _match(value: str, choices: list[str]) -> str | None:
    v = _norm(value)
    for c in choices:
        if _norm(c) == v:
            return c
    for c in choices:
        if v and (v in _norm(c) or _norm(c) in v) and len(v) > 8:
            return c
    return None


def _fill_ref(ref: dict | None, allowed: list[str], urls: dict) -> dict | None:
    if not ref:
        return ref
    if ref.get("reel_id") not in allowed:
        raise Invalid(f"Every reference must use one of the source reels: {', '.join(allowed)}.")
    ref["url"] = urls.get(ref["reel_id"], "")
    return ref


def _check_script(c, user_id: str, out, s: dict, d: dict) -> dict:
    from .work import active_topics

    pains = list(d.get("top_pains") or [])
    proofs = list(d.get("proof") or [])
    srcs = list((out.inputs or {}).get("reel_ids") or [])
    pm = _match(s.get("pain", ""), pains)
    if pains and pm is None:
        raise Invalid("pain must be one of the member's customer pains, copied exactly: " + " | ".join(pains))
    if pm:
        s["pain"] = pm
    if _norm(s.get("proof_used", "")) not in ("none available", "none"):
        pr = _match(s.get("proof_used", ""), proofs)
        if pr is None:
            raise Invalid(
                "proof_used must be one of the member's proof items, copied exactly, or 'none available'."
                + (" Proof: " + " | ".join(proofs) if proofs else "")
            )
        s["proof_used"] = pr
    else:
        s["proof_used"] = "none available"
    s["based_on"] = [b for b in s.get("based_on") or [] if b in srcs] or srcs[:1]
    deep = _deep(c, user_id, srcs)
    snap = _snapshot(c, user_id)
    cards = set()
    if snap is not None:
        for cd in (snap.data or {}).get("cards") or []:
            cards.add(f"card:{cd.get('dimension')}:{cd.get('value')}")
    if not s.get("upgrades"):
        raise Invalid("Give 3-5 upgrades: concrete ways this script beats the source.")
    for up in s["upgrades"]:
        if up.get("vs_source") not in srcs:
            raise Invalid(f"upgrades[].vs_source must be a source reel id: {', '.join(srcs)}.")
        fx = up.get("fixes") or ""
        m = re.fullmatch(r"(reel_[a-z0-9]+):(W[1-5]|general)", fx)
        if m:
            rid, wid = m.groups()
            if rid not in srcs:
                raise Invalid(f"upgrades[].fixes names reel {rid}, which is not a source reel.")
            weak = {w.get("id") for w in (deep.get(rid) or {}).get("weaknesses") or []}
            if wid == "general" and weak:
                raise Invalid(f"Reel {rid} has weakness ids ({', '.join(sorted(weak))}): cite one of them.")
            if wid != "general" and wid not in weak:
                raise Invalid(f"Reel {rid} has no weakness {wid}. Use one of: {', '.join(sorted(weak)) or 'none'}.")
        elif fx not in cards:
            raise Invalid(
                "upgrades[].fixes must be '<reel_id>:W<n>' of a source reel or a working card: "
                + (", ".join(sorted(cards)) or "(no cards yet)")
                + "."
            )
    r = db.reels
    urls = {x.id: x.url for x in c.execute(select(r.c.id, r.c.url).where(r.c.user_id == user_id, r.c.id.in_(srcs)))}
    eb = s.get("editor_brief") or {}
    eb["references"] = [_fill_ref(x, srcs, urls) for x in eb.get("references") or []]
    for row in s.get("sound_cue_sheet") or []:
        row["ref"] = _fill_ref(row.get("ref"), srcs, urls)
        if not row.get("source_hint"):
            row["source_hint"] = taxonomy.sfx_source(row.get("kind"))
    if s.get("sub_format"):
        if db.get_owned(c, db.formats, user_id, s["sub_format"]) is None:
            s["sub_format"] = ""
    slugs = {t.slug for t in active_topics(c, user_id)}
    if s.get("topic") not in slugs:
        s["topic"] = "unsorted"
    return s


def _check_ad(c, user_id: str, out, ad: dict) -> dict:
    srcs = list((out.inputs or {}).get("reel_ids") or [])
    ad["based_on"] = _member_reels(c, user_id, ad.get("based_on") or []) or srcs[:2]
    return ad


def _check_plan(c, user_id: str, out, ch: dict, d: dict) -> dict:
    o = out.options or {}
    idx = int((out.inputs or {}).get("chunks_done") or 0)
    caps = d.get("capabilities")
    if idx == 0:
        pil = ch.get("pillars") or []
        if len(pil) < 3:
            raise Invalid("Chunk 0 needs 3-5 pillars.")
        tot = sum(int(p.get("share_pct") or 0) for p in pil)
        if tot != 100:
            if not 90 <= tot <= 110:
                raise Invalid(f"pillars share_pct must add up to 100 (now {tot}).")
            scaled = [round(int(p["share_pct"]) * 100 / tot) for p in pil]
            scaled[0] += 100 - sum(scaled)
            for p, v in zip(pil, scaled, strict=True):
                p["share_pct"] = v
    if len(ch.get("weeks") or []) != 1:
        raise Invalid(f"Return exactly 1 week (week {idx + 1}).")
    wk = ch["weeks"][0]
    wk["week"] = idx + 1
    dates = _week_dates(o.get("start_date"), idx)
    other = bool(o.get("include_other_types"))
    f = db.formats
    fmt_ids = {x[0] for x in c.execute(select(f.c.id).where(f.c.user_id == user_id))}
    r = db.reels
    reel_ids = {x[0] for x in c.execute(select(r.c.id).where(r.c.user_id == user_id))}
    errs = []
    for i, day in enumerate(wk["days"]):
        day["date"] = dates[i]
        if not other and day.get("post_type") not in ("reel", "rest"):
            errs.append(f"{dates[i]}: post_type must be reel or rest")
        if day.get("post_type") == "rest":
            day["format"], day["builds_on"] = "none", ""
            continue
        bo = day.get("builds_on") or ""
        m = re.fullmatch(r"(fmt|reel):(\S+)", bo)
        if (
            not m
            or (m.group(1) == "fmt" and m.group(2) not in fmt_ids)
            or (m.group(1) == "reel" and m.group(2) not in reel_ids)
        ):
            errs.append(f"{dates[i]}: builds_on must be 'fmt:<id>' or 'reel:<id>' from the lists")
        if (
            day.get("post_type") == "reel"
            and day.get("format") not in ("none",)
            and not _can_make(day.get("format"), caps)["ok"]
        ):
            errs.append(f"{dates[i]}: the member can't make {day.get('format')}")
        day["based_on"] = [x for x in day.get("based_on") or [] if x in reel_ids]
    if errs:
        raise Invalid("Fix these days: " + "; ".join(errs[:8]) + ".")
    ch["chunk_index"] = idx
    return ch


def _check_report(c, user_id: str, out, rep: dict, d: dict) -> dict:
    nums = (out.inputs or {}).get("numbers") or {}
    allowed = card_values(nums)
    errs = []
    for key in ("working", "overused", "opportunities"):
        for fnd in rep.get(key) or []:
            if fnd.get("card_value") not in allowed:
                errs.append(f"{key}: card_value {fnd.get('card_value')!r} is not in the numbers")
    cand = {x.get("reel_id"): x for x in nums.get("remake_candidates") or []}
    for it in rep.get("remake_queue") or []:
        if it.get("reel_id") not in cand:
            errs.append(f"remake_queue: {it.get('reel_id')} is not a remake candidate")
    caps = d.get("capabilities")
    for rf in rep.get("recommended_formats") or []:
        if not _can_make(rf.get("format"), caps)["ok"] and not (rf.get("why") or "").lower().startswith("if you can"):
            errs.append(f"recommended_formats: the member can't make {rf.get('format')} (start why with 'If you can')")
    pains = list(d.get("top_pains") or [])
    for g in rep.get("customer_gaps") or []:
        pm = _match(g.get("pain", ""), pains)
        if pm is None:
            errs.append(f"customer_gaps: {g.get('pain')!r} is not one of the customer pains")
        else:
            g["pain"] = pm
    if errs:
        raise Invalid("Fix these: " + "; ".join(errs[:8]) + ".")
    cov = []
    for pc in rep.get("pain_coverage") or []:
        pm = _match(pc.get("pain", ""), pains)
        if pm:
            cov.append({"pain": pm, "covered": bool(pc.get("covered"))})
    rep["pain_coverage"] = cov
    handles = {a.get("handle") for a in nums.get("accounts") or []}
    rep["competitors"] = (
        [x for x in rep.get("competitors") or [] if x.get("handle", "").lstrip("@") in handles]
        or rep.get("competitors")
        or []
    )
    return rep


def save_chunk(user_id: str, job_row, output, made_by: dict, *, usage=None) -> dict:
    from .settings import get_settings
    from .work import _validate, digest

    out_id = (job_row.payload or {}).get("output_id")
    with db.tx() as c:
        out = _out(c, user_id, out_id or "")
        if out is None:
            raise Invalid("This output is gone.")
        if out.status == "done":
            raise Invalid("This output is already finished.")
        model = S.CHUNK_MODELS[out.kind]
        obj = _validate(model, output)
        d = digest(c, user_id) or {}
        inputs = dict(out.inputs or {})
        idx = int(inputs.get("chunks_done") or 0)
        total = int(inputs.get("chunks") or 1)
        data = dict(out.data or {})
        ch = obj.model_dump()
        if out.kind == "reel_script":
            if not ch["scripts"]:
                raise Invalid("Return exactly 1 script in scripts.")
            s = _check_script(c, user_id, out, ch["scripts"][0], d)
            data = {"kind": "reel_script", "scripts": list(data.get("scripts") or []) + [s]}
        elif out.kind == "meta_ads":
            if not ch["ads"]:
                raise Invalid("Return exactly 1 ad in ads.")
            ad = _check_ad(c, user_id, out, ch["ads"][0])
            data = {"kind": "meta_ads", "ads": list(data.get("ads") or []) + [ad]}
        elif out.kind == "content_plan":
            ch["chunk_index"] = idx
            ch = _check_plan(c, user_id, out, ch, d)
            o = out.options or {}
            if idx == 0:
                data = {
                    "kind": "content_plan",
                    "start_date": o.get("start_date"),
                    "posts_per_week": int(o.get("posts_per_week") or default_posts_per_week(d)),
                    "pillars": ch["pillars"],
                    "weeks": [],
                    "batching_tips": ch["batching_tips"],
                    "measure": ch["measure"],
                }
            data["weeks"] = list(data.get("weeks") or []) + ch["weeks"]
            if idx == total - 1:  # drop days past day 30
                end = (date.fromisoformat(o.get("start_date")) + timedelta(days=PLAN_DAYS - 1)).isoformat()
                for w in data["weeks"]:
                    w["days"] = [x for x in w["days"] if x["date"] <= end]
                data["weeks"] = [w for w in data["weeks"] if w["days"]]
        else:
            data = _check_report(c, user_id, out, ch, d)
        idx += 1
        inputs["chunks_done"] = idx
        done = idx >= total
        vals: dict[str, Any] = dict(
            data=data, inputs=inputs, made_by=made_by, updated_at=db.utcnow(), status="done" if done else "working"
        )
        if done:
            vals["markdown"] = render(out.kind, data, render_numbers(c, user_id, out, data))
            if out.kind == "content_plan":
                ContentPlan30.model_validate(data)
        c.execute(db.outputs.update().where(db.outputs.c.id == out.id).values(**vals))
    url = f"{get_settings().public_url.rstrip('/')}/#output/{out_id}"
    return {
        "ok": True,
        "output_id": out_id,
        "url": url,
        "next_chunk": None if done else idx,
        "chunk": {"index": idx - 1, "of": total},
    }


ContentPlan30 = S.ContentPlan30


def render_numbers(c, user_id: str, out, data: dict) -> dict:
    """Server-side numbers a rendering shows next to the AI's text (handles, scores, links)."""
    if out.kind == "competitor_report":
        return (out.inputs or {}).get("numbers") or {}
    ids: list[str] = list((out.inputs or {}).get("reel_ids") or [])
    for s in data.get("scripts") or []:
        ids += s.get("based_on") or []
    r, acc = db.reels, db.accounts
    rows = c.execute(
        select(r.c.id, r.c.url, r.c.score, acc.c.handle)
        .select_from(r.join(acc, acc.c.id == r.c.account_id, isouter=True))
        .where(r.c.user_id == user_id, r.c.id.in_(list(set(ids))))
    ).all()
    return {"reels": {x.id: {"url": x.url, "score": x.score, "handle": x.handle} for x in rows}}


def redo(user_id: str, out_id: str, notes: str | None) -> tuple[str, str]:
    with db.read() as c:
        out = _out(c, user_id, out_id)
        if out is None:
            raise NotReady("That output is not in your ReelVault.", "not_found", 404)
    if notes and len(notes) > 1000:
        raise NotReady("Keep the notes under 1,000 characters.")
    opts = dict(out.options or {})
    opts["redo_of"] = out_id
    new_id, jid = create(user_id, out.kind, opts, purpose=out.purpose, brand_id=out.brand_id)
    if notes:
        with db.tx() as c:
            row = _out(c, user_id, new_id)
            c.execute(
                db.outputs.update()
                .where(db.outputs.c.id == new_id)
                .values(options={**(row.options or {}), "redo_notes": notes})
            )
    return new_id, jid


def edit(user_id: str, out_id: str, data: dict) -> None:
    """PATCH /api/outputs/<id> with member edits of `data`: re-validated, markdown re-rendered."""
    with db.tx() as c:
        out = _out(c, user_id, out_id)
        if out is None:
            raise NotReady("That output is not in your ReelVault.", "not_found", 404)
        if out.status != "done":
            raise NotReady("Wait until the output is finished, then edit it.", "busy", 409)
        model = S.OUTPUT_MODELS[out.kind]
        try:
            obj = model.model_validate(data)
        except ValidationError as e:
            raise NotReady("That edit doesn't fit the output: " + str(e.errors()[0].get("msg", ""))) from None
        new = obj.model_dump()
        if out.kind == "reel_script":  # keep server-filled links
            urls = render_numbers(c, user_id, out, new)["reels"]
            for s in new["scripts"]:
                for ref in (s.get("editor_brief") or {}).get("references") or []:
                    ref["url"] = (urls.get(ref.get("reel_id")) or {}).get("url", "")
                for row in s.get("sound_cue_sheet") or []:
                    if row.get("ref"):
                        row["ref"]["url"] = (urls.get(row["ref"].get("reel_id")) or {}).get("url", "")
        if out.kind == "competitor_report":
            new = {**(out.data or {}), **new}
        c.execute(
            db.outputs.update()
            .where(db.outputs.c.id == out_id)
            .values(
                data=new, markdown=render(out.kind, new, render_numbers(c, user_id, out, new)), updated_at=db.utcnow()
            )
        )


# ============================================================================================== rendering


def _cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def _table(head: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(_cell(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def _t(sec: Any) -> str:
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return str(sec or "")
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _ref_text(ref: dict | None, numbers: dict) -> str:
    if not ref:
        return ""
    h = ((numbers.get("reels") or {}).get(ref.get("reel_id")) or {}).get("handle")
    return f"▶ @{h or '?'} {ref.get('start', '')}-{ref.get('end', '')}"


def _cue_rows(rows: list[dict], numbers: dict) -> list[list[Any]]:
    return [
        [
            r.get("t"),
            r.get("until"),
            f"{r.get('kind')} ({r.get('pattern')})",
            r.get("what"),
            r.get("purpose"),
            r.get("level"),
            r.get("source_hint"),
            _ref_text(r.get("ref"), numbers),
        ]
        for r in rows
    ]


CUE_HEAD = ["Time", "Until", "Sound", "What", "Why", "Level", "Where to get it", "Hear it in the original"]


def _brief_md(eb: dict, numbers: dict) -> str:
    m = eb.get("music") or {}
    lines = [
        f"- Format: {taxonomy.label(eb.get('format'))} ({eb.get('format')}), {eb.get('aspect')}, "
        f"{eb.get('length_s')} s",
        f"- How to shoot it: {eb.get('format_recipe')}",
        f"- Pace: {eb.get('pace')}",
        f"- Captions: {eb.get('caption_style')}, {eb.get('caption_look')}",
        f"- Color and look: {eb.get('color_look')}",
        f"- Music: {m.get('type')}, {m.get('mood')}, {m.get('bpm_range')} BPM, {m.get('level')}"
        + (f". Options: {', '.join(m.get('options') or [])}" if m.get("options") else "")
        + (f". {m.get('notes')}" if m.get("notes") else ""),
    ]
    for key, label in (
        ("broll", "B-roll to find or film"),
        ("graphics", "Graphics"),
        ("proof_inserts", "Proof to show"),
        ("dos", "Do"),
        ("donts", "Don't"),
    ):
        if eb.get(key):
            lines.append(f"- {label}: " + "; ".join(eb[key]))
    refs = eb.get("references") or []
    if refs:
        lines.append("- Watch these moments:")
        for r in refs:
            lines.append(f"  - {r.get('url')} {r.get('start')}-{r.get('end')}: {r.get('watch_for')}")
    return "\n".join(lines)


def _script_md(s: dict, numbers: dict) -> str:
    reels = numbers.get("reels") or {}
    built = []
    for rid in s.get("based_on") or []:
        x = reels.get(rid) or {}
        built.append(f"@{x.get('handle') or '?'}'s reel" + (f" ({x['score']:.1f}x normal)" if x.get("score") else ""))
    ups = "; ".join(u.get("change", "") for u in s.get("upgrades") or [])
    h = s.get("hook") or {}
    cta = s.get("cta") or {}
    parts = [
        f"## {s.get('title')}",
        f"Built on {', '.join(built) or 'your competitors'} · Upgrades: {ups}",
        f"Angle: {s.get('angle')}  \nFor this pain: {s.get('pain')} · Proof: {s.get('proof_used')} · "
        f"{s.get('length_s')} s · {taxonomy.label(s.get('format'))}",
        f"### Hook\n**{h.get('spoken', '')}**  \nOn screen: {h.get('on_screen_text', '')}  \n"
        f"Visual: {h.get('visual', '')}",
        "### Beats\n"
        + _table(
            ["Time", "Say", "On screen", "Visual"],
            [
                [f"{_t(b.get('t_start'))}-{_t(b.get('t_end'))}", b.get("say"), b.get("on_screen_text"), b.get("visual")]
                for b in s.get("beats") or []
            ],
        ),
        f"### CTA\n{cta.get('text', '')} ({cta.get('cta_type', '')}"
        + (f", keyword {cta.get('keyword')}" if cta.get("keyword") else "")
        + ")",
        f"### Caption\n{s.get('caption', '')}\n\n{' '.join(s.get('hashtags') or [])}",
        "### Editor brief\n" + _brief_md(s.get("editor_brief") or {}, numbers),
        "### Sound cue sheet\n" + _table(CUE_HEAD, _cue_rows(s.get("sound_cue_sheet") or [], numbers)),
    ]
    if s.get("why_it_should_work"):
        parts.append("### Why it should work\n" + "\n".join(f"- {w}" for w in s["why_it_should_work"]))
    if s.get("upgrades"):
        parts.append(
            "### Upgrades over the original\n"
            + "\n".join(f"- {u.get('change')} ({u.get('why')})" for u in s["upgrades"])
        )
    if s.get("too_close"):
        parts.append(
            "### Still too close to the original\n"
            + "\n".join(f'- "{t.get("line")}": {t.get("why")}' for t in s["too_close"])
        )
    return "\n\n".join(parts)


def _ad_script_md(a: dict) -> str:
    return (
        _table(
            ["Time", "Say", "On screen", "Visual"],
            [
                [f"{_t(b.get('t_start'))}-{_t(b.get('t_end'))}", b.get("say"), b.get("on_screen_text"), b.get("visual")]
                for b in a.get("beats") or []
            ],
        )
        + f"\n\nCTA: {a.get('cta')}  \nEditor notes: {a.get('editor_notes')}"
    )


def _ad_md(ad: dict, i: int, numbers: dict) -> str:
    ht = ad.get("hook_test") or {}
    pt = ad.get("primary_text") or ""
    parts = [
        f"## Ad set {i}: {ad.get('angle')}",
        f"Offer: {ad.get('offer')} · Audience: {str(ad.get('awareness', '')).replace('_', ' ')}",
        "### 15-second ad\n" + _ad_script_md(ad.get("ad_15") or {}),
        "### 30-second ad\n" + _ad_script_md(ad.get("ad_30") or {}),
        "### 5-hook test (swap the first 3 seconds of the 30-second ad)\n"
        + _table(
            ["ID", "Angle", "Spoken", "On screen", "First frame"],
            [
                [h.get("id"), h.get("angle"), h.get("spoken"), h.get("on_screen_text"), h.get("first_frame")]
                for h in ht.get("hooks") or []
            ],
        ),
        "How to run it:\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(ht.get("setup") or [], 1)),
        "How to read the results:\n" + "\n".join(f"- {s}" for s in ht.get("read_results") or []),
        "### Ad copy\n"
        + _table(
            ["Field", "Text", "Characters"],
            [
                ["Primary text", pt, f"{len(pt)} (first line {len(pt.splitlines()[0]) if pt else 0} of 125)"],
                ["Headline", ad.get("headline"), f"{len(ad.get('headline') or '')} of 40"],
                ["Description", ad.get("description"), f"{len(ad.get('description') or '')} of 30"],
                ["Button", ad.get("cta_button"), ""],
            ],
        ),
    ]
    if ad.get("policy_notes"):
        parts.append(
            "> **Check before you run it (Meta ad policy):**\n" + "\n".join(f"> - {p}" for p in ad["policy_notes"])
        )
    return "\n\n".join(parts)


def _plan_md(p: dict) -> str:
    parts = [
        f"Starts {p.get('start_date')} · about {p.get('posts_per_week')} reels a week",
        "## Pillars\n"
        + _table(
            ["Pillar", "Topic", "Share", "Why"],
            [[x.get("label"), x.get("topic"), f"{x.get('share_pct')}%", x.get("why")] for x in p.get("pillars") or []],
        ),
    ]
    for w in p.get("weeks") or []:
        parts.append(
            f"## Week {w.get('week')}: {w.get('theme')}\n"
            + _table(
                ["Date", "Type", "Topic", "Format", "Builds on", "Hook", "Idea", "CTA", "Effort"],
                [
                    [
                        d.get("date"),
                        d.get("post_type"),
                        d.get("topic"),
                        taxonomy.label(d.get("format")) if d.get("format") != "none" else "",
                        d.get("builds_on"),
                        d.get("hook"),
                        d.get("idea"),
                        d.get("cta_type"),
                        d.get("effort"),
                    ]
                    for d in w.get("days") or []
                ],
            )
        )
    if p.get("batching_tips"):
        parts.append("## Batching tips\n" + "\n".join(f"- {t}" for t in p["batching_tips"]))
    if p.get("measure"):
        parts.append("## What to measure\n" + "\n".join(f"- {t}" for t in p["measure"]))
    return "\n\n".join(parts)


def _finding_md(f: dict, numbers: dict) -> str:
    line = ""
    dim, _, val = (f.get("card_value") or "").partition(":")
    for cd in numbers.get("cards") or []:
        if cd.get("dimension") == dim and str(cd.get("value")) == val:
            line = cd.get("line") or ""
    if not line and dim == "sub_format":
        for fc in numbers.get("formats") or []:
            if fc.get("id") == val:
                line = f"{fc.get('name')}: {fc.get('line') or ''}".strip(": ")
    return f"- **{f.get('finding')}** {('(' + line + ')') if line else ''}  \n  How to use it: {f.get('how_to_use')}"


def _report_md(rep: dict, numbers: dict) -> str:
    note = numbers.get("data_note") or {}
    parts = [
        "\n".join(f"- {h}" for h in rep.get("headline") or []),
        "> "
        + (note.get("saves_line") or SAVES_LINE)
        + (f"  \n> {note['sample_line']}" if note.get("sample_line") else ""),
    ]
    accs = numbers.get("accounts") or []
    if accs:
        parts.append(
            "## Your competitors in numbers\n"
            + _table(
                ["Account", "Followers", "Reels scored", "Normal views", "Outliers", "Best reel"],
                [
                    [
                        f"@{a.get('handle')}",
                        a.get("followers"),
                        a.get("reels_scored"),
                        a.get("normal_views"),
                        a.get("outliers"),
                        ((a.get("best_reel") or {}).get("url") or "")
                        + (f" ({(a.get('best_reel') or {}).get('score')}x)" if a.get("best_reel") else ""),
                    ]
                    for a in accs
                ],
            )
        )
    rec = numbers.get("recent") or {}
    if rec.get("rising") or rec.get("fading"):
        lines = [
            f"- Rising: {r.get('label') or r.get('value')} ({r.get('dimension')})" for r in rec.get("rising") or []
        ]
        lines += [
            f"- Fading: {r.get('label') or r.get('value')} ({r.get('dimension')})" for r in rec.get("fading") or []
        ]
        parts.append(f"## Rising and fading in the last {rec.get('window_days', 90)} days\n" + "\n".join(lines))
    for key, label in (("working", "What works"), ("overused", "Overused"), ("opportunities", "Opportunities")):
        if rep.get(key):
            parts.append(f"## {label}\n" + "\n".join(_finding_md(f, numbers) for f in rep[key]))
    for cr in rep.get("competitors") or []:
        parts.append(
            f"### @{cr.get('handle')}\n- What they do: {cr.get('what_they_do')}\n- What works: "
            f"{cr.get('what_works')}\n- Best hooks: {'; '.join(cr.get('best_hooks') or [])}\n- CTAs: "
            f"{cr.get('cta_pattern')}\n- Sound: {cr.get('sound_pattern')}\n- Gaps: "
            f"{'; '.join(cr.get('gaps') or [])}"
        )
    if rep.get("sound_design"):
        parts.append("## Sound design\n" + "\n".join(f"- {s}" for s in rep["sound_design"]))
    cand = {x.get("reel_id"): x for x in numbers.get("remake_candidates") or []}
    if rep.get("remake_queue"):
        rows = []
        for n, it in enumerate(rep["remake_queue"], 1):
            x = cand.get(it.get("reel_id")) or {}
            rows.append(
                [
                    n,
                    f"@{x.get('handle', '?')} {x.get('url', '')}",
                    f"{x.get('score', '')}x",
                    it.get("why"),
                    it.get("angle"),
                ]
            )
        parts.append("## The reels to remake first\n" + _table(["#", "Reel", "Score", "Why", "Your version"], rows))
    if rep.get("recommended_formats"):
        parts.append(
            "## Formats to try\n"
            + "\n".join(
                f"- {taxonomy.label(r.get('format'))}: {r.get('why')} First idea: {r.get('first_script_idea')}"
                for r in rep["recommended_formats"]
            )
        )
    if rep.get("customer_gaps"):
        parts.append(
            "## Customer pains nobody covers\n"
            + "\n".join(f"- {g.get('pain')}: {g.get('gap')} Idea: {g.get('idea')}" for g in rep["customer_gaps"])
        )
    music = numbers.get("music") or []
    if music:
        parts.append(
            "## Music the outliers used\n"
            + _table(
                ["Track", "Artist", "Outliers", "Type"],
                [[m.get("title"), m.get("artist"), m.get("outliers"), m.get("type")] for m in music[:15]],
            )
        )
    if rep.get("next_steps"):
        parts.append("## Next steps\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(rep["next_steps"], 1)))
    return "\n\n".join(p for p in parts if p)


def render(kind: str, data: dict | None, numbers: dict | None = None) -> str:
    """Markdown for one output (the .md download and the print page). The web UI renders `data` as cards."""
    data, numbers = data or {}, numbers or {}
    title = {
        "reel_script": "# Reel scripts",
        "meta_ads": "# Meta ad scripts",
        "content_plan": "# 30-day content plan",
        "competitor_report": "# Competitor report",
    }[kind]
    if kind == "reel_script":
        body = "\n\n---\n\n".join(_script_md(s, numbers) for s in data.get("scripts") or [])
    elif kind == "meta_ads":
        body = "\n\n---\n\n".join(_ad_md(a, i, numbers) for i, a in enumerate(data.get("ads") or [], 1))
    elif kind == "content_plan":
        body = _plan_md(data)
    else:
        body = _report_md(data, numbers)
    return f"{title}\n\n{body}\n"


def render_text(kind: str, data: dict | None, numbers: dict | None = None) -> str:
    """Plain text: the editor brief(s) for reel scripts, else the markdown without symbols."""
    data, numbers = data or {}, numbers or {}
    if kind == "reel_script":
        out = []
        for s in data.get("scripts") or []:
            eb = s.get("editor_brief") or {}
            lines = [f"EDITOR BRIEF: {s.get('title')}", "", _brief_md(eb, numbers).replace("**", ""), "", "SHOT LIST"]
            for b in s.get("beats") or []:
                lines.append(
                    f"{_t(b.get('t_start'))}-{_t(b.get('t_end'))}  SAY: {b.get('say')}  ON SCREEN: "
                    f"{b.get('on_screen_text')}  VISUAL: {b.get('visual')}"
                )
            lines += ["", "SOUND CUES"]
            for r in s.get("sound_cue_sheet") or []:
                lines.append(
                    f"{r.get('t')}{('-' + r['until']) if r.get('until') else ''}  {r.get('kind')} "
                    f"({r.get('level')}): {r.get('what')}. Why: {r.get('purpose')}. Get it: "
                    f"{r.get('source_hint')}"
                    + (
                        f". Hear it: {(r.get('ref') or {}).get('url')} {(r.get('ref') or {}).get('start')}"
                        if r.get("ref")
                        else ""
                    )
                )
            out.append("\n".join(lines))
        return "\n\n\n".join(out) + "\n"
    md = render(kind, data, numbers)
    return re.sub(r"[*#>`]", "", md)


def render_csv(kind: str, data: dict | None, numbers: dict | None = None) -> str:
    data = data or {}
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "reel_script":
        w.writerow(["script", "time", "say", "on screen", "visual", "sound", "where to get it", "reference url"])
        for s in data.get("scripts") or []:
            cues = s.get("sound_cue_sheet") or []
            for b in s.get("beats") or []:
                inb = [r for r in cues if _in_beat(r.get("t"), b)]
                w.writerow(
                    [
                        s.get("title"),
                        f"{_t(b.get('t_start'))}-{_t(b.get('t_end'))}",
                        b.get("say"),
                        b.get("on_screen_text"),
                        b.get("visual"),
                        "; ".join(f"{r.get('t')} {r.get('kind')}: {r.get('what')}" for r in inb),
                        "; ".join(r.get("source_hint") or "" for r in inb),
                        "; ".join((r.get("ref") or {}).get("url") or "" for r in inb if r.get("ref")),
                    ]
                )
    elif kind == "content_plan":
        w.writerow(["date", "type", "topic", "format", "hook", "idea", "cta", "builds on", "effort"])
        for wk in data.get("weeks") or []:
            for d in wk.get("days") or []:
                w.writerow(
                    [
                        d.get("date"),
                        d.get("post_type"),
                        d.get("topic"),
                        d.get("format"),
                        d.get("hook"),
                        d.get("idea"),
                        d.get("cta_type"),
                        d.get("builds_on"),
                        d.get("effort"),
                    ]
                )
    elif kind == "meta_ads":
        w.writerow(["ad set", "length", "time", "say", "on screen", "visual"])
        for i, ad in enumerate(data.get("ads") or [], 1):
            for key in ("ad_15", "ad_30"):
                a = ad.get(key) or {}
                for b in a.get("beats") or []:
                    w.writerow(
                        [
                            i,
                            a.get("length_s"),
                            f"{_t(b.get('t_start'))}-{_t(b.get('t_end'))}",
                            b.get("say"),
                            b.get("on_screen_text"),
                            b.get("visual"),
                        ]
                    )
            for h in (ad.get("hook_test") or {}).get("hooks") or []:
                w.writerow(
                    [
                        i,
                        "hook test " + str(h.get("id")),
                        "0:00-0:03",
                        h.get("spoken"),
                        h.get("on_screen_text"),
                        h.get("first_frame"),
                    ]
                )
    else:
        w.writerow(["order", "reel id", "url", "why", "your version"])
        cand = {x.get("reel_id"): x for x in (numbers or {}).get("remake_candidates") or []}
        for n, it in enumerate(data.get("remake_queue") or [], 1):
            w.writerow(
                [
                    n,
                    it.get("reel_id"),
                    (cand.get(it.get("reel_id")) or {}).get("url", ""),
                    it.get("why"),
                    it.get("angle"),
                ]
            )
    return buf.getvalue()


def _in_beat(t: Any, b: dict) -> bool:
    m = re.match(r"(\d+):(\d+(?:\.\d+)?)", str(t or ""))
    if not m:
        return False
    s = int(m.group(1)) * 60 + float(m.group(2))
    return float(b.get("t_start") or 0) <= s < float(b.get("t_end") or 0) + 0.01


PRINT_CSS = (
    "body{font:14px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif;color:#151a2b;max-width:860px;"
    "margin:24px auto;padding:0 16px}h1{font-size:24px}h2{font-size:18px;margin-top:28px;"
    "border-bottom:1px solid #dfe1e8}h3{font-size:15px}table{border-collapse:collapse;width:100%;"
    "margin:8px 0;font-size:12.5px}td,th{border:1px solid #dfe1e8;padding:4px 6px;text-align:left;"
    "vertical-align:top}th{background:#f2f3f6}blockquote{background:#fdf3e2;border-left:4px solid #b45309;"
    "margin:8px 0;padding:6px 10px}.hint{color:#5f6577;font-size:12px}hr{border:0;border-top:2px solid "
    "#dfe1e8;margin:28px 0}@media print{.hint{display:none}a{color:inherit}}"
)


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(https://www\.instagram\.com/[^\s<]+)", r'<a href="\1">\1</a>', s)
    return s


def md_to_html(md: str) -> str:
    """A small converter for the markdown render() writes (headings, tables, lists, quotes, rules)."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            cells = [[c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", r.strip())[1:-1]] for r in rows]
            body = cells[2:] if len(cells) > 1 else []
            out.append(
                "<table><tr>"
                + "".join(f"<th>{_inline(c)}</th>" for c in cells[0])
                + "</tr>"
                + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
                + "</table>"
            )
            continue
        m = re.match(r"(#{1,3}) (.*)", ln)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
        elif ln.strip() == "---":
            out.append("<hr>")
        elif ln.startswith(">"):
            q = []
            while i < len(lines) and lines[i].startswith(">"):
                q.append(_inline(lines[i].lstrip("> ").rstrip()))
                i += 1
            out.append("<blockquote>" + "<br>".join(q) + "</blockquote>")
            continue
        elif re.match(r"\s*(- |\d+\. )", ln):
            items = []
            while i < len(lines) and re.match(r"\s*(- |\d+\. )", lines[i]):
                items.append(_inline(re.sub(r"^\s*(- |\d+\. )", "", lines[i])))
                i += 1
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            continue
        elif ln.strip():
            out.append(f"<p>{_inline(ln.rstrip())}</p>")
        i += 1
    return "\n".join(out)


def render_print(title: str, markdown: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width, initial-scale=1"><title>'
        + html.escape(title)
        + "</title><style>"
        + PRINT_CSS
        + '</style></head><body><p class="hint">To save a PDF: open your browser\'s Print menu '
        "and choose Save as PDF.</p>" + md_to_html(markdown) + "</body></html>"
    )


def print_csp() -> str:
    import base64
    import hashlib

    h = base64.b64encode(hashlib.sha256(PRINT_CSS.encode()).digest()).decode()
    return (
        f"default-src 'none'; style-src 'sha256-{h}'; img-src 'self' data:; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'none'"
    )


def summary(row, job=None) -> dict:
    from . import jobs as jobs_mod

    out = {
        "id": row.id,
        "kind": row.kind,
        "kind_label": KIND_LABELS.get(row.kind),
        "title": row.title,
        "status": row.status,
        "starred": bool(row.starred),
        "purpose": row.purpose,
        "created_at": db.iso(row.created_at),
        "updated_at": db.iso(row.updated_at),
    }
    if job is not None:
        out["job"] = jobs_mod.to_api(job)
    return out


def count_today(user_id: str) -> int:
    o = db.outputs
    start = datetime.combine(db.utcnow().date(), datetime.min.time(), tzinfo=db.utcnow().tzinfo)
    with db.read() as c:
        return int(
            c.execute(
                select(func.count()).select_from(o).where(o.c.user_id == user_id, o.c.created_at >= start)
            ).scalar()
            or 0
        )
