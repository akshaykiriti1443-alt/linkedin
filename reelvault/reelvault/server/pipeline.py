"""advance(user_id, reel_id): the ONLY place that chains a reel's steps (DESIGN-SAAS.md 6.0). WP5.

Called at the end of pick_sample (WP4), fetch_video and process_reel (WP3), save_quick_tags / save_deep (work.py),
queue_deep and when a member stars a reel. It reads the reel's states and enqueues the next step; dedupe keys make
repeated calls harmless. Jobs never carry dependencies: this table is the whole chain.

| reel state                                                              | next step                                  |
|-------------------------------------------------------------------------|--------------------------------------------|
| wanted (tier priority/sample, starred or deep queued), media none/queued | fetch_video (media pool)                   |
| media have, signals none/queued                                          | process_reel, words = priority or deep     |
| signals done, tag_state none                                             | tag_state = queued (+ the member's slot)   |
| signals done, deep queued (word times present when speech exists)        | analyze_reel                               |
| deep queued, signals done but no word times                              | process_reel again with words = True       |
| media need_extension                                                     | nothing (WP2 batches ig_videos)            |
| signals failed/skipped, or media failed                                  | tag from caption + numbers (measured);     |
|                                                                          | a requested deep read runs text-only       |
Returns the id of the job it queued (or found active), else None.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from . import db

log = logging.getLogger("reelvault.pipeline")


def _wanted(reel) -> bool:
    return reel.tier in ("priority", "sample") or bool(reel.starred) or reel.deep_state in ("queued", "waiting")


# A downloaded reel is processed before the next download of the same tier (both share the media pool, and a
# download holds its thread through the 2-5 s CDN gap), so results arrive steadily instead of after every download.
PROCESS_FIRST = 5


def _job_priority(reel) -> int:
    from .work import PRIORITY

    if reel.deep_state in ("queued", "waiting") or reel.starred:
        return PRIORITY["deep"]
    return PRIORITY.get(reel.tier or "none", 70)


def _needs_words(conn, reel) -> bool:
    """A deep read wants word times; a sample reel was transcribed without them (WP3 'words later')."""
    from .settings import get_settings

    sig = conn.execute(select(db.reel_signals.c.transcript).where(db.reel_signals.c.reel_id == reel.id)).first()
    tr = (sig.transcript or {}) if sig is not None else {}
    if not tr.get("segments") or tr.get("words"):
        return False
    return tr.get("model") not in (None, get_settings().whisper_model)


def advance(user_id: str, reel_id: str) -> str | None:
    from . import jobs_media, work

    r = db.reels
    with db.tx() as c:
        reel = db.get_owned(c, r, user_id, reel_id)
        if reel is None:
            return None
        deep = reel.deep_state in ("queued", "waiting")
        media, sig = reel.media_state, reel.signals_state
        prio = _job_priority(reel)

        if sig in ("failed", "skipped") or (media == "failed" and sig == "none"):
            jid = None
            if reel.tag_state == "none":
                c.execute(r.update().where(r.c.id == reel_id, r.c.tag_state == "none").values(tag_state="queued"))
                jid = work.ensure_tag_slot(user_id, conn=c)
            if reel.deep_state == "queued":
                jid = _queue_analyze(c, user_id, reel, prio) or jid
            return jid

        if media in ("none", "queued") and sig in ("none", "queued") and _wanted(reel):
            if media == "none":
                c.execute(r.update().where(r.c.id == reel_id, r.c.media_state == "none").values(media_state="queued"))
            return jobs_media.queue_fetch(user_id, reel_id, priority=prio, conn=c)

        if media == "have" and sig in ("none", "queued"):
            if sig == "none":
                c.execute(
                    r.update().where(r.c.id == reel_id, r.c.signals_state == "none").values(signals_state="queued")
                )
            return jobs_media.queue_process(
                user_id, reel_id, words=reel.tier == "priority" or deep, priority=prio - PROCESS_FIRST, conn=c
            )

        if sig == "done":
            jid = None
            if reel.tag_state == "none":
                c.execute(r.update().where(r.c.id == reel_id, r.c.tag_state == "none").values(tag_state="queued"))
                jid = work.ensure_tag_slot(user_id, conn=c)
            if reel.deep_state == "queued":
                if media == "have" and _needs_words(c, reel):
                    c.execute(r.update().where(r.c.id == reel_id).values(signals_state="queued"))
                    return jobs_media.queue_process(user_id, reel_id, words=True, priority=prio - PROCESS_FIRST,
                                                    conn=c)
                return _queue_analyze(c, user_id, reel, prio) or jid
            return jid
        return None


def _queue_analyze(c, user_id: str, reel, prio: int) -> str:
    from . import work

    jid = work.enqueue_ai(
        user_id, "analyze_reel", {"reel_id": reel.id}, priority=prio, dedupe_key=f"analyze_reel:{reel.id}", conn=c
    )
    row = c.execute(select(db.jobs.c.executor, db.jobs.c.state).where(db.jobs.c.id == jid)).first()
    if row is not None and row.executor != "server":
        c.execute(
            db.reels.update()
            .where(db.reels.c.id == reel.id, db.reels.c.deep_state == "queued")
            .values(deep_state="waiting")
        )
    return jid


def advance_many(user_id: str, reel_ids: list[str]) -> int:
    n = 0
    for rid in reel_ids:
        try:
            if advance(user_id, rid):
                n += 1
        except Exception:  # noqa: BLE001 - one bad reel never stops the rest
            log.exception("advance failed for %s", rid)
    return n
