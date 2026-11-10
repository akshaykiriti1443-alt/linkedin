"""Job handlers score_account, pick_sample, refresh_insights + the DB side of scoring, insights and discovery.
WP4 owns this file (DESIGN-SAAS.md 6.0, 6.6, 6.6.1, 6.7, 6.9).

Chain: WP2 ingest -> score_account (metrics only; first look in minutes) -> pick_sample (tiers, then
pipeline.advance for every priority/sample reel, work.queue_deep for the top 5) -> refresh_insights (debounced
60 s; weighted lift, sub-formats -> formats rows -> work.queue_format_names for new or grown groups).

Other packages call:
    queue_insights(user_id)               # after tags, overrides or deletes change what insights see (debounced)
    best_for_deep(user_id, account_id, n=5) -> [reel_id]
    report_numbers(user_id, account_ids) -> Insights JSON (6.7.5) on a subset of accounts (competitor_report)
    effective_tags(conn, user_id, reel_ids) -> {reel_id: {...}}   # member override ?? deep ?? quick
    start_discover / ingest_candidate / pick_check_candidates / after_check   (WP2 hooks, 6.6.1)
"""
from __future__ import annotations

import logging
import re
import statistics as st
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, select

from .. import insights as ins
from .. import outliers as ol
from . import db, jobs, storage
from .errors import bad_input, not_found
from .jobs import handler

log = logging.getLogger("reelvault.score")

INSIGHTS_DEBOUNCE_S = 60
RESCORE_AFTER_H = 20          # "nightly": re-score accounts whose scores are older than this when the member looks
SNAPSHOTS_KEPT = 10
CHECK_MAX = 15

VISUAL_KEYS = ("visual_format", "headline_box", "caption_style", "face_bubble", "broll_share", "setting", "framing")
OVERRIDE_KEYS = ("visual_format", "topics", "sound_primary", "hook_type", "cta_type", "headline_box", "caption_style",
                 "face_bubble", "broll_share", "setting", "framing")
TAG_COLUMNS = ("visual_format", "visual_format_2", "sound_primary", "sound_secondary", "topics", "hook_type",
               "cta_type", "caption_style", "has_text_hook", "cut_pace", "length_bucket", "headline_box",
               "face_bubble", "broll_share", "setting", "framing", "recipe_key", "format_id", "tag_quality")


def _now() -> datetime:
    return db.utcnow()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ============================================================================================== effective tags

def effective_tags(conn, user_id: str, reel_ids: list[str] | None = None, *, data: bool = True,
                   quick_only: bool = False) -> dict[str, dict]:
    """{reel_id: tags} for reels with a quick or deep analysis (or member overrides).

    Effective value = member override (stored on the quick row's `overrides`) ?? deep column ?? quick column.
    Extra keys: quality (vision|measured|member), tagged, has_deep, sub_format (analyses.format_id), quick (the
    QuickTags data), deep_data (ReelBreakdown data).
    quick_only=True (insights, fix round 1): member override ?? quick column, never the deep column. Deep reads run
    almost only on outliers, so mixing their answers in made the deep tagger's habits look like "lift"; every
    insight unit is tagged by the same quick tagger. `tagged` then means "has quick tags"."""
    a = db.analyses
    if data:
        q = db.scoped(conn, a, user_id)
    else:  # lists: skip the big JSON documents
        cols = [a.c.reel_id, a.c.level, a.c.overrides] + [a.c[c] for c in TAG_COLUMNS]
        q = select(*cols).where(a.c.user_id == user_id)
    if reel_ids is not None:
        if not reel_ids:
            return {}
        q = q.where(a.c.reel_id.in_(list(reel_ids)))
    rows = conn.execute(q).all()
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        by.setdefault(r.reel_id, {})[r.level] = r
    out = {}
    for rid, lv in by.items():
        quick, deep = lv.get("quick"), lv.get("deep")
        ov = dict((quick.overrides if quick is not None else None) or {})
        if deep is not None and deep.overrides:
            for k, v in deep.overrides.items():
                ov.setdefault(k, v)
        t: dict[str, Any] = {}
        for col in TAG_COLUMNS:
            dv = getattr(deep, col) if deep is not None and not quick_only else None
            qv = getattr(quick, col) if quick is not None else None
            t[col] = dv if dv is not None else qv
            if quick_only and col == "format_id" and t[col] is None and deep is not None:
                t[col] = deep.format_id  # the group id is not a tag: it is the same on both rows
        for k, v in ov.items():
            if k in OVERRIDE_KEYS and v is not None:
                t[k] = v
        visual_override = any(k in ov and ov[k] is not None for k in VISUAL_KEYS)
        quality = "member" if visual_override else (t.get("tag_quality") or None)
        t["sound"] = t.get("sound_primary")
        t["text_hook"] = t.get("has_text_hook")
        t["length"] = t.get("length_bucket")
        t["sub_format"] = t.get("format_id")
        t["quality"] = quality
        t["overrides"] = ov
        t["tagged"] = quick is not None or (deep is not None and not quick_only)
        t["has_deep"] = deep is not None
        t["quick"] = dict(getattr(quick, "data", None) or {}) if quick is not None and data else None
        t["deep_data"] = dict(getattr(deep, "data", None) or {}) if deep is not None and data else None
        out[rid] = t
    return out


def _visual_ok(tags: dict, visual: dict | None) -> bool:
    q = tags.get("quality")
    if q in ("vision", "member"):
        return True
    if q == "measured" and visual:
        lv = visual.get("local_vf") or []
        if lv and isinstance(lv[0], dict) and lv[0].get("id") == tags.get("visual_format") and \
                float(lv[0].get("p") or 0) >= 0.8:
            return True
    return False


# ============================================================================================== scoring

def _reel_rows_for_account(conn, user_id: str, account_id: str) -> list:
    """Reels scored on this account: reels.account_id == account (the owner's account when tracked), with the
    account's role on each (owner | coauthor) from reel_accounts."""
    r, ra = db.reels, db.reel_accounts
    q = (select(r, ra.c.role.label("ra_role")).select_from(
        r.outerjoin(ra, and_(ra.c.reel_id == r.c.id, ra.c.account_id == account_id)))
        .where(r.c.user_id == user_id, r.c.account_id == account_id))
    return conn.execute(q).all()


def _metrics(rows, tags: dict[str, dict]) -> list[ol.ReelMetrics]:
    out = []
    for x in rows:
        role = x.ra_role or "owner"
        out.append(ol.ReelMetrics(
            id=x.id, taken_at=x.taken_at, views=x.views, likes=x.likes, comments=x.comments,
            is_pinned=bool(x.is_pinned), owned=(role == "owner" and not x.is_collab), is_collab=bool(x.is_collab),
            followers_at_pull=x.followers_at_pull, caption=x.caption,
            cta_type=(tags.get(x.id) or {}).get("cta_type"), starred=bool(x.starred)))
    return out


def score_account_now(user_id: str, account_id: str, *, now: datetime | None = None) -> dict:
    """Score one account's reels and store the scores (6.6). Returns {"scored", "outliers"}."""
    now = now or _now()
    with db.read() as conn:
        acc = db.get_owned(conn, db.accounts, user_id, account_id)
        if acc is None:
            return {"scored": 0, "outliers": 0, "missing": True}
        rows = _reel_rows_for_account(conn, user_id, account_id)
        tags = effective_tags(conn, user_id, [x.id for x in rows], data=False)
    metrics = _metrics(rows, tags)
    scores = ol.score_account(metrics, now)
    base = ol.baseline_views(metrics, now)
    r = db.reels
    n_scored = n_out = 0
    with db.tx() as conn:
        for s in scores:
            vals = dict(age_days=s.age_days, adj_views=s.adj, baseline=s.baseline, baseline_n=s.baseline_n,
                        score=s.score, log2_score=s.log2_score, percentile=s.percentile, band=s.band,
                        confidence=s.confidence, basis=s.basis, is_top10=bool(s.is_top10), rel_er=s.rel_er,
                        rel_comment_ratio=s.rel_comment_ratio, badges=s.badges, scored_at=now)
            conn.execute(r.update().where(r.c.id == s.reel_id, r.c.user_id == user_id).values(**vals))
            if s.score is not None:
                n_scored += 1
                if ol.band_rank(s.band) >= ol.band_rank("outlier"):
                    n_out += 1
        conn.execute(db.accounts.update().where(db.accounts.c.id == account_id, db.accounts.c.user_id == user_id)
                     .values(baseline_views=base))
        jobs.enqueue(user_id, "pick_sample", {"account_id": account_id}, priority=20,
                     dedupe_key=f"pick_sample:{account_id}", conn=conn)
    return {"scored": n_scored, "outliers": n_out}


@handler("score_account", job_class="light")
def _h_score_account(job, ctx) -> dict:
    acc = (job.payload or {}).get("account_id")
    if not acc:
        return {"scored": 0, "outliers": 0}
    ctx.progress(0, 1, "Scoring reels")
    return score_account_now(job.user_id, acc)


def maybe_rescore(user_id: str) -> int:
    """Re-score accounts whose scores are older than RESCORE_AFTER_H (ages change; 6.6 'nightly'). Cheap; called
    when the member opens Home or the competitor list. Returns how many score jobs were queued."""
    r, a = db.reels, db.accounts
    cutoff = _now() - timedelta(hours=RESCORE_AFTER_H)
    with db.read() as conn:
        rows = conn.execute(select(r.c.account_id, func.max(r.c.scored_at), func.count())
                            .where(r.c.user_id == user_id).group_by(r.c.account_id)).all()
        accs = {x.id for x in conn.execute(select(a.c.id).where(a.c.user_id == user_id)).all()}
        busy = {x[0] for x in conn.execute(select(db.jobs.c.dedupe_key).where(
            db.jobs.c.user_id == user_id, db.jobs.c.kind == "score_account",
            db.jobs.c.state.in_(db.ACTIVE_JOB_STATES))).all()}
    todo = [acc for acc, last, n in rows if acc in accs and n and (last is None or _aware(last) < cutoff)
            and f"score_account:{acc}" not in busy]
    if not todo:
        return 0
    with db.tx() as conn:
        for acc in todo:
            jobs.enqueue(user_id, "score_account", {"account_id": acc}, priority=90,
                         dedupe_key=f"score_account:{acc}", conn=conn)
    return len(todo)


# ============================================================================================== tiers (6.0)

def _scores_from_rows(rows) -> list[ol.Score]:
    out = []
    for x in rows:
        role = x.ra_role or "owner"
        out.append(ol.Score(
            reel_id=x.id, taken_at=x.taken_at, basis=x.basis, age_days=x.age_days, adj=x.adj_views,
            baseline=x.baseline, baseline_n=x.baseline_n, score=x.score, log2_score=x.log2_score,
            percentile=x.percentile, band=x.band or "unscored", confidence=x.confidence, is_top10=bool(x.is_top10),
            rel_er=x.rel_er, rel_comment_ratio=x.rel_comment_ratio, badges=list(x.badges or []),
            owned=(role == "owner" and not x.is_collab), is_pinned=bool(x.is_pinned), is_collab=bool(x.is_collab),
            starred=bool(x.starred), views=x.views))
    return out


def sample_size(user) -> int:
    """baseline_sample_per_account (25); 15 for Claude-connector / desktop members without a tagging key (6.0)."""
    n = int(db.get_setting("baseline_sample_per_account") or 25)
    if user is not None and user.ai_mode in ("claude_connector", "desktop") and not user.tagging_conn_id:
        n = min(n, 15)
    return max(5, n)


def _process_all(user) -> bool:
    if user is None:
        return False
    s = user.settings or {}
    return bool(s.get("process_all")) and (user.ai_mode == "api" or bool(user.tagging_conn_id))


def best_for_deep(user_id: str, account_id: str, n: int = 5) -> list[str]:
    """Top n reels of an account for a deep read: band >= outlier, else top 10% (owned, not pinned, not collab)."""
    with db.read() as conn:
        rows = _reel_rows_for_account(conn, user_id, account_id)
    return ol.best_for_deep(_scores_from_rows(rows), n)


def _advance(user_id: str, reel_id: str) -> None:
    try:
        from . import pipeline

        pipeline.advance(user_id, reel_id)
    except NotImplementedError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("pipeline.advance failed for %s", reel_id)


def _queue_deep(user_id: str, reel_ids: list[str]) -> list[str]:
    if not reel_ids:
        return []
    try:
        from . import work

        return list(work.queue_deep(user_id, reel_ids) or [])
    except NotImplementedError:
        with db.tx() as conn:  # until WP5 lands: mark them so pipeline.advance can pick them up later
            conn.execute(db.reels.update().where(db.reels.c.user_id == user_id, db.reels.c.id.in_(reel_ids),
                                                 db.reels.c.deep_state.in_(("none", "failed")))
                         .values(deep_state="queued"))
        return []
    except Exception:  # noqa: BLE001
        log.exception("work.queue_deep failed")
        return []


def pick_sample_now(user_id: str, account_id: str) -> dict:
    """Set tier + sample_weight for one account's reels (6.0 stage 2), then chain the next steps."""
    with db.read() as conn:
        acc = db.get_owned(conn, db.accounts, user_id, account_id)
        if acc is None:
            return {"priority": 0, "sample": 0, "missing": True}
        user = conn.execute(select(db.users).where(db.users.c.id == user_id)).first()
        rows = _reel_rows_for_account(conn, user_id, account_id)
        last_pull = conn.execute(select(db.pulls.c.id).where(db.pulls.c.user_id == user_id,
                                                              db.pulls.c.account_id == account_id)
                                 .order_by(db.pulls.c.started_at.desc().nulls_last()).limit(1)).first()
    scores = _scores_from_rows(rows)
    previous = {x.id: x.tier for x in rows}
    per = sample_size(user)
    best = ol.best_for_deep(scores, 5)
    seed = f"{account_id}:{last_pull[0] if last_pull else 'none'}"
    tiers = ol.pick_sample(scores, per_account=per, strata=5, seed=seed, previous=previous, best=best,
                           process_all=_process_all(user))
    r = db.reels
    with db.tx() as conn:
        for t in tiers:
            conn.execute(r.update().where(r.c.id == t.reel_id, r.c.user_id == user_id)
                         .values(tier=t.tier, sample_weight=t.weight))
    by_score = {s.reel_id: s for s in scores}
    pri = sorted((t for t in tiers if t.tier == "priority"), key=lambda t: -(by_score[t.reel_id].score or 0))
    samp = [t for t in tiers if t.tier == "sample"]
    # sample reels round-robin over strata, so a partial run is still spread (6.0 stage 3)
    buckets: dict[Any, list] = {}
    for t in sorted(samp, key=lambda t: t.reel_id):
        buckets.setdefault(t.stratum, []).append(t)
    rr = []
    while any(buckets.values()):
        for k in sorted(buckets, key=lambda k: (k is None, k)):
            if buckets[k]:
                rr.append(buckets[k].pop(0))
    states = {x.id: x for x in rows}
    for t in pri + rr:
        x = states.get(t.reel_id)
        if x is not None and x.tag_state == "done" and x.deep_state != "queued":
            continue
        _advance(user_id, t.reel_id)
    if acc.role == "competitor":
        todo = [rid for rid in best if states.get(rid) is not None and states[rid].deep_state in ("none",)]
        _queue_deep(user_id, todo)
    queue_insights(user_id)
    return {"priority": len(pri), "sample": len(samp)}


@handler("pick_sample", job_class="light")
def _h_pick_sample(job, ctx) -> dict:
    acc = (job.payload or {}).get("account_id")
    if not acc:
        return {"priority": 0, "sample": 0}
    ctx.progress(0, 1, "Choosing which reels to study")
    return pick_sample_now(job.user_id, acc)


# ============================================================================================== insights

def queue_insights(user_id: str, *, delay_s: float | None = None, conn=None) -> str:
    """Debounced refresh_insights: one queued job per member, run `delay_s` (60 s) after the first change."""
    delay = INSIGHTS_DEBOUNCE_S if delay_s is None else delay_s

    def run(c) -> str:
        got = c.execute(select(db.jobs.c.id).where(db.jobs.c.user_id == user_id,
                                                    db.jobs.c.kind == "refresh_insights",
                                                    db.jobs.c.state == "queued")).first()
        if got:
            return got[0]
        return jobs.enqueue(user_id, "refresh_insights", {}, priority=60,
                            run_after=_now() + timedelta(seconds=delay) if delay > 0 else None, conn=c)

    if conn is not None:
        return run(conn)
    with db.tx() as c:
        return run(c)


def maybe_refresh(user_id: str) -> str | None:
    """Safety net: queue a (debounced) refresh when tags or scores changed after the latest snapshot, even if the
    code that changed them did not call queue_insights. Cheap; called when the member opens Home or insights."""
    a, r, s = db.analyses, db.reels, db.insight_snapshots
    with db.read() as conn:
        last = conn.execute(select(func.max(s.c.computed_at)).where(s.c.user_id == user_id)).scalar()
        tagged = conn.execute(select(func.max(a.c.updated_at)).where(a.c.user_id == user_id)).scalar()
        scored = conn.execute(select(func.max(r.c.scored_at)).where(r.c.user_id == user_id)).scalar()
        waiting = conn.execute(select(db.jobs.c.id).where(db.jobs.c.user_id == user_id,
                                                          db.jobs.c.kind == "refresh_insights",
                                                          db.jobs.c.state.in_(("queued", "running")))).first()
    if waiting is not None:
        return waiting[0]
    newest = max([_aware(x) for x in (tagged, scored) if x is not None], default=None)
    if newest is None or (last is not None and newest <= _aware(last)):
        return None
    return queue_insights(user_id)


def profile_digest(conn, user_id: str) -> dict:
    row = conn.execute(select(db.profiles.c.digest).where(db.profiles.c.user_id == user_id,
                                                          db.profiles.c.brand_id == "me")).first()
    return dict(row[0] or {}) if row and row[0] else {}


def capabilities(conn, user_id: str) -> dict | None:
    caps = profile_digest(conn, user_id).get("capabilities")
    return caps if isinstance(caps, dict) and caps else None


def topic_labels(conn, user_id: str) -> dict:
    t = db.topics
    return {x.slug: x.label for x in conn.execute(select(t.c.slug, t.c.label).where(t.c.user_id == user_id)).all()}


def _thumb(user_id: str, cover_path: str | None) -> str | None:
    if not cover_path:
        return None
    try:
        return storage.files_url(user_id, cover_path)
    except Exception:  # noqa: BLE001
        return None


def build_units(conn, user_id: str, account_ids: list[str] | None = None) -> tuple[list[ins.Unit], dict]:
    """Every priority/sample reel of the member's tracked accounts as an insights Unit (weights recomputed at
    insight time, 6.7) + the DB counts for the data note."""
    r, a = db.reels, db.accounts
    accs = {x.id: x for x in conn.execute(db.scoped(conn, a, user_id)).all()}
    if account_ids is not None:
        keep = set(account_ids)
        accs = {k: v for k, v in accs.items() if k in keep or v.role == "self"}
    rows = conn.execute(select(r.c.id, r.c.account_id, r.c.taken_at, r.c.score, r.c.is_top10, r.c.tier,
                               r.c.is_collab, r.c.is_pinned, r.c.band, r.c.counts_hidden, r.c.duration, r.c.url,
                               r.c.cover_path, r.c.audio, r.c.tag_quality)
                        .where(r.c.user_id == user_id, r.c.account_id.in_(list(accs) or ["-"]))).all()
    ids = [x.id for x in rows if x.tier in ("priority", "sample")]
    tags = effective_tags(conn, user_id, ids, data=False, quick_only=True)  # one tagger for every unit
    sig = {}
    if ids:
        rs = db.reel_signals
        for x in conn.execute(select(rs.c.reel_id, rs.c.visual, rs.c.edit).where(
                rs.c.user_id == user_id, rs.c.reel_id.in_(ids))).all():
            sig[x.reel_id] = (x.visual or {}, x.edit or {})
    # sample weights per account: strata over eligible non-priority reels, tagged sample reels per stratum
    weights: dict[str, float] = {}
    by_acc: dict[str, list] = {}
    for x in rows:
        by_acc.setdefault(x.account_id, []).append(x)
    for acc_id, xs in by_acc.items():
        elig = [x for x in xs if x.tier in ("sample", "rest") and x.score is not None and not x.is_collab
                and not x.is_pinned]
        n_pri = sum(1 for x in xs if x.tier == "priority")
        sc = [ol.Score(reel_id=x.id, taken_at=x.taken_at, basis=None, age_days=None, adj=None, baseline=None,
                       baseline_n=None, score=x.score, log2_score=None, percentile=None, band=x.band or "",
                       confidence=None) for x in elig]
        strata = ol.strata_of(sc, 5, n_account=len(elig) + n_pri)
        sizes: dict[int, int] = {}
        for k in strata.values():
            sizes[k] = sizes.get(k, 0) + 1
        tagged: dict[int, list[str]] = {}
        for x in elig:
            if x.tier == "sample" and (tags.get(x.id) or {}).get("tagged"):
                tagged.setdefault(strata.get(x.id, 0), []).append(x.id)
        weights.update(ins.sample_weights(sizes, tagged))
    units = []
    for x in rows:
        if x.tier not in ("priority", "sample") or x.score is None:
            continue
        acc = accs.get(x.account_id)
        t = tags.get(x.id)
        if acc is None or not t or not t.get("tagged"):
            continue
        visual, edit = sig.get(x.id, ({}, {}))
        tt = dict(t)
        if not tt.get("cut_pace"):
            tt["cut_pace"] = edit.get("cut_pace")
        if not tt.get("length"):
            tt["length"] = edit.get("length_bucket") or ins.length_bucket(x.duration)
        if tt.get("quality") is None:
            tt["quality"] = x.tag_quality
        w = 1.0 if x.tier == "priority" else weights.get(x.id, 0.0)
        units.append(ins.Unit(
            reel_id=x.id, account_id=x.account_id, handle=acc.handle, score=x.score, weight=w, tier=x.tier,
            taken_at=x.taken_at, is_self=(acc.role == "self"), is_collab=bool(x.is_collab),
            is_top10=bool(x.is_top10), tagged=True, visual_ok=_visual_ok(tt, visual), tags=tt,
            duration=x.duration, url=x.url, thumb=_thumb(user_id, x.cover_path), audio=x.audio or {}))
    # data-note counts (competitor accounts only)
    comp_ids = {k for k, v in accs.items() if v.role == "competitor"}
    cr = [x for x in rows if x.account_id in comp_ids]
    dates = sorted(_aware(x.taken_at) for x in cr if x.taken_at is not None)
    note = {"reels_pulled": len(cr), "reels_scored": sum(1 for x in cr if x.score is not None),
            "priority": sum(1 for x in cr if x.tier == "priority"),
            "sample": sum(1 for x in cr if x.tier == "sample"),
            "not_processed": sum(1 for x in cr if x.tier in ("rest", "none")),
            "excluded": {"too_new": sum(1 for x in cr if x.band == "growing"),
                         "pinned": sum(1 for x in cr if x.is_pinned),
                         "hidden": sum(1 for x in cr if x.counts_hidden),
                         "collab": sum(1 for x in cr if x.is_collab),
                         "untagged_priority": sum(1 for x in cr if x.tier == "priority"
                                                  and not (tags.get(x.id) or {}).get("tagged"))},
            "date_range": [dates[0].date().isoformat(), dates[-1].date().isoformat()] if dates else None}
    self_acc = next((v for v in accs.values() if v.role == "self"), None)
    note["self_scored"] = sum(1 for x in rows if self_acc is not None and x.account_id == self_acc.id
                              and x.score is not None)
    music_in = [{"id": x.id, "url": x.url, "audio": x.audio, "score": x.score,
                 "is_hit": (x.score or 0) >= ol.HIT} for x in cr if x.tier == "priority" and not x.is_collab]
    note["_music"] = ins.music_rows(music_in)
    return units, note


def _formats_map(conn, user_id: str) -> dict[str, Any]:
    f = db.formats
    return {x.recipe_key: x for x in conn.execute(db.scoped(conn, f, user_id).where(f.c.brand_id == "me")).all()}


def _compute_all(units, note, *, formats: dict, caps, labels, settings: dict) -> dict:
    wd = int(settings.get("insight_window_days") or 365)
    rd = int(settings.get("recent_window_days") or 90)
    per = int(settings.get("baseline_sample_per_account") or 25)
    now = _now()
    common = dict(formats=formats, capabilities=caps, topic_labels=labels, self_scored=note.get("self_scored", 0),
                  per_account=per, now=now, recent_days=rd)
    data = ins.compute(units, window_days=wd, **common)
    windows = {}
    for name, days in (("all", None), ("recent", rd)):
        d = ins.compute(units, window_days=days, **common)
        windows[name] = {k: d[k] for k in ("N", "W", "accounts", "H0", "hit_rule", "window_days", "rows", "grid",
                                           "combos")}
    data["windows"] = windows
    data["music"] = note.get("_music", [])
    dn = data["data_note"]
    extra = {k: v for k, v in note.items() if not k.startswith("_") and k != "self_scored"}
    ex = dict(extra.pop("excluded", {}))
    ex.update(dn.get("excluded", {}))
    dn.update(extra)
    dn["excluded"] = ex
    return data


def refresh(user_id: str, *, store: bool = True, account_ids: list[str] | None = None) -> dict:
    """Recompute the member's insights (6.7) and sub-formats (6.9). store=True writes formats rows, analyses.format_id
    and a new insight_snapshots row, and queues AI naming for new or grown sub-formats. Returns the Insights JSON
    (with "snapshot_id" when stored)."""
    settings = db.all_settings()
    with db.read() as conn:
        units, note = build_units(conn, user_id, account_ids)
        caps = capabilities(conn, user_id)
        labels = topic_labels(conn, user_id)
        fmap = _formats_map(conn, user_id)
    groups = ins.sub_formats(units)
    key_to_id: dict[str, str] = {g.recipe_key: fmap[g.recipe_key].id for g in groups if g.recipe_key in fmap}
    if store:
        new = [g for g in groups if g.recipe_key not in key_to_id]
        if new:
            with db.tx() as conn:
                have = _formats_map(conn, user_id)
                for g in new:
                    if g.recipe_key in have:
                        key_to_id[g.recipe_key] = have[g.recipe_key].id
                        continue
                    fid = db.new_id("fmt")
                    conn.execute(db.formats.insert().values(
                        id=fid, user_id=user_id, brand_id="me", recipe_key=g.recipe_key, vf=g.vf, recipe=g.recipe,
                        stats={"active": False}, example_reels=g.example_reels, needs=ins.vf_needs(g.vf),
                        computed_at=_now()))
                    key_to_id[g.recipe_key] = fid
    else:
        for g in groups:
            key_to_id.setdefault(g.recipe_key, f"tmp_{g.recipe_key}")
    member_of: dict[str, str] = {}
    for g in groups:
        for rid in g.reel_ids:
            member_of[rid] = key_to_id[g.recipe_key]
    for u in units:
        u.tags["sub_format"] = member_of.get(u.reel_id)
    fdict = {}
    for g in groups:
        fid = key_to_id[g.recipe_key]
        row = fmap.get(g.recipe_key)
        fdict[fid] = {"name": row.name if row is not None else None, "vf": g.vf, "recipe": g.recipe,
                      "example_reels": g.example_reels, "recipe_key": g.recipe_key}
    data = _compute_all(units, note, formats=fdict, caps=caps, labels=labels, settings=settings)
    if not store:
        return data
    sub_rows = {r["value"]: r for r in data["rows"].get("sub_format", [])}
    now = _now()
    to_name: list[str] = []
    with db.tx() as conn:
        f = db.formats
        current = _formats_map(conn, user_id)
        live_ids = set()
        for g in groups:
            fid = key_to_id[g.recipe_key]
            live_ids.add(fid)
            row = current.get(g.recipe_key)
            r = sub_rows.get(fid) or {}
            old = dict(row.stats or {}) if row is not None else {}
            stats = {"n": g.n, "k": g.k, "hits": g.hits, "lift": r.get("lift"), "typical": r.get("typical"),
                     "evidence": r.get("evidence") or ("strong" if g.n >= 10 and g.k >= 3 else
                                                       "promising" if g.n >= 5 and g.k >= 2 else "thin"),
                     "rising": r.get("trend") == "rising", "trend": r.get("trend"),
                     "share_recent": None, "usage": r.get("usage"), "active": True,
                     "line": None}
            if r.get("n"):
                stats["share_recent"] = round((r.get("n_recent") or 0) / r["n"], 3)
            if row is not None and row.named_at is not None:
                stats["n_named"] = old.get("n_named") or g.n
            elif "n_named" in old:
                stats["n_named"] = old["n_named"]
            conn.execute(f.update().where(f.c.id == fid, f.c.user_id == user_id).values(
                vf=g.vf, recipe=g.recipe, stats=stats, example_reels=g.example_reels, needs=ins.vf_needs(g.vf),
                computed_at=now))
            named = row is not None and row.name
            if not named or (stats.get("n_named") and g.n >= 2 * int(stats["n_named"])):
                to_name.append(fid)
        for key, row in current.items():
            if row.id not in live_ids and (row.stats or {}).get("active", True):
                st_ = dict(row.stats or {})
                st_["active"] = False
                conn.execute(f.update().where(f.c.id == row.id).values(stats=st_))
        a = db.analyses
        conn.execute(a.update().where(a.c.user_id == user_id, a.c.format_id.is_not(None),
                                      a.c.reel_id.not_in(list(member_of) or ["-"])).values(format_id=None))
        by_fid: dict[str, list[str]] = {}
        for rid, fid in member_of.items():
            by_fid.setdefault(fid, []).append(rid)
        for fid, rids in by_fid.items():
            conn.execute(a.update().where(a.c.user_id == user_id, a.c.reel_id.in_(rids)).values(format_id=fid))
        sid = db.new_id("snap")
        conn.execute(db.insight_snapshots.insert().values(id=sid, user_id=user_id, computed_at=now,
                                                          reel_count=data["N"], account_count=data["accounts"],
                                                          data=data))
        keep = [x[0] for x in conn.execute(select(db.insight_snapshots.c.id).where(
            db.insight_snapshots.c.user_id == user_id).order_by(db.insight_snapshots.c.computed_at.desc())
            .limit(SNAPSHOTS_KEPT)).all()]
        conn.execute(db.insight_snapshots.delete().where(db.insight_snapshots.c.user_id == user_id,
                                                         db.insight_snapshots.c.id.not_in(keep)))
        busy = conn.execute(select(db.jobs.c.id).where(db.jobs.c.user_id == user_id,
                                                       db.jobs.c.kind == "name_formats",
                                                       db.jobs.c.state.in_(db.ACTIVE_JOB_STATES))).first()
    data["snapshot_id"] = sid
    if to_name and not busy:
        _queue_names(user_id, to_name)
    return data


def _queue_names(user_id: str, ids: list[str]) -> None:
    try:
        from . import work

        for i in range(0, len(ids), 8):
            work.queue_format_names(user_id, ids[i:i + 8])
    except NotImplementedError:
        pass  # WP5 not there yet: FormatCards use insights.fallback_name
    except Exception:  # noqa: BLE001
        log.exception("queue_format_names failed")


@handler("refresh_insights", job_class="light")
def _h_refresh(job, ctx) -> dict:
    ctx.progress(0, 1, "Finding what works")
    data = refresh(job.user_id)
    return {"snapshot_id": data.get("snapshot_id"), "units": data.get("N")}


def report_numbers(user_id: str, account_ids: list[str] | None) -> dict:
    """Insights JSON (6.7.5) for a subset of competitors (competitor_report, 8.4). Nothing is stored."""
    return refresh(user_id, store=False, account_ids=account_ids)


def latest_snapshot(conn, user_id: str):
    s = db.insight_snapshots
    return conn.execute(db.scoped(conn, s, user_id).order_by(s.c.computed_at.desc()).limit(1)).first()


# ============================================================================================== discovery (6.6.1)

_QUERY_STRIP = re.compile(r"[^A-Za-z0-9 &\-'.]+")


def _clean_query(q: Any) -> str | None:
    if not isinstance(q, str):
        return None
    q = re.sub(r"\s+", " ", _QUERY_STRIP.sub(" ", q)).strip()[:60].strip()
    return q if len(q) >= 2 else None


def niche_queries(conn, user_id: str) -> list[str]:
    d = profile_digest(conn, user_id)
    raw: list[str] = []
    raw += [x for x in (d.get("search_terms") or []) if isinstance(x, str)]
    if d.get("niche_label"):
        raw.append(d["niche_label"])
    raw += [x for x in (d.get("top_pains") or [])[:1] if isinstance(x, str)]
    if not raw:
        row = conn.execute(select(db.profiles.c.answers).where(db.profiles.c.user_id == user_id,
                                                               db.profiles.c.brand_id == "me")).first()
        q1 = ((row[0] or {}).get("q1") or {}).get("value") if row else None
        if isinstance(q1, str):
            raw.append(re.sub(r"(?i)^i help\s+", "", q1).split(" get ")[0])
    out = []
    for q in raw:
        c = _clean_query(q)
        if c and c.lower() not in {x.lower() for x in out}:
            out.append(c)
    return out[:3]


def start_discover(user_id: str, mode: str, *, query: str | None = None, seeds: list | None = None) -> str:
    """Create a discover run and queue ig_discover (WP2 chains ig_check on the best <= 15). Returns the run id."""
    from . import jobs_ig

    if mode not in ("niche", "similar", "search"):
        raise bad_input("Pick how to search: your niche, similar accounts, or your own words.")
    queries: list[str] = []
    seed_items: list[dict] = []
    with db.read() as conn:
        if mode == "niche":
            queries = niche_queries(conn, user_id)
            if not queries:
                raise bad_input("Fill in your profile first, or type a few words to search for.")
        elif mode == "search":
            q = _clean_query(query)
            if not q:
                raise bad_input("Type a few words to search for, like \"facebook ads agency\".")
            queries = [q]
        else:
            accs = {x.handle: x for x in conn.execute(db.scoped(conn, db.accounts, user_id)).all()}
            handles = []
            for s in (seeds or [])[:2]:
                h = s.get("handle") if isinstance(s, dict) else s
                h = parse_handle(h) if isinstance(h, str) else None
                if h is None:
                    raise bad_input("That doesn't look like an Instagram account. Use @name or a profile link.")
                handles.append(h)
            if not handles:
                comp = sorted((x for x in accs.values() if x.role == "competitor"),
                              key=lambda x: -(x.followers or 0))[:2]
                handles = [x.handle for x in comp]
            if not handles:
                raise bad_input("Add one or two accounts whose reels work in your niche, then try again.")
            for h in handles:
                item = {"handle": h}
                if h in accs and accs[h].ig_pk:
                    item["pk"] = accs[h].ig_pk
                seed_items.append(item)
    run_id = db.new_id("drun")
    with db.tx() as conn:
        conn.execute(db.discover_runs.insert().values(id=run_id, user_id=user_id, mode=mode, queries=queries,
                                                      seeds=seed_items, state="queued", created_at=_now()))
    try:
        jobs_ig.request_discover(user_id, run_id, mode, queries=queries or None, seeds=seed_items or None)
    except Exception:
        with db.tx() as conn:
            conn.execute(db.discover_runs.update().where(db.discover_runs.c.id == run_id).values(state="failed"))
        raise
    return run_id


HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")
_URL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?instagram\.com/([^/?#]+)", re.I)
_RESERVED = {"reel", "reels", "p", "explore", "stories", "accounts", "tv", "direct"}


def parse_handle(s: str) -> str | None:
    """'@alexhormozi', 'https://instagram.com/alexhormozi/?igsh=..' -> 'alexhormozi'; None when not a handle."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    m = _URL_RE.match(s)
    if m:
        s = m.group(1)
    s = s.strip().lstrip("@").strip().lower().rstrip("/")
    if not HANDLE_RE.match(s) or s in _RESERVED or s.startswith(".") or s.endswith(".") or ".." in s:
        return None
    return s


def _hidden_handles(conn, user_id: str) -> set[str]:
    return {x[0] for x in conn.execute(select(db.accounts.c.handle).where(db.accounts.c.user_id == user_id)).all()}


def pick_check_candidates(user_id: str, run_id: str) -> list[dict]:
    """Which found candidates get the light check (ig_check, <= 15): not private, not tracked, not the member's own
    account; biggest audiences first, seeds' similar accounts interleaved so both seeds are covered."""
    dc = db.discover_candidates
    with db.read() as conn:
        hidden = _hidden_handles(conn, user_id)
        rows = conn.execute(db.scoped(conn, dc, user_id).where(dc.c.run_id == run_id, dc.c.status == "found",
                                                               dc.c.is_private.is_(False))).all()
    rows = [x for x in rows if x.handle not in hidden]
    by_seed: dict[str, list] = {}
    for x in sorted(rows, key=lambda x: (-(x.followers or 0), x.handle)):
        by_seed.setdefault(x.seed_handle or "", []).append(x)
    picked = []
    while len(picked) < CHECK_MAX and any(by_seed.values()):
        for k in sorted(by_seed):
            if by_seed[k] and len(picked) < CHECK_MAX:
                picked.append(by_seed[k].pop(0))
    return [{"cand_id": x.id, "handle": x.handle, **({"pk": x.pk} if x.pk else {})} for x in picked]


def ingest_candidate(user_id: str, cand_id: str, account: dict, items: list[dict]) -> None:
    """ig_check upload (profile + first clips page of <= 12 reels) -> median views, posting rate, rank (6.6.1)."""
    account = account or {}
    followers = account.get("followers")
    rank = ol.candidate_rank(followers, items or [], _now())
    vals = dict(median_views_12=rank["median_views_12"], posts_per_week=rank["posts_per_week"],
                last_post_at=rank["last_post_at"], rank_score=rank["rank_score"], checked_at=_now(),
                status="checked")
    for k_src, k_dst in (("pk", "pk"), ("full_name", "full_name"), ("followers", "followers")):
        if account.get(k_src) is not None:
            vals[k_dst] = account[k_src]
    vals["is_private"] = bool(account.get("is_private"))
    vals["is_verified"] = bool(account.get("is_verified"))
    with db.tx() as conn:
        row = db.get_owned(conn, db.discover_candidates, user_id, cand_id)
        if row is None:
            raise not_found()
        if row.status in ("tracked", "dismissed"):
            vals.pop("status")
        conn.execute(db.discover_candidates.update().where(db.discover_candidates.c.id == cand_id,
                                                           db.discover_candidates.c.user_id == user_id).values(**vals))


def after_check(user_id: str, run_id: str) -> None:
    """Called by WP2 when ig_check is done: the run is ready to show."""
    with db.tx() as conn:
        conn.execute(db.discover_runs.update().where(db.discover_runs.c.id == run_id,
                                                     db.discover_runs.c.user_id == user_id).values(state="done"))


def rank_candidates(conn, user_id: str, run_id: str) -> list:
    """The run's candidates to show: checked ones by rank_score, then unchecked by followers. Tracked, private,
    dismissed and the member's own account are left out."""
    dc = db.discover_candidates
    hidden = _hidden_handles(conn, user_id)
    rows = conn.execute(db.scoped(conn, dc, user_id).where(dc.c.run_id == run_id)).all()
    rows = [x for x in rows if not x.is_private and x.handle not in hidden and x.status in ("found", "checked")]
    rows.sort(key=lambda x: (x.checked_at is None, -(x.rank_score if x.rank_score is not None else -1e9),
                             -(x.followers or 0), x.handle))
    return rows


def candidate_json(x) -> dict:
    d = {"id": x.id, "handle": x.handle, "full_name": x.full_name, "followers": x.followers,
         "median_views_12": x.median_views_12, "posts_per_week": x.posts_per_week,
         "last_post_at": db.iso(x.last_post_at), "source": x.source, "seed_handle": x.seed_handle,
         "status": x.status, "checked": x.checked_at is not None, "is_verified": bool(x.is_verified),
         "not_posting": x.rank_score is not None and x.rank_score < -60, "pic_url": None,
         "url": f"https://www.instagram.com/{x.handle}/"}
    d["why"] = ol.candidate_why({"median_views_12": x.median_views_12, "posts_per_week": x.posts_per_week,
                                 "followers": x.followers, "checked_at": x.checked_at, "rank_score": x.rank_score})
    return d


def median_or_none(xs: list[float]) -> float | None:
    return st.median(xs) if xs else None
