"""Shared AI task contexts and save functions for every path: API key, MCP connector and desktop app (DESIGN-SAAS.md
7.2). WP5.

work.py is the ONLY place that builds AI tasks and saves AI results. The API-key runner (jobs_ai.py), the MCP tools
(WP6) and the desktop worker API (api_ai.py) all call these, so no path has its own prompt logic.

    ai_executor(user_id, kind) -> "server" | "mcp" | "desktop" | None
    queue_deep / queue_profile_draft / queue_topics / queue_retag_topics / queue_format_names
    create_output / quick_output                       (bodies in outputs.py)
    build_task(user_id, job_row, *, provider=None) -> AiTask JSON (4.7); third-party text wrapped (7.6)
    task_images(user_id, job_row, dest) -> [ai.Image]  enforces the 6.1.1 caps
    lease_tag_batch(conn, user_id, job_row, dest, n=None) -> [reel_id]   (prepare hook for jobs.lease_external)
    save_result(user_id, job_row, output, made_by, *, usage=None, finish=True) -> dict   raises Invalid(message)
    next_pending(user_id, kinds) -> Row | None
    mcp_tag_batch(user_id) -> (batch_id, text, [Image]) | None       whole result < 90,000 characters
    mcp_reel_task(user_id, reel_id=None) -> (job_id, text, [Image]) | None
    taxonomy_text(part) -> str                          (MCP get_taxonomy)

Transactions: never held across an AI call, ffmpeg or HTTP. Functions that take `conn=` join the caller's write
transaction (never open a second db.tx() inside one: SQLite would wait 5 s and fail).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, or_, select

from .. import schema_members as S
from .. import taxonomy
from ..ai import untrusted
from . import db, jobs, storage

log = logging.getLogger("reelvault.work")

AI_KINDS = (
    "tag_reels",
    "retag_topics",
    "analyze_reel",
    "name_formats",
    "make_output",
    "draft_profile",
    "suggest_topics",
)
NO_AI_MSG = "Choose how ReelVault uses AI in Settings > AI."
SCHEMAS = {
    "tag_reels": "QuickTagBatch",
    "retag_topics": "TopicRetagBatch",
    "analyze_reel": "ReelBreakdown",
    "name_formats": "FormatGuideBatch",
    "draft_profile": "ProfileDraft",
    "suggest_topics": "TopicList",
}
EFFORT = {
    "tag_reels": "low",
    "retag_topics": "low",
    "analyze_reel": "medium",
    "name_formats": "low",
    "make_output": "medium",
    "draft_profile": "medium",
    "suggest_topics": "low",
}
MAX_OUT = {
    "tag_reels": 6000,
    "retag_topics": 4000,
    "analyze_reel": 12000,
    "name_formats": 6000,
    "make_output": 16000,
    "draft_profile": 8000,
    "suggest_topics": 3000,
}
PRIORITY = {"deep": 20, "priority": 30, "sample": 60, "rest": 70, "none": 70}
API_TAG_BATCH = 10
DESKTOP_TAG_BATCH = 6
RETAG_BATCH = 40
MCP_BUDGET = 90_000
MCP_BASE = 3_000
MCP_MAX_REELS = 3
MCP_DEEP_TEXT = 18_000
LEASE_MCP_S = 1800
LEASE_DESKTOP_S = 900
REELS_FOR_TOPICS = 30


class Invalid(ValueError):
    """The AI output did not validate (pydantic or a cross-check). `message` goes back to the AI for one retry."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotReady(Exception):
    """A request cannot start yet (plain message for the member). The API turns it into a 4xx."""

    def __init__(self, message: str, code: str = "bad_input", status: int = 400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


# ============================================================================================== small reads


def _get(conn, table, user_id: str, row_id: str, id_col: str = "id"):
    return db.get_owned(conn, table, user_id, row_id, id_col=id_col)


def user_row(conn, user_id: str):
    return conn.execute(select(db.users).where(db.users.c.id == user_id)).first()


def profile_row(conn, user_id: str, brand_id: str = "me"):
    p = db.profiles
    return conn.execute(select(p).where(p.c.user_id == user_id, p.c.brand_id == brand_id)).first()


def digest(conn, user_id: str, brand_id: str = "me") -> dict | None:
    p = profile_row(conn, user_id, brand_id)
    return dict(p.digest) if p is not None and p.digest else None


def active_topics(conn, user_id: str, brand_id: str = "me") -> list:
    t = db.topics
    return conn.execute(
        select(t)
        .where(t.c.user_id == user_id, t.c.brand_id == brand_id, t.c.status == "active")
        .order_by(t.c.position, t.c.created_at)
    ).all()


def connections(conn, user_id: str) -> list:
    a = db.ai_connections
    return conn.execute(select(a).where(a.c.user_id == user_id).order_by(a.c.is_default.desc(), a.c.created_at)).all()


def main_connection(conn, user_id: str):
    rows = connections(conn, user_id)
    u = user_row(conn, user_id)
    tagging = u.tagging_conn_id if u is not None else None
    for r in rows:
        if r.is_default:
            return r
    rest = [r for r in rows if r.id != tagging]
    return rest[0] if rest else None


def connection_for(conn, user_id: str, kind: str):
    """The ai_connections row a server-side AI job uses (tagging connection for bulk tags, 3.13.1)."""
    u = user_row(conn, user_id)
    if u is None:
        return None
    if kind in ("tag_reels", "retag_topics") and u.tagging_conn_id:
        r = _get(conn, db.ai_connections, user_id, u.tagging_conn_id)
        if r is not None:
            return r
    return main_connection(conn, user_id)


# ============================================================================================== executors


def ai_executor(user_id: str, kind: str, *, conn=None) -> str | None:
    """3.13.1: tag/retag use the tagging connection (server) when set; otherwise the member's ai_mode."""

    def run(c):
        u = user_row(c, user_id)
        if u is None:
            return None
        if (
            kind in ("tag_reels", "retag_topics")
            and u.tagging_conn_id
            and _get(c, db.ai_connections, user_id, u.tagging_conn_id) is not None
        ):
            return "server"
        mode = u.ai_mode
        if mode == "api":
            return "server" if main_connection(c, user_id) is not None else None
        return {"claude_connector": "mcp", "desktop": "desktop"}.get(mode or "")

    if conn is not None:
        return run(conn)
    with db.read() as c:
        return run(c)


def enqueue_ai(
    user_id: str,
    kind: str,
    payload: dict | None = None,
    *,
    priority: int = 50,
    dedupe_key: str | None = None,
    conn=None,
    parent_id: str | None = None,
    state: str | None = None,
    message: str | None = None,
    error_code: str | None = None,
    run_after=None,
) -> str:
    """Enqueue an AI job on the member's executor; no AI yet -> paused with 'Choose how ReelVault uses AI'.
    run_after(executor) may return a start time (only server jobs use it)."""

    def run(c) -> str:
        ex = ai_executor(user_id, kind, conn=c)
        st, msg, code = state, message, error_code
        if ex is None:
            ex, st, msg, code = "server", "paused", NO_AI_MSG, "no_ai"
        jid = jobs.enqueue(
            user_id,
            kind,
            payload or {},
            executor=ex,
            priority=priority,
            dedupe_key=dedupe_key,
            conn=c,
            parent_id=parent_id,
            state=st,
            message=msg,
            run_after=run_after(ex) if callable(run_after) else run_after,
        )
        if code:
            c.execute(
                db.jobs.update().where(db.jobs.c.id == jid, db.jobs.c.error_code.is_(None)).values(error_code=code)
            )
        return jid

    if conn is not None:
        return run(conn)
    with db.tx() as c:
        return run(c)


RESUMABLE = ("no_ai", "bad_key", "quota", "retry", "waiting_ai")


def resume_ai_jobs(user_id: str, *, conn=None) -> int:
    """Move the member's not-running AI jobs to the current executor (PUT /api/ai/mode, key fixed). Paused jobs
    that only waited for AI (no_ai, bad_key, quota) start again; jobs paused for other reasons stay paused."""
    j = db.jobs

    def run(c) -> int:
        n = 0
        rows = c.execute(
            select(j).where(
                j.c.user_id == user_id, j.c.kind.in_(AI_KINDS), j.c.state.in_(("queued", "waiting", "paused"))
            )
        ).all()
        for r in rows:
            if r.state == "paused" and (r.error_code or "") not in RESUMABLE:
                continue
            ex = ai_executor(user_id, r.kind, conn=c)
            if ex is None:
                vals = dict(
                    executor="server", job_class="ai", state="paused", error_code="no_ai", error_message=NO_AI_MSG
                )
            else:
                vals = dict(
                    executor=ex,
                    job_class="ai" if ex == "server" else "external",
                    state="queued" if ex == "server" else "waiting",
                    error_code=None,
                    error_message=None,
                    run_after=None,
                    progress={"done": 0, "total": 0, "message": jobs.WAIT_MESSAGES.get(ex, "")},
                )
                if r.state == "paused":
                    vals["attempts"] = 0
            c.execute(j.update().where(j.c.id == r.id, j.c.state == r.state).values(**vals))
            if ex:
                db.upsert(
                    c,
                    db.job_turns,
                    {"user_id": user_id, "job_class": vals["job_class"], "last_served_at": jobs.EPOCH},
                    do_nothing=True,
                )
            n += 1
        c.execute(
            db.outputs.update()
            .where(db.outputs.c.user_id == user_id, db.outputs.c.status.in_(("queued", "waiting")))
            .values(status="waiting" if ai_executor(user_id, "make_output", conn=c) in ("mcp", "desktop") else "queued")
        )
        return n

    if conn is not None:
        return run(conn)
    with db.tx() as c:
        return run(c)


def release_dedupe(job_id: str, *, conn) -> None:
    """Free a running job's dedupe key so the next slot job can be queued while this one finishes."""
    j = db.jobs
    conn.execute(
        j.update()
        .where(j.c.id == job_id, j.c.dedupe_key.is_not(None))
        .values(dedupe_done=j.c.dedupe_key, dedupe_key=None)
    )


def back_to_waiting(job_id: str, *, conn) -> None:
    """External job with more chunks to do: hand it back to the member's Claude / desktop app (attempts reset)."""
    j = db.jobs
    conn.execute(
        j.update()
        .where(j.c.id == job_id, j.c.state == "running")
        .values(state="waiting", lease_owner=None, lease_until=None, attempts=0)
    )


# ============================================================================================== queueing


def _queued_tags(c, user_id: str) -> int:
    r = db.reels
    return int(
        c.execute(select(func.count()).select_from(r).where(r.c.user_id == user_id, r.c.tag_state == "queued")).scalar()
        or 0
    )


TAG_GATHER_S = 45   # an API-key slot waits this long for more reels while the member's media work still runs


def _media_pending(c, user_id: str) -> bool:
    r = db.reels
    return c.execute(select(r.c.id).where(
        r.c.user_id == user_id, r.c.tag_state == "none",
        or_(r.c.signals_state == "queued", r.c.media_state.in_(("queued", "fetching")))).limit(1)).first() is not None


def ensure_tag_slot(user_id: str, *, conn=None) -> str | None:
    """One tag_reels 'slot' job per member while reels wait for quick tags (3.13.2).

    API-key members pay per call, so a server slot with less than a full batch waits TAG_GATHER_S while more reels
    are still being processed (integration finding: a steady trickle gave 1-reel calls, 36 calls for 37 reels).
    Claude and desktop slots never wait: the member is looking at them."""

    def run(c):
        n = _queued_tags(c, user_id)
        if not n:
            return None

        def gather(ex):
            if ex == "server" and n < API_TAG_BATCH and _media_pending(c, user_id):
                return db.utcnow() + timedelta(seconds=TAG_GATHER_S)
            return None

        return enqueue_ai(user_id, "tag_reels", {}, priority=60, dedupe_key=f"tag_reels:{user_id}", conn=c,
                          run_after=gather)

    if conn is not None:
        return run(conn)
    with db.tx() as c:
        return run(c)


def _active_job_for_reel(c, reel_id: str) -> str | None:
    from .jobs_media import signals_version

    keys = [f"fetch_video:{reel_id}", f"process_reel:{reel_id}:v{signals_version()}", f"analyze_reel:{reel_id}"]
    got = c.execute(
        select(db.jobs.c.id).where(db.jobs.c.dedupe_key.in_(keys)).order_by(db.jobs.c.created_at.desc())
    ).first()
    return got[0] if got else None


def queue_deep(user_id: str, reel_ids: list[str]) -> list[str]:
    """Mark reels for a deep read and chain them (media -> signals -> deep). Returns the next job id per reel."""
    from . import pipeline

    ids: list[str] = []
    r = db.reels
    with db.tx() as c:
        owned = [
            row.id
            for row in c.execute(
                select(r.c.id, r.c.deep_state).where(r.c.user_id == user_id, r.c.id.in_(list(reel_ids)))
            ).all()
        ]
        c.execute(
            r.update()
            .where(r.c.user_id == user_id, r.c.id.in_(owned), r.c.deep_state != "done")
            .values(deep_state="queued")
        )
    for rid in owned:
        jid = pipeline.advance(user_id, rid)
        if not jid:
            with db.read() as c:
                jid = _active_job_for_reel(c, rid)
        if jid:
            ids.append(jid)
    return ids


def queue_profile_draft(user_id: str) -> str:
    p = db.profiles
    with db.tx() as c:
        prof = profile_row(c, user_id)
        if prof is None:
            c.execute(p.insert().values(user_id=user_id, brand_id="me", status="drafting", answers={}))
        elif prof.status in ("empty", "draft"):
            c.execute(p.update().where(p.c.user_id == user_id, p.c.brand_id == "me").values(status="drafting"))
        return enqueue_ai(user_id, "draft_profile", {}, priority=10, dedupe_key=f"draft_profile:{user_id}", conn=c)


def queue_topics(user_id: str, source: str = "profile") -> str:
    src = "reels" if source == "reels" else "profile"
    return enqueue_ai(
        user_id,
        "suggest_topics",
        {"from": src},
        priority=40 if src == "profile" else 70,
        dedupe_key=f"suggest_topics:{user_id}:{src}",
    )


def queue_retag_topics(user_id: str) -> str | None:
    """After a topic change: every tagged reel gets its topics re-done (text only, 40 reels per call)."""
    r = db.reels
    with db.tx() as c:
        c.execute(r.update().where(r.c.user_id == user_id, r.c.tag_state == "done").values(needs_topic_retag=True))
        n = c.execute(
            select(func.count()).select_from(r).where(r.c.user_id == user_id, r.c.needs_topic_retag.is_(True))
        ).scalar()
        if not n:
            return None
        return enqueue_ai(user_id, "retag_topics", {}, priority=50, dedupe_key=f"retag_topics:{user_id}", conn=c)


def queue_format_names(user_id: str, format_ids: list[str]) -> str | None:
    ids = list(dict.fromkeys(format_ids))[:8]
    if not ids:
        return None
    with db.tx() as c:
        f = db.formats
        own = [row.id for row in c.execute(select(f.c.id).where(f.c.user_id == user_id, f.c.id.in_(ids))).all()]
        if not own:
            return None
        return enqueue_ai(
            user_id,
            "name_formats",
            {"format_ids": own},
            priority=60,
            dedupe_key=f"name_formats:{user_id}:{','.join(sorted(own))}",
            conn=c,
        )


def create_output(user_id: str, kind: str, options: dict, *, purpose=None, brand_id: str = "me") -> tuple[str, str]:
    from . import outputs

    return outputs.create(user_id, kind, options, purpose=purpose, brand_id=brand_id)


def quick_output(user_id: str, action: str, **refs) -> tuple[str, str]:
    from . import outputs

    return outputs.quick(user_id, action, **refs)


def unblock_outputs(user_id: str, reel_id: str) -> int:
    from . import outputs

    return outputs.unblock(user_id, reel_id)


# ============================================================================================== text helpers


def mmss(t: float | None) -> str:
    if t is None:
        return "?"
    t = max(0.0, float(t))
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def _num(v) -> str:
    if v is None:
        return "?"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 100 else f"{v:,.0f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


VISUAL_SKIP = ("per_frame", "sheet_bytes", "frames", "version", "local_vf", "local_recipe", "face_detector")


def visual_text(visual: dict | None) -> str:
    if not visual:
        return "VisualStats: none (no frames)."
    nums = " · ".join(
        f"{k} {_num(v)}"
        for k, v in visual.items()
        if k not in VISUAL_SKIP and v is not None and not isinstance(v, (dict, list))
    )
    guess = ", ".join(f"{g.get('id')} (p {_num(g.get('p'))})" for g in visual.get("local_vf") or []) or "none"
    rec = visual.get("local_recipe") or {}
    rec_t = ", ".join(f"{k} {_num(v)}" for k, v in rec.items()) or "none"
    return f"VisualStats: {nums}\nLocal format guess: {guess} · local recipe: {rec_t}"


def edit_text(edit: dict | None) -> str:
    if not edit:
        return "EditStats: none."
    e = edit
    return (
        f"EditStats: {_num(e.get('duration'))} s ({e.get('length_bucket')}) · {_num(e.get('cuts'))} cuts, "
        f"{_num(e.get('cuts_per_10s'))} per 10 s ({e.get('cut_pace')}), hook {_num(e.get('hook_cuts_per_10s'))} "
        f"per 10 s · average shot {_num(e.get('avg_shot_s'))} s · speech {_num(e.get('speech_ratio'))} · "
        f"{_num(e.get('wpm'))} words per minute · {_num(e.get('words_first_3s'))} words in the first 3 s · "
        f"{_num(e.get('silence_gaps'))} silence gaps"
    )


def sound_summary_text(sound: dict | None) -> str:
    if not sound:
        return "Sound: no sound map."
    s = sound.get("summary") or {}
    m = sound.get("music") or {}
    counts = ", ".join(f"{k} {v}" for k, v in (s.get("sfx_counts") or {}).items()) or "none"
    ig = sound.get("ig_audio") or {}
    ig_t = f"{ig.get('type', 'unknown')}" + (" (reused)" if ig.get("is_reused") else "")
    return (
        f"Sound ({sound.get('detector', '?')}): SFX {_num(s.get('sfx_per_10s'))} per 10 s ({counts}); cuts with "
        f"SFX {_num(s.get('cuts_with_sfx_pct'))}%; first SFX at {_num(s.get('first_sfx_s'))} s; hook hit "
        f"{_num(s.get('hook_hit'))}; music {_num(m.get('coverage_pct'))}% of the reel, ducked "
        f"{_num(m.get('duck_db'))} dB; speech {_num(sound.get('speech_coverage_pct'))}%; TTS likely "
        f"{_num(sound.get('tts_likely'))}; loudness {_num(sound.get('lufs'))} LUFS; local pattern hints: "
        f"{', '.join(sound.get('pattern_hints') or []) or 'none'}; Instagram audio: {ig_t}"
    )


def cue_list(sound: dict | None, limit: int = 40) -> list[str]:
    if not sound:
        return []
    out: list[tuple[float, str]] = []
    for e in sound.get("events") or []:
        lab = e.get("label") or "hit"
        conf = e.get("confidence") or "low"
        # A low-confidence event is only "a sound here": naming it (whoosh, click) made models build cue sheets
        # and briefs on guesses (fix round 1).
        name = lab if conf == "high" else "SFX hit"
        where = " on cut" if e.get("on_cut") is not None else ""
        span = f"-{mmss(e.get('t_end'))}" if e.get("t_end") and lab in ("whoosh", "riser") and conf == "high" else ""
        out.append((float(e.get("t") or 0), f"{mmss(e.get('t'))}{span} {name}{where} ({conf})"))
    for a, b in sound.get("silences") or []:
        out.append((float(a), f"{mmss(a)}-{mmss(b)} silence"))
    for a, b in (sound.get("music") or {}).get("spans") or []:
        out.append((float(a), f"{mmss(a)}-{mmss(b)} music"))
    out.sort(key=lambda x: x[0])
    return [t for _, t in out[:limit]]


def cue_times(sound: dict | None) -> list[float]:
    if not sound:
        return []
    ts: list[float] = []
    for e in sound.get("events") or []:
        ts.append(float(e.get("t") or 0))
        if e.get("t_end"):
            ts.append(float(e["t_end"]))
    for a, b in sound.get("silences") or []:
        ts += [float(a), float(b)]
    for a, b in (sound.get("music") or {}).get("spans") or []:
        ts += [float(a), float(b)]
    return ts


def tiles_text(tiles: list[dict]) -> str:
    return " · ".join(f"{t['tile']}={_num(round(float(t['t']), 1))}s {t.get('kind', '')}".strip() for t in tiles)


def metrics_text(reel) -> str:
    bits = []
    for k in ("views", "likes", "comments", "shares"):
        v = getattr(reel, k, None)
        if v is not None:
            bits.append(f"{v:,} {k}")
    if reel.score is not None:
        bits.append(f"{reel.score:.1f}x this creator's normal {reel.basis or 'views'}")
    if reel.band:
        bits.append(f"band {reel.band}")
    return " · ".join(bits) or "no public numbers"


def topics_text(rows) -> str:
    if not rows:
        return "(none yet: return topics [] for every reel; you may suggest one new_topic)"
    return "\n".join(f"- {t.slug}: {t.label}" + (f" ({t.description})" if t.description else "") for t in rows)


def profile_text(d: dict | None) -> str:
    """ProfileDigest as bullets (9.4). The member wrote it (trusted), but quotes from sources may sit inside, so
    it is escaped the same way."""
    if not d:
        return "(no approved profile yet)"
    caps = d.get("capabilities") or {}
    can = [k for k, v in caps.items() if v is True]
    cant = [k for k, v in caps.items() if v is False]
    voice = d.get("voice") or {}
    lines = [
        f"- Niche: {d.get('niche_label') or ''}",
        f"- Who we help: {d.get('who_we_help') or ''}",
        f"- Dream outcome: {d.get('dream_outcome') or ''}",
        "- Customer pains (copy exactly when asked): " + " | ".join(d.get("top_pains") or []),
        "- Tried before: " + " | ".join(d.get("failed_attempts") or []),
        "- Objections: " + " | ".join(d.get("objections") or []),
        f"- Offer: {d.get('offer') or ''}",
        "- Proof (copy exactly when asked): " + " | ".join(d.get("proof") or []),
        f"- Different because: {d.get('difference') or ''}",
        "- Voice: tone "
        + ", ".join(voice.get("tone") or [])
        + "; always say "
        + ", ".join(voice.get("always") or [])
        + "; never say "
        + ", ".join(voice.get("never") or []),
        f"- Main CTA: {d.get('main_cta') or ''} · Platforms: {d.get('platforms') or ''}",
        "- Can make: "
        + (", ".join(can) or "unknown")
        + ("; can't: " + ", ".join(cant) if cant else "")
        + f"; hours per week {caps.get('hours_per_week', '?')}; editor {_num(caps.get('editor'))}",
        "- Topics covered recently: " + ", ".join(d.get("covered_topics") or []),
        "- Hard no's: " + " | ".join(d.get("hard_nos") or []),
        f"- Language: {d.get('language') or 'en'}",
    ]
    samples = voice.get("samples") or []
    text = "\n".join(lines)
    if samples:
        text += "\n- Voice samples (the member's own first lines): " + " | ".join(samples[:5])
    return untrusted.escape(text)


# ============================================================================================== prompts

PROMPTS = Path(__file__).resolve().parent.parent / "ai" / "prompts"
_PROMPT_CACHE: dict[str, str] = {}


def prompt_file(name: str) -> str:
    if name not in _PROMPT_CACHE:
        raw = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
        shared = (PROMPTS / "_shared.md").read_text(encoding="utf-8")
        _PROMPT_CACHE[name] = raw.replace("{{include _shared.md}}", shared.strip())
    return _PROMPT_CACHE[name]


def render_prompt(name: str, values: dict[str, str]) -> tuple[str, str]:
    """(system, prompt) from ai/prompts/<name>.md: text before '---task---' is the system prompt."""
    raw = prompt_file(name)
    sys_t, _, task_t = raw.partition("---task---")
    base = {
        "taxonomy_vf": taxonomy.prompt_block("visual"),
        "taxonomy_sd": taxonomy.prompt_block("sound"),
        "taxonomy_ep": taxonomy.prompt_block("edit"),
        "taxonomy_lists": taxonomy.prompt_block("lists"),
    }
    base.update(values)

    def fill(t: str) -> str:
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(base.get(m.group(1), "")), t).strip()

    return fill(sys_t), fill(task_t or "{{data}}")


# ============================================================================================== images


def _reel_dir(user_id: str, reel_id: str) -> Path:
    return storage.member_path(user_id, f"reels/{reel_id}")


def _has_frames(user_id: str, reel_id: str) -> bool:
    try:
        return (_reel_dir(user_id, reel_id) / "frames" / "index.json").exists()
    except storage.UnsafePath:
        return False


def dest_for(provider) -> str:
    """6.1.1: providers with max_images < 8 or a small per-image cap get the 'small' sheets and frames."""
    if provider is None:
        return "paid_api"
    if int(getattr(provider, "max_images", 10)) < 8:
        return "small"
    return "paid_api"


_IMG_CACHE: dict[tuple, tuple[float, list]] = {}
_IMG_LOCK = threading.Lock()


def _cache_get(key: tuple):
    with _IMG_LOCK:
        hit = _IMG_CACHE.get(key)
        if hit and time.monotonic() - hit[0] < 1800:
            return hit[1]
    return None


def _cache_put(key: tuple, val: list) -> None:
    with _IMG_LOCK:
        if len(_IMG_CACHE) > 32:
            for k in sorted(_IMG_CACHE, key=lambda k: _IMG_CACHE[k][0])[:8]:
                _IMG_CACHE.pop(k, None)
        _IMG_CACHE[key] = (time.monotonic(), val)


def _cap_check(name: str, data: bytes, cap: int) -> None:
    if len(data) > cap:
        log.warning("image %s is %d bytes, over its %d cap (6.1.1)", name, len(data), cap)


def _tag_images(user_id: str, reel_ids: list[str], dest: str) -> list:
    from ..ai.base import Image
    from ..signals import frames

    out = []
    for rid in reel_ids:
        if not _has_frames(user_id, rid):
            continue
        try:
            data = frames.sheet_for(_reel_dir(user_id, rid), dest)
        except (FileNotFoundError, OSError) as e:
            log.warning("no sheet for %s: %s", rid, e)
            continue
        _cap_check(f"sheet_{rid}", data, frames.SHEETS[dest].cap)
        out.append(Image(data=data, label=f"Contact sheet of reel {rid}", name=f"sheet_{rid}.jpg"))
    return out


def _deep_images(user_id: str, reel_id: str, dest: str, max_images: int | None = None) -> list:
    from ..ai.base import Image
    from ..signals import frames

    if not _has_frames(user_id, reel_id):
        return []
    rd = _reel_dir(user_id, reel_id)
    out = []
    if dest == "mcp":
        sheet = frames.sheet_for(rd, "mcp", cap_bytes=frames.MCP_DEEP_SHEET_CAP)
        _cap_check("mcp sheet", sheet, frames.MCP_DEEP_SHEET_CAP)
        out.append(Image(data=sheet, label="Contact sheet (6 key frames)", name=f"sheet_{reel_id}.jpg"))
        picks = frames.deep_frames(rd, "mcp", 2)
    elif dest == "small":
        n = max(1, int(max_images or 3) - 1)
        sheet = frames.sheet_for(rd, "small")
        out.append(Image(data=sheet, label="Contact sheet (6 key frames)", name=f"sheet_{reel_id}.jpg"))
        picks = frames.deep_frames(rd, "small", n)
    else:
        n = max(1, min(10, int(max_images or 8), 8))
        picks = frames.deep_frames(rd, "paid_api", n)
    cap = frames.FRAMES[dest].cap
    for f, data in picks:
        _cap_check(f"frame {f.get('n')}", data, cap)
        base = Path(f["path"]).name
        out.append(
            Image(
                data=data,
                name=f"{reel_id}_{base}",
                label=f"Frame {int(f['n']) + 1} at {round(float(f['t']), 1)} s (shot {f.get('shot', '?')}, "
                f"{f.get('kind', 'body')})",
            )
        )
    return out


def task_images(user_id: str, job_row, dest: str, *, max_images: int | None = None) -> list:
    """The images of a task for one destination (6.1.1). Sheets/frames come from WP3's frames module, which steps
    down until each byte cap is met; this function re-checks the caps and never returns more than the plan."""
    payload = job_row.payload or {}
    key = (job_row.id, dest, max_images, json.dumps(payload, sort_keys=True, default=str))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    if job_row.kind == "tag_reels":
        imgs = _tag_images(user_id, payload.get("reel_ids") or [], dest)
    elif job_row.kind == "analyze_reel":
        imgs = _deep_images(user_id, payload.get("reel_id") or "", dest, max_images)
    else:
        imgs = []
    _cache_put(key, imgs)
    return imgs


def image_bytes(user_id: str, job_row, name: str) -> bytes | None:
    """For GET /api/worker/files/<job_id>/<name>: the bytes of one image the job's task listed."""
    dest = (job_row.payload or {}).get("dest") or "paid_api"
    for im in task_images(user_id, job_row, dest):
        if im.name == name:
            return im.data
    return None


# ============================================================================================== tag batches


def _priority_order(r):
    return (case((r.c.tier == "priority", 0), else_=1), r.c.score.desc().nulls_last(), r.c.first_seen_at)


def lease_tag_batch(
    conn, user_id: str, job_row, dest: str, n: int | None = None, only: list[str] | None = None
) -> list[str]:
    """Form a tag batch at lease time (3.13.2): reels already leased to this job are kept (a retried job), else up
    to n queued reels are moved to tag_state='leased'. Writes {"reel_ids", "dest"} into the job payload."""
    r = db.reels
    n = int(n or API_TAG_BATCH)
    ids = [
        x[0]
        for x in conn.execute(
            select(r.c.id)
            .where(r.c.user_id == user_id, r.c.tag_batch == job_row.id, r.c.tag_state == "leased")
            .order_by(*_priority_order(r))
        )
    ]
    if not ids:
        q = select(r.c.id).where(r.c.user_id == user_id, r.c.tag_state == "queued")
        if only is not None:
            q = q.where(r.c.id.in_(only))
        ids = [x[0] for x in conn.execute(q.order_by(*_priority_order(r)).limit(n))]
        if only is not None:
            ids = [i for i in only if i in ids]
        if ids:
            conn.execute(
                r.update()
                .where(r.c.user_id == user_id, r.c.id.in_(ids), r.c.tag_state == "queued")
                .values(tag_state="leased", tag_batch=job_row.id)
            )
    payload = dict(job_row.payload or {})
    payload.update({"reel_ids": ids, "dest": dest})
    conn.execute(
        db.jobs.update()
        .where(db.jobs.c.id == job_row.id)
        .values(payload=payload, progress={"done": 0, "total": len(ids), "message": f"Tagging {len(ids)} reels"})
    )
    return ids


def release_tag_batch(conn, job_row, *, state: str = "queued") -> int:
    r = db.reels
    return conn.execute(
        r.update().where(r.c.tag_batch == job_row.id, r.c.tag_state == "leased").values(tag_state=state, tag_batch=None)
    ).rowcount


def lease_retag_batch(conn, user_id: str, job_row) -> list[str]:
    r = db.reels
    ids = list((job_row.payload or {}).get("reel_ids") or [])
    if not ids:
        ids = [
            x[0]
            for x in conn.execute(
                select(r.c.id)
                .where(r.c.user_id == user_id, r.c.needs_topic_retag.is_(True), r.c.tag_state == "done")
                .order_by(r.c.first_seen_at)
                .limit(RETAG_BATCH)
            )
        ]
        conn.execute(
            db.jobs.update()
            .where(db.jobs.c.id == job_row.id)
            .values(payload={**(job_row.payload or {}), "reel_ids": ids})
        )
    return ids


def prepare_external(user_id: str, executor: str):
    """prepare(conn, job) for jobs.lease_external: forms tag/retag batches; returns False (undo) when empty."""

    def prep(conn, job):
        dest = "mcp" if executor == "mcp" else "paid_api"
        if job.kind == "tag_reels":
            n = DESKTOP_TAG_BATCH if executor == "desktop" else MCP_MAX_REELS
            return bool(lease_tag_batch(conn, user_id, job, dest, n=n))
        if job.kind == "retag_topics":
            return bool(lease_retag_batch(conn, user_id, job))
        payload = dict(job.payload or {})
        if payload.get("dest") != dest:
            payload["dest"] = dest
            conn.execute(db.jobs.update().where(db.jobs.c.id == job.id).values(payload=payload))
        return True

    return prep


# ============================================================================================== task builders


def _reels_by_id(c, user_id: str, ids: list[str]) -> dict:
    r = db.reels
    return {row.id: row for row in c.execute(select(r).where(r.c.user_id == user_id, r.c.id.in_(list(ids))))}


def _signals(c, user_id: str, ids: list[str]) -> dict:
    s = db.reel_signals
    return {row.reel_id: row for row in c.execute(select(s).where(s.c.user_id == user_id, s.c.reel_id.in_(list(ids))))}


def _handles(c, user_id: str, account_ids: list[str]) -> dict:
    a = db.accounts
    return {
        row.id: row.handle
        for row in c.execute(
            select(a.c.id, a.c.handle).where(a.c.user_id == user_id, a.c.id.in_(list(set(account_ids))))
        )
    }


def reel_tag_block(reel, sig, handle: str | None, *, image_name: str | None, tiles: list[dict] | None) -> str:
    """The per-reel text of a tag batch (≤ 3,500 characters for MCP)."""
    head = f"=== Reel {reel.id}" + (f" (@{handle})" if handle else "") + " ==="
    lines = [head, f"Numbers: {metrics_text(reel)} · length {_num(reel.duration)} s"]
    if image_name:
        lines.append(f'Contact sheet: image "{image_name}". Tiles: {tiles_text(tiles or [])}')
    else:
        lines.append(
            "No frames: the video could not be read. Tag from the caption, numbers and transcript; keep "
            "visual_confidence at 0.3 or less."
        )
    if sig is not None:
        lines += [visual_text(sig.visual), edit_text(sig.edit), sound_summary_text(sig.sound)]
    tr = (sig.transcript or {}).get("text") if sig is not None else ""
    lines.append("Caption: " + untrusted.wrap(reel.caption, "caption", 600, reel=reel.id))
    lines.append("Transcript start: " + untrusted.wrap(tr, "transcript", 1200, reel=reel.id))
    return "\n".join(lines)


def _tiles(user_id: str, reel_id: str, dest: str) -> list[dict]:
    from ..signals import frames

    try:
        return frames.sheet_tiles(_reel_dir(user_id, reel_id), dest)
    except (OSError, ValueError, storage.UnsafePath):
        return []


def _task(
    kind: str,
    system: str,
    prompt: str,
    images: list,
    dest: str,
    schema_name: str,
    provider_name: str,
    extra: dict | None = None,
) -> dict:
    from .. import ai

    model = S.model_for(schema_name)
    out = {
        "task": kind,
        "system": system,
        "prompt": prompt,
        "images": [{"name": im.name, "t": _img_t(im), "label": im.label} for im in images],
        "image_dest": dest,
        "schema_name": schema_name,
        "schema": ai.portable_schema(model, provider_name),
        "effort": EFFORT.get(kind, "medium"),
        "max_output_tokens": MAX_OUT.get(kind, 8000),
    }
    if extra:
        out.update(extra)
    return out


def _img_t(im) -> float | None:
    m = re.search(r"_(\d{3}\.\d{2})\.jpg$", im.name or "")
    return float(m.group(1)) if m else None


def _build_tag(c, user_id: str, job, dest: str, vision: bool) -> tuple[str, str, list]:
    ids = list((job.payload or {}).get("reel_ids") or [])
    reels = _reels_by_id(c, user_id, ids)
    sigs = _signals(c, user_id, ids)
    handles = _handles(c, user_id, [r.account_id for r in reels.values()])
    topics = active_topics(c, user_id)
    blocks = []
    for rid in ids:
        reel = reels.get(rid)
        if reel is None:
            continue
        has = vision and _has_frames(user_id, rid)
        blocks.append(
            reel_tag_block(
                reel,
                sigs.get(rid),
                handles.get(reel.account_id),
                image_name=f"sheet_{rid}.jpg" if has else None,
                tiles=_tiles(user_id, rid, dest) if has else None,
            )
        )
    data = (
        f"Batch of {len(blocks)} reels. Reel ids: {', '.join(ids)}.\n\n"
        + "\n\n".join(blocks)
        + "\n\nReturn one tag per reel id, each id once."
    )
    system, prompt = render_prompt(
        "tag_reels", {"topics": topics_text(topics), "field_guide": S.field_guide(S.QuickTagBatch), "data": data}
    )
    return system, prompt, ids


def deep_text(c, user_id: str, reel, *, max_chars: int = MCP_DEEP_TEXT, images: list | None = None) -> str:
    sig = _signals(c, user_id, [reel.id]).get(reel.id)
    a = db.analyses
    quick = c.execute(
        select(a.c.data).where(a.c.reel_id == reel.id, a.c.user_id == user_id, a.c.level == "quick")
    ).first()
    handle = _handles(c, user_id, [reel.account_id]).get(reel.account_id)
    lines = [
        f"=== Reel {reel.id}" + (f" (@{handle})" if handle else "") + " ===",
        f"Instagram: {reel.url}",
        f"Numbers: {metrics_text(reel)} · length {_num(reel.duration)} s",
    ]
    if images:
        lines.append("Images: " + "; ".join(f'"{im.name}" = {im.label}' for im in images))
    else:
        lines.append("No frames: the video could not be read. Say so in confidence_notes and keep confidences low.")
    if sig is not None:
        lines += [visual_text(sig.visual), edit_text(sig.edit), sound_summary_text(sig.sound)]
        cues = cue_list(sig.sound)
        lines.append("Local cue list (use these times only):\n" + ("\n".join(cues) if cues else "(no cues found)"))
    if quick is not None:
        q = dict(quick.data or {})
        q.pop("hook_bank", None)
        lines.append(
            "Quick tags: " + untrusted.wrap(json.dumps(q, ensure_ascii=False), "quick_tags", 2500, reel=reel.id)
        )
    tr = (sig.transcript or {}) if sig is not None else {}
    segs = tr.get("segments") or []
    timed = "\n".join(f"[{mmss(s.get('start'))}] {s.get('text', '').strip()}" for s in segs) or tr.get("text") or ""
    lines.append("Timed transcript: " + untrusted.wrap(timed, "transcript", 6000, reel=reel.id))
    lines.append("Caption: " + untrusted.wrap(reel.caption, "caption", 2000, reel=reel.id))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 200] + "\n[cut to fit]"
    return text


def _build_deep(c, user_id: str, job, images: list) -> tuple[str, str]:
    rid = (job.payload or {}).get("reel_id")
    reel = _get(c, db.reels, user_id, rid)
    if reel is None:
        raise Invalid("This reel is gone.")
    data = deep_text(c, user_id, reel, images=images) + f"\n\nReturn the ReelBreakdown for reel_id {rid}."
    system, prompt = render_prompt(
        "analyze_reel",
        {
            "topics": topics_text(active_topics(c, user_id)),
            "profile": profile_text(digest(c, user_id)),
            "field_guide": S.field_guide(S.ReelBreakdown),
            "data": data,
        },
    )
    return system, prompt


QUESTIONS = [
    ("q1", "Who do you help?"),
    ("q2", "Your best client so far"),
    ("q3", "Their top 3 problems, in their words (list)"),
    ("q4", "What have they already tried that didn't work?"),
    ("q5", "What do they really want?"),
    ("q6", "Why do people say 'no' or 'not now'? (list)"),
    ("q7", "What do you sell?"),
    ("q8", "Your proof"),
    ("q9", "What makes you different?"),
    ("q10", "How do you talk?"),
    ("q11", "Where do you post, and what should a viewer do next?"),
    ("q12", "What can you actually make?"),
    ("q13", "Hard no's"),
    ("q14", "Who are your competitors or role models? (list of Instagram handles)"),
]
LIST_QUESTIONS = ("q3", "q6", "q14")


def _build_draft(c, user_id: str) -> tuple[str, str]:
    src = db.profile_sources
    rows = c.execute(select(src).where(src.c.user_id == user_id, src.c.status == "ok").order_by(src.c.created_at)).all()
    prof = profile_row(c, user_id)
    answers = dict(prof.answers or {}) if prof is not None else {}
    blocks = []
    for s in rows:
        kind = {
            "website": "site",
            "skool_group": "skool",
            "skool_profile": "skool",
            "linkedin_pdf": "linkedin",
            "linkedin_text": "linkedin",
            "linkedin_page": "linkedin",
            "instagram_bio": "bio",
        }.get(s.kind, "paste")
        blocks.append(
            f"Source {s.id} ({s.kind}{', ' + s.url if s.url else ''}):\n"
            + untrusted.wrap(s.text, kind, untrusted.cap_for(kind, long=True), id=s.id)
        )
    known = []
    for qid, _ in QUESTIONS:
        a = answers.get(qid) or {}
        if a.get("confirmed") and a.get("value"):
            v = a["value"]
            known.append(
                f"- {qid} (the member answered): {untrusted.escape(' | '.join(v) if isinstance(v, list) else str(v))}"
            )
    data = (
        "Sources:\n\n"
        + ("\n\n".join(blocks) if blocks else "(no sources)")
        + (
            "\n\nAnswers the member already gave (keep them; don't contradict them):\n" + "\n".join(known)
            if known
            else ""
        )
        + "\n\nReturn the ProfileDraft."
    )
    system, prompt = render_prompt(
        "draft_profile",
        {
            "questions": "\n".join(f"- {q}: {label}" for q, label in QUESTIONS),
            "field_guide": S.field_guide(S.ProfileDraft),
            "data": data,
        },
    )
    return system, prompt


def _build_topics(c, user_id: str, source: str) -> tuple[str, str]:
    t = db.topics
    current = c.execute(select(t).where(t.c.user_id == user_id, t.c.status.in_(("active", "suggested")))).all()
    if source == "reels":
        a = db.analyses
        rows = c.execute(
            select(a.c.reel_id, a.c.data)
            .where(a.c.user_id == user_id, a.c.level == "quick")
            .order_by(a.c.updated_at.desc())
            .limit(50)
        ).all()
        samples = []
        for r in rows:
            d = r.data or {}
            samples.append(
                f"- {d.get('visual_subject', '')} · {d.get('one_line', '')}"
                + (f" · suggested topic: {d.get('new_topic')}" if d.get("new_topic") else "")
            )
        data = (
            "What competitor reels show (50 samples):\n"
            + untrusted.wrap("\n".join(samples), "quick_tags", 9000)
            + "\n\nReturn the TopicList."
        )
        how = "Suggest up to 5 NEW topics that many of these reels share and the current list misses. [] is fine."
    else:
        data = (
            "The member's profile:\n"
            + profile_text(digest(c, user_id) or _draft_digest(c, user_id))
            + "\n\nReturn the TopicList."
        )
        how = "Suggest up to 10 topics this member's customers care about. Fewer is fine."
    system, prompt = render_prompt(
        "suggest_topics",
        {"topics": topics_text(current), "how_many": how, "field_guide": S.field_guide(S.TopicList), "data": data},
    )
    return system, prompt


def _draft_digest(c, user_id: str) -> dict | None:
    """A rough digest from unapproved answers (topic suggestions may run before approval)."""
    prof = profile_row(c, user_id)
    if prof is None or not prof.answers:
        return None
    ans = prof.answers

    def val(q):
        v = (ans.get(q) or {}).get("value")
        return v if v else ""

    pains = val("q3")
    return {
        "who_we_help": val("q1"),
        "top_pains": pains if isinstance(pains, list) else [pains] if pains else [],
        "offer": val("q7"),
        "niche_label": (ans.get("_draft") or {}).get("niche_label", ""),
    }


def _build_retag(c, user_id: str, job) -> tuple[str, str, list[str]]:
    ids = list((job.payload or {}).get("reel_ids") or [])
    reels = _reels_by_id(c, user_id, ids)
    a = db.analyses
    quick = {
        row.reel_id: row.data or {}
        for row in c.execute(
            select(a.c.reel_id, a.c.data).where(a.c.user_id == user_id, a.c.level == "quick", a.c.reel_id.in_(ids))
        )
    }
    lines = []
    for rid in ids:
        if rid not in reels:
            continue
        q = quick.get(rid, {})
        lines.append(
            f"- {rid}: shows {untrusted.wrap(q.get('visual_subject', ''), 'quick_tags', 120)}; "
            f"does {untrusted.wrap(q.get('one_line', ''), 'quick_tags', 160)}; caption "
            + untrusted.wrap(reels[rid].caption, "caption", 200, reel=rid)
        )
    data = f"{len(lines)} reels:\n" + "\n".join(lines) + "\n\nReturn one entry per reel id."
    system, prompt = render_prompt(
        "retag_topics",
        {
            "topics": topics_text(active_topics(c, user_id)),
            "field_guide": S.field_guide(S.TopicRetagBatch),
            "data": data,
        },
    )
    return system, prompt, ids


def _build_names(c, user_id: str, job) -> tuple[str, str]:
    f, a = db.formats, db.analyses
    ids = list((job.payload or {}).get("format_ids") or [])
    rows = c.execute(select(f).where(f.c.user_id == user_id, f.c.id.in_(ids))).all()
    blocks = []
    for fm in rows:
        ex = list(fm.example_reels or [])[:6]
        q = c.execute(
            select(a.c.reel_id, a.c.data, a.c.sound_primary).where(
                a.c.user_id == user_id, a.c.reel_id.in_(ex), a.c.level == "quick"
            )
        ).all()
        sigs = _signals(c, user_id, ex)
        examples, sounds, cues, edits = [], {}, [], []
        for row in q:
            d = row.data or {}
            examples.append(
                f"{d.get('visual_subject', '')} · {d.get('one_line', '')} · captions "
                f"{(d.get('recipe') or {}).get('caption_look', '')} · hook: {d.get('hook_line', '')}"
            )
            if row.sound_primary:
                sounds[row.sound_primary] = sounds.get(row.sound_primary, 0) + 1
        for rid in ex:
            s = sigs.get(rid)
            if s is None:
                continue
            cues += cue_list(s.sound, limit=6)[:4]
            if s.edit:
                edits.append(s.edit)
        med = {}
        for k in ("cuts_per_10s", "avg_shot_s", "wpm", "duration"):
            vals = sorted(float(e[k]) for e in edits if e.get(k) is not None)
            if vals:
                med[k] = vals[len(vals) // 2]
        blocks.append(
            "\n".join(
                [
                    f"=== Format {fm.id} ===",
                    f"Visual format: {fm.vf} {taxonomy.label(fm.vf)}",
                    "Recipe: " + json.dumps(fm.recipe or {}, ensure_ascii=False),
                    "Stats: "
                    + json.dumps(
                        {
                            k: v
                            for k, v in (fm.stats or {}).items()
                            if k in ("n", "k", "lift", "typical", "evidence", "rising")
                        }
                    ),
                    "Example reels: " + untrusted.wrap("\n".join(examples), "quick_tags", 2500, format=fm.id),
                    "Sound pattern counts: "
                    + (", ".join(f"{k} {taxonomy.label(k)} x{v}" for k, v in sounds.items()) or "none"),
                    "Cue examples: " + ("; ".join(cues[:10]) or "none"),
                    "EditStats medians: " + (", ".join(f"{k} {_num(v)}" for k, v in med.items()) or "none"),
                ]
            )
        )
    data = "\n\n".join(blocks) + f"\n\nReturn one FormatGuide per format id: {', '.join(r.id for r in rows)}."
    return render_prompt("name_formats", {"field_guide": S.field_guide(S.FormatGuideBatch), "data": data})


def build_task(user_id: str, job_row, *, provider=None, dest: str | None = None) -> dict:
    """The AiTask JSON of 4.7 for one job (one call). The job must already hold its batch (tag/retag)."""
    from . import outputs

    kind = job_row.kind
    executor = job_row.executor
    if dest is None:
        dest = "mcp" if executor == "mcp" else ("paid_api" if executor == "desktop" else dest_for(provider))
    pname = getattr(provider, "name", None) or ("anthropic" if executor in ("mcp", "desktop") else "anthropic")
    vision = bool(getattr(provider, "vision", True))
    max_images = getattr(provider, "max_images", None)
    if kind == "make_output":
        return outputs.build_brief(user_id, job_row, provider_name=pname)
    images: list = []
    with db.read() as c:
        if kind == "tag_reels":
            system, prompt, _ = _build_tag(c, user_id, job_row, dest, vision)
        elif kind == "analyze_reel":
            images = task_images(user_id, job_row, dest, max_images=max_images) if vision else []
            system, prompt = _build_deep(c, user_id, job_row, images)
        elif kind == "draft_profile":
            system, prompt = _build_draft(c, user_id)
        elif kind == "suggest_topics":
            system, prompt = _build_topics(c, user_id, (job_row.payload or {}).get("from") or "profile")
        elif kind == "retag_topics":
            system, prompt, _ = _build_retag(c, user_id, job_row)
        elif kind == "name_formats":
            system, prompt = _build_names(c, user_id, job_row)
        else:
            raise Invalid(f"Unknown AI task {kind}.")
    if kind == "tag_reels" and vision:
        images = task_images(user_id, job_row, dest)
    return _task(kind, system, prompt, images, dest, SCHEMAS[kind], pname)


# ============================================================================================== saving


def _made_by(made_by: dict | None) -> dict:
    m = dict(made_by or {})
    m.setdefault("via", "api")
    return {k: m[k] for k in ("via", "provider", "model") if m.get(k)}


def _validate(model_cls, output) -> Any:
    from pydantic import ValidationError

    if isinstance(output, model_cls):
        return output
    if isinstance(output, str):
        from ..ai.base import extract_json

        try:
            output = extract_json(output)
        except ValueError as e:
            raise Invalid(str(e)) from None
    try:
        return model_cls.model_validate(output)
    except ValidationError as e:
        raise Invalid(str(e)[:1500]) from None


def _norm_topics(slugs: list[str], active: set[str]) -> list[str]:
    out = []
    for s in slugs or []:
        k = S.slugify(s) if isinstance(s, str) else ""
        if k in active and k not in out:
            out.append(k)
    return out[:3]


def _hook_bank(reel, sig, tags) -> dict | None:
    if reel.score is None or reel.score < 2:
        return None
    tr = ((sig.transcript or {}).get("text") or "") if sig is not None else ""
    first = re.split(r"(?<=[.!?])\s", tr.strip(), maxsplit=1)[0][:200] if tr else ""
    vis = (sig.visual or {}) if sig is not None else {}
    return {
        "spoken": first or tags.hook_line,
        "on_screen_text": tags.hook_line if tags.has_text_hook else "",
        "visual": tags.visual_subject,
        "hook_type": tags.hook_type,
        "level": "quick",
        "text_frames_share": vis.get("text_frames_share"),
    }


def _upsert_analysis(
    c, user_id: str, reel_id: str, level: str, data: dict, cols: dict, made_by: dict, usage=None
) -> None:
    a = db.analyses
    now = db.utcnow()
    row = c.execute(select(a.c.id).where(a.c.reel_id == reel_id, a.c.level == level)).first()
    vals = dict(
        data=data,
        schema_version=S.SCHEMA_VERSION,
        made_by=made_by,
        updated_at=now,
        tokens_in=getattr(usage, "input", None),
        tokens_out=getattr(usage, "output", None),
        **cols,
    )
    if row is None:
        c.execute(
            a.insert().values(
                id=db.new_id("ana"), reel_id=reel_id, user_id=user_id, level=level, created_at=now, **vals
            )
        )
    else:
        c.execute(a.update().where(a.c.id == row.id).values(**vals))


def _cut_pace(edit: dict | None) -> str | None:
    return (edit or {}).get("cut_pace")


def _length_bucket(reel, sig) -> str | None:
    lb = ((sig.edit or {}) if sig is not None else {}).get("length_bucket")
    if lb:
        return lb
    from .. import insights

    return insights.length_bucket(reel.duration)


def _recipe_key(vf: str, recipe: dict) -> str:
    from .. import insights

    return insights.recipe_key(vf, recipe)


def _local_agrees(sig, vf: str) -> bool:
    vis = (sig.visual or {}) if sig is not None else {}
    return any(g.get("id") == vf and float(g.get("p") or 0) >= 0.8 for g in vis.get("local_vf") or [])


def save_quick_tags(user_id: str, job_row, batch: S.QuickTagBatch, made_by: dict, usage=None, *, vision: bool) -> dict:
    ids = list((job_row.payload or {}).get("reel_ids") or [])
    got = [t.reel_id for t in batch.tags]
    if sorted(got) != sorted(ids) or len(set(got)) != len(got):
        missing = [i for i in ids if i not in got]
        extra = [g for g in got if g not in ids]
        raise Invalid(
            "Return exactly one tag per reel id in the batch, each id once. "
            + (f"Missing: {', '.join(missing)}. " if missing else "")
            + (f"Not in this batch: {', '.join(extra)}." if extra else "")
        )
    from . import pipeline

    r = db.reels
    saved = 0
    with db.tx() as c:
        active = {t.slug for t in active_topics(c, user_id)}
        reels = _reels_by_id(c, user_id, ids)
        sigs = _signals(c, user_id, ids)
        for t in batch.tags:
            reel = reels.get(t.reel_id)
            if reel is None:
                continue
            sig = sigs.get(t.reel_id)
            saw = vision and _has_frames(user_id, t.reel_id)
            quality = "vision" if saw else "measured"
            topics = _norm_topics(t.topics, active)
            vf2 = [v for v in dict.fromkeys(t.visual_format_2) if v != t.visual_format][:2]
            data = t.model_dump()
            data.update(
                {
                    "topics": topics,
                    "visual_format_2": vf2,
                    "tag_quality": quality,
                    "local_agrees": _local_agrees(sig, t.visual_format),
                }
            )
            hb = _hook_bank(reel, sig, t)
            if hb:
                data["hook_bank"] = hb
            rec = t.recipe.model_dump()
            cols = dict(
                visual_format=t.visual_format,
                visual_format_2=vf2,
                sound_primary=t.sound_primary,
                sound_secondary=list(dict.fromkeys(s for s in t.sound_secondary if s != t.sound_primary)),
                topics=topics,
                hook_type=t.hook_type,
                cta_type=t.cta_type,
                caption_style=rec["caption_style"],
                has_text_hook=t.has_text_hook,
                cut_pace=_cut_pace(sig.edit if sig is not None else None),
                length_bucket=_length_bucket(reel, sig),
                headline_box=rec["headline_box"],
                face_bubble=rec["face_bubble"],
                broll_share=rec["broll_share"],
                setting=rec["setting"],
                framing=rec["framing"],
                recipe_key=_recipe_key(t.visual_format, rec),
                tag_quality=quality,
            )
            _upsert_analysis(c, user_id, t.reel_id, "quick", data, cols, made_by, usage)
            c.execute(
                r.update()
                .where(r.c.id == t.reel_id)
                .values(
                    tag_state="done",
                    tag_batch=None,
                    tag_quality="member" if reel.tag_quality == "member" else quality,
                    needs_topic_retag=not active,
                )
            )
            saved += 1
        release_tag_batch(c, job_row)  # anything left in the batch goes back to the queue
        release_dedupe(job_row.id, conn=c)
        ensure_tag_slot(user_id, conn=c)
        remaining = _queued_tags(c, user_id)
    for rid in ids:
        pipeline.advance(user_id, rid)
    if saved:
        _queue_insights(user_id)
    _maybe_topics_from_reels(user_id)
    return {"tagged": saved, "remaining": remaining}


def _queue_insights(user_id: str) -> None:
    """New tags change what works: WP4's debounced refresh (60 s, one job per member). Integration finding: without
    it, insights and named formats only refreshed when the member opened Home, never while tagging ran."""
    try:
        from . import jobs_score

        jobs_score.queue_insights(user_id)
    except Exception:  # noqa: BLE001 - a missed refresh is caught by jobs_score.maybe_refresh on the next visit
        log.exception("queue_insights failed for %s", user_id)


def _maybe_topics_from_reels(user_id: str) -> None:
    """6.5: suggest_topics(from=reels) when the member first has 30 tagged reels, then after every 30 more."""
    r = db.reels
    with db.tx() as c:
        n = int(
            c.execute(
                select(func.count()).select_from(r).where(r.c.user_id == user_id, r.c.tag_state == "done")
            ).scalar()
            or 0
        )
        u = user_row(c, user_id)
        if u is None:
            return
        st = dict(u.settings or {})
        last = st.get("topics_from_reels_at")
        if n < REELS_FOR_TOPICS or (last is not None and n - int(last) < REELS_FOR_TOPICS):
            return
        st["topics_from_reels_at"] = n
        c.execute(db.users.update().where(db.users.c.id == user_id).values(settings=st))
    queue_topics(user_id, "reels")


def save_deep(user_id: str, job_row, bd: S.ReelBreakdown, made_by: dict, usage=None) -> dict:
    rid = (job_row.payload or {}).get("reel_id")
    if bd.reel_id != rid:
        raise Invalid(f"reel_id must be {rid}.")
    from . import pipeline

    r = db.reels
    with db.tx() as c:
        reel = _get(c, db.reels, user_id, rid)
        if reel is None:
            raise Invalid("This reel is gone.")
        sig = _signals(c, user_id, [rid]).get(rid)
        active = {t.slug for t in active_topics(c, user_id)}
        data = bd.model_dump()
        data["topics"] = _norm_topics(bd.topics, active)
        times = cue_times(sig.sound if sig is not None else None)
        if times:  # never keep invented cues (6.8): each cue time must sit near a detected event
            kept = [cu for cu in data["sound"]["cues"] if any(abs(cu["t"] - x) <= 0.6 for x in times)]
            dropped = len(data["sound"]["cues"]) - len(kept)
            data["sound"]["cues"] = kept
            if dropped:
                data["dropped_cues"] = dropped
        hook_type = bd.skeleton.hook.hook_type if bd.skeleton.hook.hook_type in taxonomy.HOOK_TYPES else "other"
        cta_type = bd.skeleton.cta.cta_type if bd.skeleton.cta.cta_type in taxonomy.CTA_TYPES else "none"
        e = bd.edit
        setting = S.slugify(e.setting).replace("-", "_") if e.setting else "other"
        rec = {
            "headline_box": e.headline_box,
            "caption_style": e.caption_style,
            "face_bubble": e.face_bubble,
            "broll_share": e.broll_share,
            "setting": setting,
            "framing": e.framing,
        }
        data["hook_bank"] = {
            "spoken": bd.skeleton.hook.spoken,
            "on_screen_text": bd.skeleton.hook.on_screen_text,
            "visual": bd.skeleton.hook.visual,
            "hook_type": hook_type,
            "formula": bd.hook_formula,
            "why": bd.skeleton.hook.why_it_works,
            "level": "deep",
        }
        quality = "vision" if (_has_frames(user_id, rid) and made_by.get("vision", True)) else "measured"
        cols = dict(
            visual_format=bd.format.primary,
            visual_format_2=[v for v in bd.format.secondary if v != bd.format.primary][:2],
            sound_primary=bd.sound.primary,
            sound_secondary=list(bd.sound.secondary),
            topics=data["topics"],
            hook_type=hook_type,
            cta_type=cta_type,
            caption_style=e.caption_style,
            has_text_hook=bool(bd.skeleton.hook.on_screen_text),
            cut_pace=_cut_pace(sig.edit if sig else None),
            length_bucket=_length_bucket(reel, sig),
            headline_box=e.headline_box,
            face_bubble=e.face_bubble,
            broll_share=e.broll_share,
            setting=setting,
            framing=e.framing,
            recipe_key=_recipe_key(bd.format.primary, rec),
            tag_quality=quality,
        )
        _upsert_analysis(c, user_id, rid, "deep", data, cols, _made_by(made_by), usage)
        c.execute(r.update().where(r.c.id == rid).values(deep_state="done"))
    pipeline.advance(user_id, rid)
    unblock_outputs(user_id, rid)
    with db.read() as c:
        left = c.execute(
            select(func.count())
            .select_from(db.jobs)
            .where(
                db.jobs.c.user_id == user_id,
                db.jobs.c.kind == "analyze_reel",
                db.jobs.c.state.in_(("queued", "waiting")),
            )
        ).scalar()
    return {"ok": True, "saved_reel_id": rid, "remaining": int(left or 0)}


def save_profile_draft(user_id: str, draft: S.ProfileDraft, made_by: dict) -> dict:
    """9.3: each answer saved unconfirmed unless the member already confirmed that question. Under 0.5 confidence
    the answer is kept as a suggestion only (value stays empty)."""
    p = db.profiles
    with db.tx() as c:
        src_ids = {
            row[0] for row in c.execute(select(db.profile_sources.c.id).where(db.profile_sources.c.user_id == user_id))
        }
        prof = profile_row(c, user_id)
        answers = dict(prof.answers or {}) if prof is not None else {}
        changed = 0
        for qid, _ in QUESTIONS:
            da: S.DraftAnswer = getattr(draft.answers, qid)
            old = answers.get(qid) or {}
            if old.get("confirmed") or old.get("source") == "member":
                continue
            if qid in LIST_QUESTIONS:
                value: Any = [x for x in da.items if x] or (
                    [v.strip() for v in re.split(r"\n|;", da.value) if v.strip()] if da.value else []
                )
                if qid == "q14":
                    value = [re.sub(r"^@", "", v).strip().lower() for v in value][:5]
            else:
                value = da.value or "\n".join(da.items)
            if not value:
                continue
            srcs = [s for s in da.source_ids if s in src_ids]
            ans = {
                "value": value,
                "source": srcs[0] if srcs else "ai",
                "confidence": round(float(da.confidence), 2),
                "confirmed": False,
                "note": da.note,
            }
            if da.confidence < 0.5:
                ans = {
                    "value": "",
                    "suggestion": value,
                    "source": ans["source"],
                    "confidence": ans["confidence"],
                    "confirmed": False,
                    "note": da.note,
                }
            answers[qid] = ans
            changed += 1
        answers["_draft"] = {
            "niche_label": draft.niche_label,
            "search_terms": list(draft.search_terms),
            "competitor_handles": [h.lstrip("@").lower() for h in draft.competitor_handles],
            "made_by": _made_by(made_by),
            "at": db.iso(db.utcnow()),
        }
        now = db.utcnow()
        if prof is None:
            c.execute(
                p.insert().values(user_id=user_id, brand_id="me", status="draft", answers=answers, updated_at=now)
            )
        else:
            status = prof.status
            if status in ("empty", "drafting") or (status == "approved" and changed):
                status = "draft"
            c.execute(
                p.update()
                .where(p.c.user_id == user_id, p.c.brand_id == "me")
                .values(answers=answers, status=status, updated_at=now)
            )
    return {"ok": True, "filled": changed}


def save_topic_list(user_id: str, tl: S.TopicList, source: str) -> dict:
    t = db.topics
    cap = 5 if source == "reels" else 10
    n = 0
    with db.tx() as c:
        have = {row.slug for row in c.execute(select(t.c.slug).where(t.c.user_id == user_id, t.c.brand_id == "me"))}
        pos = int(c.execute(select(func.coalesce(func.max(t.c.position), 0)).where(t.c.user_id == user_id)).scalar())
        for item in tl.topics:
            if n >= cap or item.slug in have or not item.label:
                continue
            pos += 1
            c.execute(
                t.insert().values(
                    id=db.new_id("top"),
                    user_id=user_id,
                    brand_id="me",
                    slug=item.slug,
                    label=item.label,
                    description=item.description,
                    examples=list(item.examples)[:5],
                    source="ai_from_reels" if source == "reels" else "ai",
                    status="suggested",
                    position=pos,
                    created_at=db.utcnow(),
                )
            )
            have.add(item.slug)
            n += 1
    return {"count": n}


def save_retag(user_id: str, job_row, batch: S.TopicRetagBatch, made_by: dict) -> dict:
    ids = list((job_row.payload or {}).get("reel_ids") or [])
    got = [t.reel_id for t in batch.tags]
    if sorted(got) != sorted(ids) or len(set(got)) != len(got):
        raise Invalid("Return exactly one entry per reel id in the task, each id once.")
    a, r = db.analyses, db.reels
    with db.tx() as c:
        active = {t.slug for t in active_topics(c, user_id)}
        for t in batch.tags:
            topics = _norm_topics(t.topics, active)
            row = c.execute(
                select(a).where(a.c.reel_id == t.reel_id, a.c.user_id == user_id, a.c.level == "quick")
            ).first()
            if row is not None:
                data = dict(row.data or {})
                data["topics"] = topics
                if t.new_topic:
                    data["new_topic"] = t.new_topic
                c.execute(a.update().where(a.c.id == row.id).values(topics=topics, data=data, updated_at=db.utcnow()))
            c.execute(r.update().where(r.c.id == t.reel_id, r.c.user_id == user_id).values(needs_topic_retag=False))
        release_dedupe(job_row.id, conn=c)
        left = c.execute(
            select(func.count())
            .select_from(r)
            .where(r.c.user_id == user_id, r.c.needs_topic_retag.is_(True), r.c.tag_state == "done")
        ).scalar()
        if left:
            enqueue_ai(user_id, "retag_topics", {}, priority=50, dedupe_key=f"retag_topics:{user_id}", conn=c)
    _queue_insights(user_id)   # topic lift changed
    return {"retagged": len(got), "remaining": int(left or 0)}


def save_format_names(user_id: str, job_row, batch: S.FormatGuideBatch, made_by: dict) -> dict:
    ids = list((job_row.payload or {}).get("format_ids") or [])
    got = [g.format_id for g in batch.guides]
    if sorted(got) != sorted(ids) or len(set(got)) != len(got):
        raise Invalid("Return exactly one FormatGuide per format id in the task, each id once.")
    f = db.formats
    with db.tx() as c:
        for g in batch.guides:
            row = _get(c, f, user_id, g.format_id)
            if row is None:
                continue
            vals = dict(name=g.name, guide=g.model_dump(), named_at=db.utcnow())
            if not row.needs:
                vals["needs"] = list(g.needs)
            c.execute(f.update().where(f.c.id == row.id).values(**vals))
    return {"named": len(got)}


def save_result(
    user_id: str,
    job_row,
    output,
    made_by: dict | None = None,
    *,
    usage=None,
    finish: bool = True,
    vision: bool | None = None,
) -> dict:
    """Validate one AI answer for a job with the job's pydantic model, cross-check it, write it, and (finish=True,
    the MCP and desktop paths) complete the job. Raises Invalid(message) on structural errors or failed checks."""
    from . import outputs

    kind = job_row.kind
    mb = _made_by(made_by)
    if job_row.user_id != user_id:
        raise Invalid("This task is not yours.")
    if kind in ("tag_reels", "retag_topics") and job_row.state != "running":
        raise Invalid("This batch expired or was already saved. Ask for a new batch.")
    if kind == "tag_reels":
        batch = _validate(S.QuickTagBatch, output)
        vis = (job_row.executor in ("mcp", "desktop")) if vision is None else vision
        res = save_quick_tags(user_id, job_row, batch, mb, usage, vision=vis)
    elif kind == "analyze_reel":
        res = save_deep(
            user_id, job_row, _validate(S.ReelBreakdown, output), dict(mb, vision=vision is not False), usage
        )
    elif kind == "draft_profile":
        res = save_profile_draft(user_id, _validate(S.ProfileDraft, output), mb)
    elif kind == "suggest_topics":
        res = save_topic_list(user_id, _validate(S.TopicList, output), (job_row.payload or {}).get("from") or "profile")
    elif kind == "retag_topics":
        res = save_retag(user_id, job_row, _validate(S.TopicRetagBatch, output), mb)
    elif kind == "name_formats":
        res = save_format_names(user_id, job_row, _validate(S.FormatGuideBatch, output), mb)
    elif kind == "make_output":
        res = outputs.save_chunk(user_id, job_row, output, mb, usage=usage)
        if res.get("next_chunk") is not None and finish:
            with db.tx() as c:
                back_to_waiting(job_row.id, conn=c)
            _bump_usage(user_id, job_row, usage)
            return res
    else:
        raise Invalid(f"Unknown AI task {kind}.")
    _bump_usage(user_id, job_row, usage)
    if finish:
        jobs.complete(job_row.id, {k: v for k, v in res.items() if k != "data"})
    return res


def _bump_usage(user_id: str, job_row, usage) -> None:
    conn_id = (job_row.payload or {}).get("conn_id") or getattr(usage, "conn_id", None)
    if not conn_id or usage is None:
        return
    month = db.utcnow().strftime("%Y-%m")
    db.bump(
        db.ai_usage,
        {"conn_id": conn_id, "month": month},
        _set={"user_id": user_id},
        calls=1,
        tokens_in=int(getattr(usage, "input", 0) or 0),
        tokens_out=int(getattr(usage, "output", 0) or 0),
    )


# ============================================================================================== pending / MCP


def next_pending(user_id: str, kinds: list[str]):
    j = db.jobs
    with db.read() as c:
        return c.execute(
            select(j)
            .where(
                j.c.user_id == user_id,
                j.c.executor.in_(("mcp", "desktop")),
                j.c.state == "waiting",
                j.c.kind.in_(list(kinds)),
            )
            .order_by(j.c.priority, j.c.created_at)
            .limit(1)
        ).first()


def pending_counts(user_id: str, executor: str | None = None) -> dict:
    j, r = db.jobs, db.reels
    with db.read() as c:
        q = select(j.c.kind, func.count()).where(
            j.c.user_id == user_id, j.c.kind.in_(AI_KINDS), j.c.state.in_(("waiting", "queued", "paused"))
        )
        if executor:
            q = q.where(j.c.executor == executor)
        by = dict(c.execute(q.group_by(j.c.kind)).all())
        tags = c.execute(
            select(func.count()).select_from(r).where(r.c.user_id == user_id, r.c.tag_state.in_(("queued", "leased")))
        ).scalar()
    out = {k: int(by.get(k, 0)) for k in AI_KINDS}
    out["tag_reels"] = int(tags or 0)
    return out


def _b64_len(n: int) -> int:
    return 4 * ((n + 2) // 3)


def _held_mcp(user_id: str, kind: str):
    """A job of `kind` this member's Claude already holds (leased, not saved): a chat that got cut off or hit
    its message limit. It is handed out again first (same batch id), so the next chat finishes it instead of
    hearing "Nothing to tag" for 30 minutes (fix round 1)."""
    from datetime import timedelta

    j = db.jobs
    with db.tx() as c:
        row = c.execute(db.scoped(c, j, user_id).where(j.c.kind == kind, j.c.state == "running",
                                                       j.c.lease_owner == "mcp")
                        .order_by(j.c.priority, j.c.created_at).limit(1)).first()
        if row is None:
            return None
        c.execute(j.update().where(j.c.id == row.id, j.c.state == "running")
                  .values(lease_until=db.utcnow() + timedelta(seconds=LEASE_MCP_S)))
        return c.execute(select(j).where(j.c.id == row.id)).first()


def mcp_tag_batch(user_id: str):
    """Lease the next tag batch for the member's Claude, sized by bytes: reels are added one at a time while
    3,000 + sum(b64(sheet) + text) < 90,000; max 3, at least 1 (6.1.1). Returns (batch_id, text, [Image]) or None.
    A batch an earlier chat leased but never saved comes back first."""
    from ..signals import frames

    held = _held_mcp(user_id, "tag_reels")
    if held is not None:
        return _tag_batch_text(user_id, held)
    r = db.reels
    with db.read() as c:
        cands = [
            x[0]
            for x in c.execute(
                select(r.c.id)
                .where(r.c.user_id == user_id, r.c.tag_state == "queued")
                .order_by(*_priority_order(r))
                .limit(MCP_MAX_REELS)
            )
        ]
    if not cands:
        return None
    sizes: dict[str, int] = {}
    for rid in cands:
        sz = 0
        if _has_frames(user_id, rid):
            try:
                sz = _b64_len(len(frames.sheet_for(_reel_dir(user_id, rid), "mcp")))
            except (OSError, FileNotFoundError):
                sz = 0
        sizes[rid] = sz + 3500
    chosen, total = [], MCP_BASE
    for rid in cands:
        if chosen and total + sizes[rid] >= MCP_BUDGET:
            break
        chosen.append(rid)
        total += sizes[rid]

    def prep(conn, job):
        return bool(lease_tag_batch(conn, user_id, job, "mcp", n=len(chosen), only=chosen))

    ensure_tag_slot(user_id)
    job = jobs.lease_external(user_id, "mcp", ["tag_reels"], "mcp", LEASE_MCP_S, prepare=prep)
    if job is None:
        return None
    return _tag_batch_text(user_id, job)


def _tag_batch_text(user_id: str, job):
    task = build_task(user_id, job, dest="mcp")
    images = task_images(user_id, job, "mcp")
    text = (
        f"batch_id: {job.id}\nLook at each contact sheet, then call save_tags with this batch_id and one tag per "
        f"reel.\n\n" + task["prompt"]
    )
    while len(text) + sum(_b64_len(len(im.data)) for im in images) >= MCP_BUDGET and len(text) > 4000:
        text = text[: int(len(text) * 0.9)] + "\n[cut to fit]"
    return job.id, text, images


def _lease_job(job_id: str, user_id: str, owner: str, lease_s: int):
    from datetime import timedelta

    j = db.jobs
    now = db.utcnow()
    with db.tx() as c:
        n = c.execute(
            j.update()
            .where(j.c.id == job_id, j.c.user_id == user_id, j.c.state.in_(("waiting", "queued")))
            .values(
                state="running",
                lease_owner=owner,
                lease_until=now + timedelta(seconds=lease_s),
                started_at=func.coalesce(j.c.started_at, now),
                attempts=j.c.attempts + 1,
            )
        ).rowcount
        return c.execute(select(j).where(j.c.id == job_id)).first() if n else None


def mcp_reel_task(user_id: str, reel_id: str | None = None):
    """The next waiting deep read for the member's Claude (or one reel): (job_id, text, [mcp sheet, 2 frames]),
    < 90,000 characters. Returns None when nothing waits; raises NotReady when the reel's media isn't ready."""
    j = db.jobs
    job = None
    if reel_id:
        with db.read() as c:
            reel = _get(c, db.reels, user_id, reel_id)
            if reel is None:
                raise NotReady("That reel is not in your ReelVault.", "not_found", 404)
            row = c.execute(
                select(j).where(
                    j.c.user_id == user_id, j.c.kind == "analyze_reel", j.c.dedupe_key == f"analyze_reel:{reel_id}"
                )
            ).first()
        if row is None:
            queue_deep(user_id, [reel_id])
            with db.read() as c:
                row = c.execute(
                    select(j).where(
                        j.c.user_id == user_id, j.c.kind == "analyze_reel", j.c.dedupe_key == f"analyze_reel:{reel_id}"
                    )
                ).first()
        if row is None:
            raise NotReady("This reel's video is still being read. Try again in a few minutes.", "busy", 409)
        if row.executor != "mcp":
            with db.tx() as c:
                c.execute(
                    j.update()
                    .where(j.c.id == row.id, j.c.state.in_(("waiting", "queued", "paused")))
                    .values(executor="mcp", job_class="external", state="waiting")
                )
        job = _lease_job(row.id, user_id, "mcp", LEASE_MCP_S) if row.state != "running" else row
        if job is None:
            job = row
    else:
        job = _held_mcp(user_id, "analyze_reel") or jobs.lease_external(
            user_id, "mcp", ["analyze_reel"], "mcp", LEASE_MCP_S, prepare=prepare_external(user_id, "mcp")
        )
        if job is None:
            return None
    task = build_task(user_id, job, dest="mcp")
    images = task_images(user_id, job, "mcp")
    text = f"reel_id: {(job.payload or {}).get('reel_id')}\n\n" + task["prompt"]
    budget = MCP_BUDGET - sum(_b64_len(len(im.data)) for im in images) - 500
    if len(text) > min(budget, MCP_DEEP_TEXT + 2000):
        text = text[: max(2000, min(budget, MCP_DEEP_TEXT + 2000) - 50)] + "\n[cut to fit]"
    return job.id, text, images


def _job_of_kind(user_id: str, kind: str, job_id: str | None = None):
    j = db.jobs
    with db.read() as c:
        q = select(j).where(j.c.user_id == user_id, j.c.kind == kind)
        if job_id:
            q = q.where(j.c.id == job_id)
        else:
            q = q.where(j.c.state.in_(("running", "waiting", "queued", "paused"))).order_by(j.c.created_at.desc())
        return c.execute(q.limit(1)).first()


def mcp_save(user_id: str, kind: str, output, *, job_id: str | None = None, made_by: dict | None = None) -> dict:
    """MCP save_profile_draft / save_topics / save_format_names: save into the member's job of that kind (the
    given batch id, else the newest active one; a new done job when Claude drafts without one)."""
    job = _job_of_kind(user_id, kind, job_id)
    if job is None and job_id:
        raise Invalid("That batch id is not yours or it is gone.")
    if job is None:
        if kind not in ("draft_profile", "suggest_topics"):
            raise Invalid("Nothing is waiting for this. Call list_pending_work.")
        jid = jobs.enqueue(
            user_id, kind, {"from": "profile"} if kind == "suggest_topics" else {}, executor="mcp", state="running"
        )
        job = jobs.get(jid)
    res = save_result(user_id, job, output, made_by or {"via": "mcp"})
    return {"ok": True, **{k: v for k, v in res.items() if k in ("filled", "count", "named", "remaining")}}


def output_brief(
    user_id: str, output_id: str | None = None, *, kind: str | None = None, options: dict | None = None
) -> dict:
    """MCP get_output_brief: the next chunk's brief of an output (creating the output when kind/options are
    given). Leases the make_output job to Claude until save_output (or the lease ends)."""
    from . import outputs as out_mod

    if output_id is None:
        if not kind:
            row = next_pending(user_id, ["make_output"])
            if row is None:
                raise NotReady("No output is waiting. Ask for one with a kind and options.")
            output_id = (row.payload or {}).get("output_id")
        else:
            output_id, _ = create_output(user_id, kind, options or {})
    with db.read() as c:
        out = _get(c, db.outputs, user_id, output_id or "")
        if out is None:
            raise NotReady("That output is not in your ReelVault.", "not_found", 404)
        job = c.execute(select(db.jobs).where(db.jobs.c.id == out.job_id)).first() if out.job_id else None
    if out.status == "done":
        raise NotReady("This output is already finished.", "busy", 409)
    if job is None or job.state not in ("running", "waiting", "queued"):
        raise NotReady(
            "This output is waiting for a source reel's breakdown, or it stopped. Check it in ReelVault.", "busy", 409
        )
    if job.state != "running" or job.lease_owner != "mcp":
        with db.tx() as c:
            c.execute(
                db.jobs.update()
                .where(db.jobs.c.id == job.id, db.jobs.c.state.in_(("waiting", "queued")))
                .values(executor="mcp", job_class="external", state="waiting")
            )
        job = _lease_job(job.id, user_id, "mcp", LEASE_MCP_S) or jobs.get(job.id)
    task = out_mod.build_brief(user_id, job, provider_name="anthropic")
    with db.tx() as c:
        c.execute(db.outputs.update().where(db.outputs.c.id == output_id).values(status="working"))
    return {
        "output_id": output_id,
        "kind": out.kind,
        "brief": task["system"] + "\n\n" + task["prompt"],
        "schema_name": task["schema_name"],
        "chunk": task["chunk"],
    }


def mcp_save_output(
    user_id: str, output_id: str, data, *, chunk_index: int | None = None, made_by: dict | None = None
) -> dict:
    """MCP save_output: one chunk of an output. Returns {"ok", "url", "next_chunk"?}."""
    with db.read() as c:
        out = _get(c, db.outputs, user_id, output_id)
        job = c.execute(select(db.jobs).where(db.jobs.c.id == out.job_id)).first() if out and out.job_id else None
    if out is None or job is None:
        raise Invalid("That output is not in your ReelVault.")
    want = int((out.inputs or {}).get("chunks_done") or 0)
    if chunk_index is not None and int(chunk_index) != want:
        raise Invalid(f"Save chunk {want} next (call get_output_brief for it).")
    res = save_result(user_id, job, data, made_by or {"via": "mcp"})
    ret = {"ok": True, "url": res.get("url")}
    if res.get("next_chunk") is not None:
        ret["next_chunk"] = res["next_chunk"]
    return ret


def taxonomy_text(part: str | None = "all") -> str:
    """MCP get_taxonomy: the VF/SD/EP lists with cues and precedence, and the field guides (≤ 30,000 chars)."""
    part = part or "all"
    blocks = []
    if part in ("visual", "all"):
        blocks.append(taxonomy.prompt_block("visual"))
    if part in ("sound", "all"):
        blocks.append(taxonomy.prompt_block("sound"))
    if part in ("fields", "all"):
        blocks += [
            taxonomy.prompt_block("edit"),
            taxonomy.prompt_block("lists"),
            "QuickTagBatch fields (save_tags):\n" + S.field_guide(S.QuickTagBatch),
            "ReelBreakdown fields (save_analysis):\n" + S.field_guide(S.ReelBreakdown),
        ]
    if not blocks:
        raise ValueError("part must be visual, sound, fields or all")
    return "\n\n".join(blocks)[:30_000]


def image_b64(im) -> str:
    return base64.standard_b64encode(im.data).decode()
