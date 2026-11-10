"""/api/reels/*, /api/insights/*, /api/banks/*, /api/formats/* (DESIGN-SAAS.md 4.9) + the Home parts (4.11).
WP4 owns this file.

Effective tags everywhere = member override ?? deep analysis ?? quick tags (jobs_score.effective_tags).
Ownership is checked before the request body is read (another member's ids -> 404).
"""
from __future__ import annotations

import base64
import logging
import re
import statistics as st
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import func, select

from .. import insights as ins
from .. import outliers as ol
from . import db, jobs, security, storage
from . import jobs_score as js
from .errors import bad_input, not_found

log = logging.getLogger("reelvault.reels")
router = APIRouter()

SAVES_LINE = ins.SAVES_LINE
VF_IDS = tuple(f"VF{i:02d}" for i in range(1, 21)) + ("other",)
SD_IDS = tuple(f"SD{i:02d}" for i in range(1, 17))
OVERRIDE_CHOICES = {
    "visual_format": VF_IDS, "sound_primary": SD_IDS, "hook_type": tuple(ins.HOOK_LABELS),
    "cta_type": tuple(ins.CTA_LABELS), "caption_style": tuple(ins.CAPTION_LABELS),
    "broll_share": tuple(ins.BROLL_LABELS), "setting": tuple(ins.SETTING_LABELS), "framing": tuple(ins.FRAMING_LABELS),
}
OVERRIDE_BOOLS = ("headline_box", "face_bubble")
PURPOSES = ("remake", "format", "hook", "learn")
RECENT_DAYS = 90


def _body(b: Any) -> dict:
    if b is None:
        return {}
    if not isinstance(b, dict):
        raise bad_input("Send a JSON object.")
    return b


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _files(user_id: str, rel: str | None) -> str | None:
    if not rel:
        return None
    try:
        return storage.files_url(user_id, rel)
    except Exception:  # noqa: BLE001
        return None


def _first_line(caption: str | None) -> str:
    for line in (caption or "").splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return ""


def _bool(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes", "on")


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _cursor_in(c: Any) -> int:
    if not c:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(str(c).encode() + b"==").decode()))
    except Exception:  # noqa: BLE001
        raise bad_input("That page link is broken. Start from the first page.") from None


def _cursor_out(n: int) -> str:
    return base64.urlsafe_b64encode(str(n).encode()).decode().rstrip("=")


# ============================================================================================== ReelSummary

def _accounts(conn, user_id: str) -> dict:
    return {x.id: x for x in conn.execute(db.scoped(conn, db.accounts, user_id)).all()}


def _format_names(conn, user_id: str) -> dict[str, dict]:
    f = db.formats
    out = {}
    for x in conn.execute(select(f.c.id, f.c.name, f.c.vf, f.c.recipe, f.c.stats).where(f.c.user_id == user_id)).all():
        out[x.id] = {"name": x.name or ins.fallback_name(x.vf, x.recipe or {}), "named": bool(x.name), "vf": x.vf,
                     "active": (x.stats or {}).get("active", True)}
    return out


def _actions(x, t: dict | None) -> list[str]:
    acts = []
    if ol.band_rank(x.band) >= ol.band_rank("above") or x.starred:
        acts.append("remake")
    if t and t.get("visual_format") and t.get("visual_format") != "other":
        acts.append("use_format")
    if (t and t.get("hook_type")) or (x.score is not None and x.score >= 2):
        acts.append("borrow_hook")
    return acts


def reel_summary(user_id: str, x, accs: dict, tags: dict, fmts: dict, *, now: datetime | None = None) -> dict:
    acc = accs.get(x.account_id)
    t = tags.get(x.id)
    sc = {"band": x.band, "score": x.score, "basis": x.basis, "rel_er": x.rel_er,
          "age_days": ((_aware(now or db.utcnow()) - _aware(x.taken_at)).total_seconds() / 86400.0)
          if x.taken_at is not None else x.age_days}
    out = {"id": x.id, "account": {"id": x.account_id, "handle": acc.handle if acc else None,
                                   "role": acc.role if acc else None},
           "url": x.url, "code": x.code, "taken_at": db.iso(x.taken_at), "duration": x.duration,
           "caption_first_line": _first_line(x.caption), "views": x.views, "likes": x.likes, "comments": x.comments,
           "shares": x.shares, "score": round(x.score, 2) if x.score is not None else None, "band": x.band,
           "band_label": ol.band_label(x.band), "confidence": x.confidence,
           "percentile": x.percentile, "basis": x.basis, "badges": list(x.badges or []),
           "badge_labels": [ol.BADGE_LABELS.get(b, b) for b in (x.badges or [])], "why": ol.why(sc),
           "thumb": _files(user_id, x.cover_path), "is_collab": bool(x.is_collab), "tier": x.tier,
           "actions": _actions(x, t),
           "states": {"media": x.media_state, "signals": x.signals_state, "tags": x.tag_state, "deep": x.deep_state},
           "starred": bool(x.starred)}
    if t and t.get("tagged"):
        fid = t.get("sub_format")
        tg = {"visual_format": t.get("visual_format"), "visual_format_label": ins.vf_label(t.get("visual_format"))
              if t.get("visual_format") else None, "sound": t.get("sound"),
              "sound_label": ins.sd_label(t.get("sound")) if t.get("sound") else None,
              "topics": list(t.get("topics") or []), "hook_type": t.get("hook_type"), "cta_type": t.get("cta_type"),
              "quality": t.get("quality"), "unverified": t.get("quality") == "measured"}
        if fid and fid in fmts:
            tg["format"] = {"id": fid, "name": fmts[fid]["name"]}
        out["tags"] = tg
    return out


def _reel_cols():
    r = db.reels
    return [r.c.id, r.c.account_id, r.c.url, r.c.code, r.c.taken_at, r.c.duration, r.c.caption, r.c.views, r.c.likes,
            r.c.comments, r.c.shares, r.c.score, r.c.band, r.c.confidence, r.c.percentile, r.c.basis, r.c.badges,
            r.c.rel_er, r.c.age_days, r.c.cover_path, r.c.is_collab, r.c.tier, r.c.media_state, r.c.signals_state,
            r.c.tag_state, r.c.deep_state, r.c.starred, r.c.first_seen_at, r.c.is_pinned, r.c.is_top10]


def pipeline_state(conn, user) -> dict:
    r = db.reels
    left = conn.execute(select(func.count()).where(r.c.user_id == user.id, r.c.tier.in_(("priority", "sample")),
                                                   r.c.tag_state != "done")).scalar() or 0
    eta = None
    try:
        from . import budget

        e = budget.eta(user.id)
        eta = e["text"] if e else None
    except Exception:  # noqa: BLE001
        eta = None
    if not left:
        line = None
    elif user.ai_mode is None and not user.tagging_conn_id:
        line = f"Connect your AI to tag {left} reels."
    elif user.ai_mode in ("claude_connector", "desktop") and not user.tagging_conn_id:
        who = "Claude" if user.ai_mode == "claude_connector" else "the desktop app"
        line = f"{left} reels left to tag · waiting for {who}"
    else:
        line = f"{left} reels left to tag" + (f" · {eta[0].lower()}{eta[1:]}" if eta else "")
    return {"left_to_tag": int(left), "eta": eta, "line": line}


def list_reels(user, params: dict) -> dict:
    """GET /api/reels (also the admin support mirror)."""
    r = db.reels
    limit = _int(params.get("limit"), 50, 1, 200)
    offset = _cursor_in(params.get("cursor"))
    q = select(*_reel_cols()).where(r.c.user_id == user.id)
    if params.get("account"):
        q = q.where(r.c.account_id == params["account"])
    bands = [b for b in str(params.get("band") or "").split(",") if b]
    if bands:
        q = q.where(r.c.band.in_(bands))
    if params.get("min_score") not in (None, ""):
        try:
            q = q.where(r.c.score >= float(params["min_score"]))
        except ValueError:
            raise bad_input("min_score must be a number.") from None
    if _bool(params.get("starred")):
        q = q.where(r.c.starred.is_(True))
    if _bool(params.get("recent")):
        q = q.where(r.c.taken_at >= db.utcnow() - timedelta(days=RECENT_DAYS))
    purpose = params.get("purpose")
    if purpose and purpose not in PURPOSES:
        raise bad_input("purpose must be remake, format, hook or learn.")
    if purpose == "remake":
        q = q.where(r.c.band.in_(ol.OUTLIER_BANDS))
    elif purpose == "learn":
        q = q.where(r.c.deep_state == "done")
    sort = params.get("sort") or "score"
    if sort not in ("score", "newest", "views"):
        raise bad_input("sort must be score, newest or views.")
    if sort == "score":
        q = q.order_by(r.c.score.desc().nulls_last(), r.c.taken_at.desc().nulls_last(), r.c.id)
    elif sort == "newest":
        q = q.order_by(r.c.taken_at.desc().nulls_last(), r.c.id)
    else:
        q = q.order_by(r.c.views.desc().nulls_last(), r.c.id)
    with db.read() as conn:
        accs = _accounts(conn, user.id)
        rows = conn.execute(q).all()
        text = (params.get("q") or "").strip().lower()
        if text:
            rows = [x for x in rows if text in (x.caption or "").lower()
                    or text in ((accs.get(x.account_id).handle if accs.get(x.account_id) else "") or "")]
        need_tags = any(params.get(k) for k in ("format", "sub_format", "sound", "topic", "hook_type")) or \
            purpose in ("format", "hook")
        tags = js.effective_tags(conn, user.id, None, data=False)
        fmts = _format_names(conn, user.id)
        if need_tags:
            def ok(x) -> bool:
                t = tags.get(x.id) or {}
                if params.get("format") and t.get("visual_format") != params["format"]:
                    return False
                if params.get("sub_format") and t.get("sub_format") != params["sub_format"]:
                    return False
                if params.get("sound") and t.get("sound") != params["sound"]:
                    return False
                if params.get("hook_type") and t.get("hook_type") != params["hook_type"]:
                    return False
                tp = params.get("topic")
                if tp == "unsorted":
                    if not t.get("tagged") or (t.get("topics") or []):
                        return False
                elif tp and tp not in (t.get("topics") or []):
                    return False
                if purpose == "format" and not (t.get("sub_format") or t.get("visual_format")):
                    return False
                if purpose == "hook" and not (t.get("hook_type") or (x.score or 0) >= 2):
                    return False
                return True

            rows = [x for x in rows if ok(x)]
        counts = {"total": len(rows), "outlier": sum(1 for x in rows if x.band == "outlier"),
                  "big": sum(1 for x in rows if x.band == "big"),
                  "breakout": sum(1 for x in rows if x.band == "breakout")}
        page = rows[offset:offset + limit]
        now = db.utcnow()
        out = [reel_summary(user.id, x, accs, tags, fmts, now=now) for x in page]
        pipe = pipeline_state(conn, user)
    nxt = _cursor_out(offset + limit) if offset + limit < len(rows) else None
    return {"reels": out, "next_cursor": nxt, "counts": counts, "pipeline": pipe, "saves_line": SAVES_LINE}


@router.get("/api/reels")
def get_reels(request: Request, user=Depends(security.current_user)):
    return list_reels(user, dict(request.query_params))


def _get_reel(conn, user_id: str, reel_id: str):
    row = db.get_owned(conn, db.reels, user_id, reel_id)
    if row is None:
        raise not_found("We couldn't find that reel.")
    return row


def _signals_view(sig: dict | None) -> dict | None:
    if not sig:
        return None
    visual = dict(sig.get("visual") or {})
    visual.pop("per_frame", None)
    visual.pop("sheet_bytes", None)
    return {"transcript": sig.get("transcript"), "cuts": sig.get("cuts"), "sound": sig.get("sound"),
            "edit": sig.get("edit"), "visual": visual, "version": sig.get("version"), "error": sig.get("error")}


def reel_detail(conn, user, x) -> dict:
    accs = _accounts(conn, user.id)
    tags = js.effective_tags(conn, user.id, [x.id])
    fmts = _format_names(conn, user.id)
    out = reel_summary(user.id, x, accs, tags, fmts)
    t = tags.get(x.id) or {}
    rs = db.reel_snapshots
    snaps = conn.execute(select(rs.c.seen_at, rs.c.views, rs.c.likes, rs.c.comments, rs.c.shares)
                         .where(rs.c.user_id == user.id, rs.c.reel_id == x.id).order_by(rs.c.seen_at)).all()
    sig_row = db.get_owned(conn, db.reel_signals, user.id, x.id, id_col="reel_id")
    sig = dict(sig_row._mapping) if sig_row is not None else None
    frames = []
    for fr in (sig or {}).get("frames") or []:
        if isinstance(fr, dict) and fr.get("path"):
            frames.append({"url": _files(user.id, f"reels/{x.id}/{fr['path']}"), "t": fr.get("t"),
                           "kind": fr.get("kind"), "n": fr.get("n")})
    media = {"video": _files(user.id, x.video_path) if x.media_state == "have" else None,
             "cover": _files(user.id, x.cover_path), "frames": frames,
             "sheet": _files(user.id, f"reels/{x.id}/{sig['contact_sheet_path']}")
             if sig and sig.get("contact_sheet_path") else None}
    ra = db.reel_accounts
    links = conn.execute(select(ra.c.account_id, ra.c.role).where(ra.c.user_id == user.id, ra.c.reel_id == x.id)).all()
    out.update({"caption": x.caption, "audio": x.audio or {}, "coauthors": list(x.coauthors or []),
                "is_pinned": bool(x.is_pinned), "is_paid_partnership": bool(x.is_paid_partnership),
                "baseline": x.baseline, "baseline_n": x.baseline_n, "rel_comment_ratio": x.rel_comment_ratio,
                "snapshots": [{"seen_at": db.iso(s.seen_at), "views": s.views, "likes": s.likes,
                               "comments": s.comments, "shares": s.shares} for s in snaps],
                "media": media, "signals": _signals_view(sig), "quick": t.get("quick"), "deep": t.get("deep_data"),
                "overrides": t.get("overrides") or {},
                "effective": {k: t.get(k) for k in ("visual_format", "topics", "sound_primary", "hook_type",
                                                    "cta_type", "headline_box", "caption_style", "face_bubble",
                                                    "broll_share", "setting", "framing")} if t else {},
                "accounts": [{"id": a_id, "handle": accs[a_id].handle if a_id in accs else None, "role": role}
                             for a_id, role in links],
                "saves_line": SAVES_LINE})
    return out


@router.get("/api/reels/{reel_id}")
def get_reel(reel_id: str, user=Depends(security.current_user)):
    with db.read() as conn:
        x = _get_reel(conn, user.id, reel_id)
        return {"reel": reel_detail(conn, user, x)}


def _reel_job(conn, user_id: str, reel_id: str):
    j = db.jobs
    rows = conn.execute(db.scoped(conn, j, user_id).where(j.c.state.in_(db.ACTIVE_JOB_STATES), j.c.kind.in_(
        ("analyze_reel", "process_reel", "fetch_video", "ig_videos"))).order_by(j.c.created_at.desc())).all()
    for row in rows:
        p = row.payload or {}
        if p.get("reel_id") == reel_id or any(it.get("reel_id") == reel_id for it in p.get("items") or []):
            return row
    return None


def _queue_deep(user_id: str, reel_ids: list[str]) -> list[str]:
    try:
        from . import work

        return list(work.queue_deep(user_id, reel_ids) or [])
    except NotImplementedError:
        with db.tx() as conn:
            conn.execute(db.reels.update().where(db.reels.c.user_id == user_id, db.reels.c.id.in_(reel_ids),
                                                 db.reels.c.deep_state.in_(("none", "failed")))
                         .values(deep_state="queued"))
        return []


@router.post("/api/reels/analyze-best")
def analyze_best(body: Any = Body(None), user=Depends(security.current_user)):
    b = _body(body)
    limit = _int(b.get("limit"), 10, 1, 20)
    r, a = db.reels, db.accounts
    with db.read() as conn:
        comp = [x.id for x in conn.execute(db.scoped(conn, a, user.id).where(a.c.role == "competitor")).all()]
        acc_id = b.get("account_id")
        if acc_id:
            if db.get_owned(conn, a, user.id, acc_id) is None:
                raise not_found("We couldn't find that account.")
            comp = [acc_id]
        rows = conn.execute(select(r.c.id).where(r.c.user_id == user.id, r.c.account_id.in_(comp or ["-"]),
                                                 r.c.band.in_(ol.OUTLIER_BANDS), r.c.is_collab.is_(False),
                                                 r.c.deep_state.in_(("none", "failed")))
                            .order_by(r.c.score.desc()).limit(limit)).all()
    ids = [x[0] for x in rows]
    job_ids = _queue_deep(user.id, ids) if ids else []
    with db.read() as conn:
        jrows = [jobs.get(j, user.id) for j in job_ids]
        if not job_ids:
            jrows = [_reel_job(conn, user.id, rid) for rid in ids]
    return {"jobs": [jobs.to_api(x) for x in jrows if x is not None], "count": len(ids), "reel_ids": ids}


@router.post("/api/reels/{reel_id}/analyze")
def analyze_reel(reel_id: str, body: Any = Body(None), user=Depends(security.current_user)):
    with db.read() as conn:
        x = _get_reel(conn, user.id, reel_id)
    job_ids = _queue_deep(user.id, [x.id])
    with db.read() as conn:
        row = jobs.get(job_ids[0], user.id) if job_ids else _reel_job(conn, user.id, x.id)
        st_ = conn.execute(select(db.reels.c.deep_state).where(db.reels.c.id == x.id)).scalar()
    return {"job": jobs.to_api(row) if row is not None else None, "deep_state": st_}


def _clean_overrides(conn, user_id: str, ov: Any) -> dict:
    if not isinstance(ov, dict):
        raise bad_input("overrides must be an object.")
    out: dict[str, Any] = {}
    for k, v in ov.items():
        if k in OVERRIDE_CHOICES:
            if v is not None and v not in OVERRIDE_CHOICES[k]:
                raise bad_input(f"That is not a valid choice for {k.replace('_', ' ')}.")
            out[k] = v
        elif k in OVERRIDE_BOOLS:
            if v is not None and not isinstance(v, bool):
                raise bad_input(f"{k.replace('_', ' ')} must be true or false.")
            out[k] = v
        elif k == "topics":
            if v is None:
                out[k] = None
                continue
            if not isinstance(v, list) or len(v) > 3 or not all(isinstance(s, str) for s in v):
                raise bad_input("Pick up to 3 topics.")
            t = db.topics
            active = {x[0] for x in conn.execute(select(t.c.slug).where(t.c.user_id == user_id,
                                                                         t.c.status == "active")).all()}
            bad = [s for s in v if s not in active]
            if bad:
                raise bad_input(f"These topics are not on your list: {', '.join(bad)}.")
            out[k] = list(dict.fromkeys(v))
        else:
            raise bad_input(f"You can't change {k.replace('_', ' ')} here.")
    return out


@router.patch("/api/reels/{reel_id}")
def patch_reel(reel_id: str, body: Any = Body(None), user=Depends(security.current_user)):
    a, r = db.analyses, db.reels
    advance = False
    changed_tags = False
    with db.tx() as conn:
        x = _get_reel(conn, user.id, reel_id)
        b = _body(body)
        vals: dict = {}
        if "starred" in b:
            if not isinstance(b["starred"], bool):
                raise bad_input("starred must be true or false.")
            vals["starred"] = b["starred"]
            if b["starred"] and not x.starred:
                advance = True
                if x.is_collab and x.tier in ("rest", "none") and x.score is not None:
                    vals.update(tier="priority", sample_weight=1.0)
        if "overrides" in b and b["overrides"] is not None:
            ov = _clean_overrides(conn, user.id, b["overrides"])
            if ov:
                quick = conn.execute(select(a).where(a.c.user_id == user.id, a.c.reel_id == x.id,
                                                     a.c.level == "quick")).first()
                cur = dict((quick.overrides if quick is not None else None) or {})
                for k, v in ov.items():
                    if v is None:
                        cur.pop(k, None)
                    else:
                        cur[k] = v
                now = db.utcnow()
                if quick is None:
                    conn.execute(a.insert().values(id=db.new_id("ana"), reel_id=x.id, user_id=user.id, level="quick",
                                                   data={}, overrides=cur, made_by={"via": "member"},
                                                   created_at=now, updated_at=now))
                else:
                    conn.execute(a.update().where(a.c.id == quick.id).values(overrides=cur, updated_at=now))
                if any(k in cur for k in js.VISUAL_KEYS):
                    vals["tag_quality"] = "member"
                changed_tags = True
        if vals:
            conn.execute(r.update().where(r.c.id == x.id, r.c.user_id == user.id).values(**vals))
        if changed_tags or "starred" in vals:
            js.queue_insights(user.id, conn=conn)
    if advance:
        js._advance(user.id, reel_id)  # noqa: SLF001 - the one chaining point (WP5), 6.0
    with db.read() as conn:
        x = _get_reel(conn, user.id, reel_id)
        accs = _accounts(conn, user.id)
        tags = js.effective_tags(conn, user.id, [x.id], data=False)
        return {"reel": reel_summary(user.id, x, accs, tags, _format_names(conn, user.id))}


# ============================================================================================== insights

def _empty_insights(dim: str | None = None) -> dict:
    return {"snapshot_id": None, "computed_at": None, "rows": [], "grid": {"working_common": [], "opportunity": [],
                                                                          "overused": []},
            "combos": [], "data_note": {"saves_line": SAVES_LINE},
            "message": "No insights yet. Pull reels from your competitors and tag them first."}


def _fresh_cards(conn, user_id: str, data: dict) -> tuple[list, list]:
    """Cards and FormatCards with can-make and format names as they are NOW (profile and names change)."""
    caps = js.capabilities(conn, user_id)
    fmts = _format_names(conn, user_id)
    cards = []
    for c in data.get("cards") or []:
        c = dict(c)
        if c.get("dimension") == "visual_format":
            c["can_make"] = ins.can_make(c.get("value"), caps)
        cards.append(c)
    cards.sort(key=lambda c: (not (c.get("can_make") or {}).get("ok", True), -(c.get("lift") or 0)))
    fcs = []
    for f in data.get("formats") or []:
        meta = fmts.get(f.get("id"))
        if meta is None or not meta.get("active", True):
            continue
        f = dict(f)
        f["name"] = meta["name"]
        f["named"] = meta["named"]
        f["can_make"] = ins.can_make(f.get("vf"), caps)
        fcs.append(f)
    fcs.sort(key=lambda f: (not f["can_make"]["ok"], f.get("lift") is None, -(f.get("lift") or 0)))
    return cards, fcs


@router.get("/api/insights")
def get_insights(dimension: str = "visual_format", window: str | None = None, user=Depends(security.current_user)):
    if dimension not in ins.DIMENSIONS:
        raise bad_input("Unknown dimension.", detail={"allowed": list(ins.DIMENSIONS)})
    if window not in (None, "", "all", "recent"):
        raise bad_input("window must be all or recent.")
    _maybe_refresh(user.id)
    with db.read() as conn:
        snap = js.latest_snapshot(conn, user.id)
    if snap is None:
        out = _empty_insights(dimension)
        out.update(dimension=dimension, window=window or "default")
        return out
    data = snap.data or {}
    part = (data.get("windows") or {}).get(window) if window else data
    part = part or {}
    return {"snapshot_id": snap.id, "computed_at": db.iso(snap.computed_at), "dimension": dimension,
            "window": window or "default", "window_days": part.get("window_days"),
            "rows": (part.get("rows") or {}).get(dimension, []),
            "grid": (part.get("grid") or {}).get(dimension, {"working_common": [], "opportunity": [],
                                                            "overused": []}),
            "combos": part.get("combos") or [], "H0": part.get("H0"), "hit_rule": part.get("hit_rule"),
            "N": part.get("N"), "data_note": data.get("data_note") or {"saves_line": SAVES_LINE}}


def _maybe_refresh(user_id: str) -> None:
    try:
        js.maybe_refresh(user_id)
    except Exception:  # noqa: BLE001
        log.exception("maybe_refresh failed")


def insights_summary(user) -> dict:
    _maybe_refresh(user.id)
    with db.read() as conn:
        snap = js.latest_snapshot(conn, user.id)
        if snap is None:
            out = _empty_insights()
            out.update(cards=[], formats=[], per_competitor=[], you_vs_them=None)
            out.pop("rows", None)
            out.pop("grid", None)
            out.pop("combos", None)
            return out
        data = snap.data or {}
        cards, fcs = _fresh_cards(conn, user.id, data)
        pipe = pipeline_state(conn, user)
    return {"snapshot_id": snap.id, "computed_at": db.iso(snap.computed_at), "cards": cards[:6], "formats": fcs[:3],
            "per_competitor": data.get("per_competitor") or [], "you_vs_them": data.get("you_vs_them"),
            "data_note": data.get("data_note") or {"saves_line": SAVES_LINE}, "pipeline": pipe}


@router.get("/api/insights/summary")
def get_insights_summary(user=Depends(security.current_user)):
    return insights_summary(user)


# ============================================================================================== formats (6.9)

def _get_format(conn, user_id: str, format_id: str):
    row = db.get_owned(conn, db.formats, user_id, format_id)
    if row is None:
        raise not_found("We couldn't find that format.")
    return row


@router.get("/api/formats")
def list_formats(can_make: int = 0, user=Depends(security.current_user)):
    with db.read() as conn:
        snap = js.latest_snapshot(conn, user.id)
        _, fcs = _fresh_cards(conn, user.id, (snap.data if snap else None) or {})
    if can_make:
        fcs = [f for f in fcs if f["can_make"]["ok"]]
    return {"formats": fcs}


def _key_frames(user_id: str, reel_id: str, frames: list, n: int = 4) -> list[dict]:
    fr = [f for f in frames or [] if isinstance(f, dict) and f.get("path")]
    order = {"hook": 0, "cut": 1, "body": 2, "end": 3}
    hook = [f for f in fr if f.get("kind") == "hook"]
    pick = (hook[1:2] or hook[:1]) + [f for f in fr if f.get("kind") == "cut"][:2] + \
        [f for f in fr if f.get("kind") == "end"][:1]
    if len(pick) < n:
        pick += [f for f in sorted(fr, key=lambda f: (order.get(f.get("kind"), 9), f.get("t") or 0))
                 if f not in pick][:n - len(pick)]
    pick = sorted(pick[:n], key=lambda f: f.get("t") or 0)
    return [{"url": _files(user_id, f"reels/{reel_id}/{f['path']}"), "t": f.get("t"), "kind": f.get("kind")}
            for f in pick]


@router.get("/api/formats/{format_id}")
def get_format(format_id: str, user=Depends(security.current_user)):
    a = db.analyses
    with db.read() as conn:
        f = _get_format(conn, user.id, format_id)
        snap = js.latest_snapshot(conn, user.id)
        _, fcs = _fresh_cards(conn, user.id, (snap.data if snap else None) or {})
        card = next((c for c in fcs if c["id"] == f.id), None)
        caps = js.capabilities(conn, user.id)
        if card is None:
            stt = f.stats or {}
            card = {"id": f.id, "name": f.name or ins.fallback_name(f.vf, f.recipe or {}), "named": bool(f.name),
                    "vf": f.vf, "vf_label": ins.vf_label(f.vf), "recipe": f.recipe or {}, "line": None,
                    "lift": stt.get("lift"), "typical": stt.get("typical"), "evidence": stt.get("evidence"),
                    "trend": stt.get("trend"), "can_make": ins.can_make(f.vf, caps), "n": stt.get("n"),
                    "k": stt.get("k"), "examples": [], "href": f"#format/{f.id}"}
        member_ids = [x[0] for x in conn.execute(select(a.c.reel_id).where(a.c.user_id == user.id,
                                                                           a.c.format_id == f.id)).all()]
        ex_ids = [x for x in (f.example_reels or []) if isinstance(x, str)][:6]
        r = db.reels
        want = list(dict.fromkeys(ex_ids + member_ids))
        rows = {x.id: x for x in conn.execute(select(*_reel_cols(), r.c.audio).where(
            r.c.user_id == user.id, r.c.id.in_(want or ["-"]))).all()}
        accs = _accounts(conn, user.id)
        tags = js.effective_tags(conn, user.id, ex_ids, data=False)
        fmts = _format_names(conn, user.id)
        rs = db.reel_signals
        sig = {x.reel_id: x.frames for x in conn.execute(select(rs.c.reel_id, rs.c.frames).where(
            rs.c.user_id == user.id, rs.c.reel_id.in_(ex_ids or ["-"]))).all()}
        examples = []
        for rid in ex_ids:
            x = rows.get(rid)
            if x is None:
                continue
            s = reel_summary(user.id, x, accs, tags, fmts)
            s["frames"] = _key_frames(user.id, rid, sig.get(rid) or [])
            examples.append(s)
        music = ins.music_rows([{"id": x.id, "url": x.url, "audio": x.audio, "score": x.score,
                                 "is_hit": (x.score or 0) >= ol.HIT} for x in rows.values()])
    card = dict(card)
    card.update({"guide": f.guide, "examples": examples, "can_make": ins.can_make(f.vf, caps), "music": music,
                 "needs": list(f.needs or ins.vf_needs(f.vf)), "recipe_key": f.recipe_key,
                 "stats": f.stats or {}, "computed_at": db.iso(f.computed_at), "named_at": db.iso(f.named_at),
                 "actions": ["use_format", "write_3"]})
    return {"format": card}


# ============================================================================================== banks

_SENT = re.compile(r"(.+?[.!?])(\s|$)")
_KEYWORD = re.compile(r"(?i:comment|dm|type)\s+[\"“'‘]?([A-Z0-9]{2,20})\b")


def _first_sentence(text: str | None) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    m = _SENT.match(t)
    return (m.group(1) if m else t)[:200].strip()


def _bank_rows(conn, user_id: str, *, min_score: float):
    r, a = db.reels, db.accounts
    accs = {x.id: x for x in conn.execute(db.scoped(conn, a, user_id)).all()}
    rows = conn.execute(select(r.c.id, r.c.account_id, r.c.url, r.c.score, r.c.caption, r.c.is_collab)
                        .where(r.c.user_id == user_id, r.c.score >= min_score)
                        .order_by(r.c.score.desc())).all()
    return accs, rows


@router.get("/api/banks/hooks")
def bank_hooks(format: str | None = None, topic: str | None = None, min_score: float = 2.0,
               level: str | None = None, user=Depends(security.current_user)):
    if level not in (None, "", "quick", "deep"):
        raise bad_input("level must be quick or deep.")
    with db.read() as conn:
        accs, rows = _bank_rows(conn, user.id, min_score=min_score)
        ids = [x.id for x in rows]
        tags = js.effective_tags(conn, user.id, ids)
        rs = db.reel_signals
        tr = {x.reel_id: x.transcript for x in conn.execute(select(rs.c.reel_id, rs.c.transcript).where(
            rs.c.user_id == user.id, rs.c.reel_id.in_(ids or ["-"]))).all()}
    out = []
    for x in rows:
        acc = accs.get(x.account_id)
        if acc is None or acc.role != "competitor":
            continue
        t = tags.get(x.id) or {}
        if format and t.get("visual_format") != format:
            continue
        if topic and topic not in (t.get("topics") or []):
            continue
        deep = t.get("deep_data") or {}
        hook = ((deep.get("skeleton") or {}).get("hook") or {}) if deep else {}
        if hook:
            row = {"reel_id": x.id, "handle": acc.handle, "url": x.url, "score": round(x.score, 2),
                   "spoken": hook.get("spoken") or "", "on_screen_text": hook.get("on_screen_text") or "",
                   "visual": hook.get("visual") or "", "hook_type": hook.get("hook_type") or t.get("hook_type"),
                   "formula": deep.get("hook_formula") or None, "why": hook.get("why_it_works") or None,
                   "level": "deep"}
        else:
            quick = t.get("quick") or {}
            spoken = _first_sentence(((tr.get(x.id) or {}) or {}).get("text"))
            hl = quick.get("hook_line") or ""
            if not (spoken or hl or t.get("hook_type")):
                continue
            row = {"reel_id": x.id, "handle": acc.handle, "url": x.url, "score": round(x.score, 2),
                   "spoken": spoken or hl, "on_screen_text": hl if hl and hl != spoken else "",
                   "visual": quick.get("visual_subject") or "", "hook_type": t.get("hook_type"), "formula": None,
                   "why": None, "level": "quick"}
        if level and row["level"] != level:
            continue
        row["actions"] = ["borrow_hook", "remake"]
        out.append(row)
    return {"hooks": out[:200]}


@router.get("/api/banks/ctas")
def bank_ctas(user=Depends(security.current_user)):
    with db.read() as conn:
        accs, rows = _bank_rows(conn, user.id, min_score=0.0)
        ids = [x.id for x in rows]
        tags = js.effective_tags(conn, user.id, ids)
    out = []
    for x in rows:
        acc = accs.get(x.account_id)
        if acc is None or acc.role != "competitor":
            continue
        t = tags.get(x.id) or {}
        cta = (((t.get("deep_data") or {}).get("skeleton") or {}).get("cta") or {})
        ctype = cta.get("cta_type") or t.get("cta_type")
        if not ctype or ctype == "none":
            continue
        m = _KEYWORD.search(x.caption or "")
        text = cta.get("text") or ""
        if not text and m:
            line = next((ln for ln in (x.caption or "").splitlines() if m.group(0) in ln), m.group(0))
            text = line.strip()[:200]
        out.append({"reel_id": x.id, "handle": acc.handle, "url": x.url, "score": round(x.score, 2),
                    "text": text, "cta_type": ctype, "cta_label": ins.CTA_LABELS.get(ctype, ctype),
                    "keyword": cta.get("keyword") or (m.group(1) if m else ""),
                    "placement": cta.get("placement") or ("caption" if m else "")})
    return {"ctas": out[:200]}


def _snapshot_data(conn, user_id: str) -> dict:
    snap = js.latest_snapshot(conn, user_id)
    return (snap.data if snap else None) or {}


@router.get("/api/banks/sounds")
def bank_sounds(user=Depends(security.current_user)):
    with db.read() as conn:
        data = _snapshot_data(conn, user.id)
        rows = (data.get("rows") or {}).get("sound", [])
        want = [rid for row in rows for rid in row.get("best_reels", [])[:2]]
        rs = db.reel_signals
        sound = {x.reel_id: x.sound for x in conn.execute(select(rs.c.reel_id, rs.c.sound).where(
            rs.c.user_id == user.id, rs.c.reel_id.in_(want or ["-"]))).all()}
    out = []
    for row in rows:
        cues = []
        for rid in row.get("best_reels", [])[:2]:
            for ev in ((sound.get(rid) or {}).get("events") or [])[:4]:
                if isinstance(ev, dict):
                    cues.append({"reel_id": rid, "t": ev.get("t"), "label": ev.get("label"),
                                 "confidence": ev.get("confidence"), "on_cut": ev.get("on_cut")})
        out.append({"id": row["value"], "label": row.get("label") or ins.sd_label(row["value"]), "reels": row["n"],
                    "lift": row.get("lift"), "evidence": row.get("evidence"), "trend": row.get("trend"),
                    "example_cues": cues[:6], "best_reels": row.get("best_reels", [])})
    return {"patterns": out}


@router.get("/api/banks/formats")
def bank_formats(user=Depends(security.current_user)):
    with db.read() as conn:
        data = _snapshot_data(conn, user.id)
        fmts = _format_names(conn, user.id)
    sub = {}
    for fid, meta in fmts.items():
        if meta.get("active", True) and meta.get("vf"):
            sub.setdefault(meta["vf"], []).append(fid)
    sub_rows = {r["value"]: r for r in (data.get("rows") or {}).get("sub_format", [])}
    out = []
    for row in (data.get("rows") or {}).get("visual_format", []):
        ids = sorted(sub.get(row["value"], []), key=lambda i: -((sub_rows.get(i) or {}).get("lift") or 0))
        out.append({"id": row["value"], "label": row.get("label") or ins.vf_label(row["value"]), "reels": row["n"],
                    "lift": row.get("lift"), "typical": row.get("typical"), "evidence": row.get("evidence"),
                    "trend": row.get("trend"), "best_reels": row.get("best_reels", [])[:3],
                    "recipe": fmts[ids[0]]["name"] if ids else None, "sub_formats": ids,
                    "can_make": row.get("can_make")})
    return {"formats": out}


def music_for(conn, user_id: str) -> list[dict]:
    r, a = db.reels, db.accounts
    comp = [x[0] for x in conn.execute(select(a.c.id).where(a.c.user_id == user_id, a.c.role == "competitor")).all()]
    rows = conn.execute(select(r.c.id, r.c.url, r.c.audio, r.c.score).where(
        r.c.user_id == user_id, r.c.account_id.in_(comp or ["-"]), r.c.tier == "priority",
        r.c.is_collab.is_(False))).all()
    return ins.music_rows([{"id": x.id, "url": x.url, "audio": x.audio, "score": x.score,
                            "is_hit": (x.score or 0) >= ol.HIT} for x in rows])


@router.get("/api/banks/music")
def bank_music(user=Depends(security.current_user)):
    with db.read() as conn:
        return {"tracks": music_for(conn, user.id)}


# ============================================================================================== Home (4.11)

def home_parts(user_id: str) -> dict:
    """Merged into GET /api/home by WP1: new outliers, first look per recent pull, cards, format cards, pipeline."""
    try:
        js.maybe_rescore(user_id)
    except Exception:  # noqa: BLE001
        log.exception("maybe_rescore failed")
    _maybe_refresh(user_id)
    r, p = db.reels, db.pulls
    with db.read() as conn:
        user = conn.execute(select(db.users).where(db.users.c.id == user_id)).first()
        accs = _accounts(conn, user_id)
        comp = [k for k, v in accs.items() if v.role == "competitor"]
        tags = {}
        fmts = _format_names(conn, user_id)
        newest = conn.execute(select(*_reel_cols()).where(
            r.c.user_id == user_id, r.c.account_id.in_(comp or ["-"]), r.c.band.in_(ol.OUTLIER_BANDS))
            .order_by(r.c.first_seen_at.desc(), r.c.score.desc()).limit(8)).all()
        pulls = conn.execute(db.scoped(conn, p, user_id).where(p.c.finished_at.is_not(None), p.c.reels_new > 0)
                             .order_by(p.c.finished_at.desc()).limit(3)).all()
        looks = []
        for pl in pulls:
            acc = accs.get(pl.account_id)
            if acc is None:
                continue
            since = (pl.started_at or pl.finished_at) - timedelta(minutes=5)
            outs = conn.execute(select(*_reel_cols()).where(
                r.c.user_id == user_id, r.c.account_id == acc.id, r.c.band.in_(ol.OUTLIER_BANDS),
                r.c.first_seen_at >= since).order_by(r.c.score.desc())).all()
            looks.append((pl, acc, outs))
        ids = [x.id for x in newest] + [x.id for _, _, outs in looks for x in outs[:8]]
        tags = js.effective_tags(conn, user_id, ids, data=False)
        new_out = [reel_summary(user_id, x, accs, tags, fmts) for x in newest]
        first = []
        for pl, acc, outs in looks:
            n = len(outs)
            line = (f"Your first look: {n} outlier{'s' if n != 1 else ''} from @{acc.handle}" if pl.mode == "first"
                    else f"{n} outlier{'s' if n != 1 else ''} in the latest reels from @{acc.handle}")
            first.append({"account": {"id": acc.id, "handle": acc.handle}, "pull_id": pl.id, "mode": pl.mode,
                          "finished_at": db.iso(pl.finished_at), "outliers": n, "line": line,
                          "tags_coming": any(x.tag_state != "done" for x in outs),
                          "reels": [reel_summary(user_id, x, accs, tags, fmts) for x in outs[:8]]})
        data = _snapshot_data(conn, user_id)
        cards, fcs = _fresh_cards(conn, user_id, data)
        pipe = pipeline_state(conn, user) if user is not None else None
    return {"new_outliers": new_out, "first_look": first, "cards": cards[:3], "formats": fcs[:3], "pipeline": pipe,
            "saves_line": SAVES_LINE}


# ============================================================================================== admin hooks (11.5)

def _support_reels(user_id: str, query: dict) -> dict:
    with db.read() as conn:
        user = conn.execute(select(db.users).where(db.users.c.id == user_id)).first()
    if user is None:
        raise not_found()
    return list_reels(user, query)


def _support_insights(user_id: str, query: dict) -> dict:
    with db.read() as conn:
        user = conn.execute(select(db.users).where(db.users.c.id == user_id)).first()
    if user is None:
        raise not_found()
    return insights_summary(user)


def community_formats() -> dict:
    """Anonymous 'top formats this month' (11.5): per VF, how many members' latest snapshots this month have it
    (n >= 3) and the median lift. api_admin drops rows with < 5 members and keeps only the anonymous fields."""
    s = db.insight_snapshots
    now = db.utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with db.read() as conn:
        latest = conn.execute(select(s.c.user_id, func.max(s.c.computed_at)).where(s.c.computed_at >= start)
                              .group_by(s.c.user_id)).all()
        per_vf: dict[str, list[float]] = {}
        for uid, at in latest:
            row = conn.execute(select(s.c.data).where(s.c.user_id == uid, s.c.computed_at == at)).first()
            for lr in ((row[0] or {}).get("rows") or {}).get("visual_format", []) if row else []:
                if lr.get("n", 0) >= 3 and lr.get("lift") is not None:
                    per_vf.setdefault(lr["value"], []).append(float(lr["lift"]))
    formats = [{"vf": vf, "label": ins.vf_label(vf), "members_using": len(v), "lift_median": round(st.median(v), 2)}
               for vf, v in per_vf.items()]
    formats.sort(key=lambda f: (-f["members_using"], -f["lift_median"], f["vf"]))
    return {"month": now.strftime("%Y-%m"), "formats": formats}


def _register_admin_hooks() -> None:
    try:
        from .api_admin import register_community_formats, register_support_mirror

        register_support_mirror("reels", _support_reels)
        register_support_mirror("insights", _support_insights)
        register_community_formats(community_formats)
    except Exception:  # noqa: BLE001
        log.exception("could not register admin hooks")


_register_admin_hooks()
