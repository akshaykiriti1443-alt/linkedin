"""/api/accounts/*, /api/discover/* (DESIGN-SAAS.md 4.8). WP4 owns this file.
GET /api/jobs/<id> lives in api_files.py (WP1).

Ownership is always checked before the request body is read, so another member's ids get a 404 whatever they send.
"""
from __future__ import annotations

import logging
import shutil
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from .. import outliers as ol
from . import db, jobs, ratelimit, security, storage
from . import jobs_score as js
from .errors import ApiError, bad_input, not_found

log = logging.getLogger("reelvault.accounts")
router = APIRouter()

SAVES_LINE = "Instagram hides saves, and usually shares, for accounts you don't own. We rank on views, likes and comments."


# ============================================================================================== helpers

def _body(b: Any) -> dict:
    if b is None:
        return {}
    if not isinstance(b, dict):
        raise bad_input("Send a JSON object.")
    return b


def _today() -> date:
    return db.utcnow().date()


def _pull_days(conn, user_id: str) -> Counter:
    """Pulls per UTC day (today and later): a pull counts on the day it runs (run_after) or was asked for.
    Pulls canceled before they started don't count."""
    p, j = db.pulls, db.jobs
    rows = conn.execute(select(p.c.run_after, p.c.started_at, j.c.created_at, j.c.state)
                        .select_from(p.outerjoin(j, j.c.id == p.c.job_id)).where(p.c.user_id == user_id)).all()
    today = _today()
    c: Counter = Counter()
    for run_after, started, created, state in rows:
        if state == "canceled" and started is None:
            continue
        when = run_after or started or created
        if when is None:
            continue
        d = when.date() if isinstance(when, datetime) else when
        if d >= today:
            c[d] += 1
    return c


def _slots(counter: Counter, n: int, per_day: int) -> list[datetime | None]:
    """run_after for the next n pulls: None while today has room, else the start of the next day with room."""
    out = []
    d = _today()
    c = Counter(counter)
    per_day = max(1, int(per_day))
    for _ in range(n):
        while c[d] >= per_day:
            d += timedelta(days=1)
        c[d] += 1
        out.append(None if d == _today() else datetime.combine(d, time(0, 5), tzinfo=timezone.utc))
    return out


def _schedule_note(slots: list[datetime | None]) -> str | None:
    if not any(s is not None for s in slots):
        return None
    days = Counter((s.date() if s else _today()) for s in slots)
    parts = []
    for i, d in enumerate(sorted(days)):
        n = days[d]
        delta = (d - _today()).days
        when = "today" if delta == 0 else ("tomorrow" if delta == 1 else f"on {d.strftime('%A')}")
        parts.append(f"{n} {when}" if i else f"{n} account{'s' if n != 1 else ''} {when}")
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"We'll read {text} to keep your Instagram safe."


def _active_pulls(conn, user_id: str) -> dict[str, Any]:
    j = db.jobs
    out = {}
    for row in conn.execute(db.scoped(conn, j, user_id).where(j.c.kind == "ig_pull",
                                                             j.c.state.in_(db.ACTIVE_JOB_STATES))).all():
        acc = (row.payload or {}).get("account_id")
        if acc:
            out[acc] = row
    return out


def _outlier_counts(conn, user_id: str) -> dict[str, int]:
    r = db.reels
    rows = conn.execute(select(r.c.account_id, func.count()).where(
        r.c.user_id == user_id, r.c.band.in_(ol.OUTLIER_BANDS)).group_by(r.c.account_id)).all()
    return {a: int(n) for a, n in rows}


def extension_state(conn, user_id: str) -> dict:
    """{"paired": bool, "online": bool} of the member's Chrome extension, so the Competitors page says "Waiting
    for Chrome" instead of "Reading now" when nothing can read (fix round 1)."""
    from .api_ext import ONLINE_S

    d = db.devices
    dev = conn.execute(select(d.c.last_seen_at).where(d.c.user_id == user_id, d.c.kind == "extension",
                                                      d.c.revoked_at.is_(None))
                       .order_by(d.c.created_at.desc())).first()
    if dev is None:
        return {"paired": False, "online": False}
    seen = dev.last_seen_at
    if seen is not None and seen.tzinfo is None:
        from datetime import timezone

        seen = seen.replace(tzinfo=timezone.utc)
    return {"paired": True, "online": bool(seen and (db.utcnow() - seen).total_seconds() < ONLINE_S)}


def account_summary(acc, *, outliers: int = 0, pull_job=None) -> dict:
    out = {"id": acc.id, "handle": acc.handle, "full_name": acc.full_name, "followers": acc.followers,
           "role": acc.role, "status": acc.status, "reels_count": acc.reels_count or 0, "outliers": outliers,
           "baseline_views": acc.baseline_views, "cursor_done": bool(acc.cursor_done),
           "last_pull_at": db.iso(acc.last_pull_at), "last_refresh_at": db.iso(acc.last_refresh_at),
           "pulling": pull_job is not None, "profile_pic": None, "note": acc.note,
           "url": f"https://www.instagram.com/{acc.handle}/"}
    if pull_job is not None:
        out["pull_job"] = jobs.to_api(pull_job, with_eta=False)
        if pull_job.run_after is not None and pull_job.run_after > db.utcnow():
            out["scheduled_for"] = db.iso(pull_job.run_after)
    return out


def _summaries(conn, user_id: str, accs: list) -> list[dict]:
    oc = _outlier_counts(conn, user_id)
    ap = _active_pulls(conn, user_id)
    return [account_summary(a, outliers=oc.get(a.id, 0), pull_job=ap.get(a.id)) for a in accs]


def _limits(conn, user_id: str) -> dict:
    s = db.all_settings(conn=conn)
    return {"max": int(s.get("max_accounts_per_member") or 10), "pulls_today": _pull_days(conn, user_id)[_today()],
            "pulls_max": int(s.get("max_pulls_per_day") or 5)}


# ============================================================================================== add accounts

def add_accounts(user, handles: list, *, pull: bool = True, role: str = "competitor",
                 known: dict | None = None) -> dict:
    """POST /api/accounts core (also used by discover/track). Returns {"accounts", "jobs", "schedule_note"?}.
    known: {handle: {"ig_pk", "full_name", "followers"}} from a discovery check, written on new accounts."""
    from . import jobs_ig

    if role not in ("competitor", "self"):
        raise bad_input("role must be competitor or self.")
    if not isinstance(handles, list) or not handles:
        raise bad_input("Add at least one Instagram account (@name or a profile link).")
    if len(handles) > 20:
        raise bad_input("Add up to 20 accounts at a time.")
    parsed, bad = [], []
    for h in handles:
        p = js.parse_handle(h) if isinstance(h, str) else None
        if p is None:
            bad.append(str(h)[:60])
        elif p not in parsed:
            parsed.append(p)
    if bad:
        raise bad_input(f"These don't look like Instagram accounts: {', '.join(bad)}. Use @name or a profile link.",
                        detail={"bad": bad})
    if role == "self" and len(parsed) != 1:
        raise bad_input("Give just one account as your own.")
    a = db.accounts
    created, touched = [], []
    with db.tx() as conn:
        existing = {x.handle: x for x in conn.execute(db.scoped(conn, a, user.id)).all()}
        cap = int(db.get_setting("max_accounts_per_member", conn=conn) or 10)
        n_comp = sum(1 for x in existing.values() if x.role == "competitor")
        if role == "self":
            cur_self = next((x for x in existing.values() if x.role == "self"), None)
            if cur_self is not None and cur_self.handle != parsed[0]:
                raise ApiError(409, "busy", f"Your account is already set to @{cur_self.handle}. Remove it first.")
        new_handles = [h for h in parsed if h not in existing]
        if role == "competitor" and n_comp + len(new_handles) > cap:
            left = max(0, cap - n_comp)
            raise ApiError(429, "limit_reached",
                           f"You can track up to {cap} competitors. You have room for {left} more. "
                           "Remove one to add another.")
        for h in parsed:
            row = existing.get(h)
            if row is None:
                aid = db.new_id("acc")
                extra = {k: v for k, v in ((known or {}).get(h) or {}).items()
                         if k in ("ig_pk", "full_name", "followers") and v is not None}
                conn.execute(a.insert().values(id=aid, user_id=user.id, role=role, platform="instagram", handle=h,
                                               status="new", created_at=db.utcnow(), **extra))
                created.append(aid)
                touched.append(aid)
            else:
                if role == "self" and row.role != "self":
                    conn.execute(a.update().where(a.c.id == row.id).values(role="self"))
                touched.append(row.id)
        per_day = int(db.get_setting("max_pulls_per_day", conn=conn) or 5)
        days = _pull_days(conn, user.id)
        rows = {x.id: x for x in conn.execute(db.scoped(conn, a, user.id).where(a.c.id.in_(touched))).all()}
        active = _active_pulls(conn, user.id)
    job_ids: list[str] = []
    to_pull = []
    if pull:
        to_pull = [aid for aid in touched if aid not in active and rows[aid].status not in ("private", "not_found")
                   and not rows[aid].last_pull_at]
    slots = _slots(days, len(to_pull), per_day)
    for aid, when in zip(to_pull, slots):
        job_ids.append(jobs_ig.start_pull(user.id, aid, "first", run_after=when))
    for aid in touched:
        if aid in active:
            job_ids.append(active[aid].id)
    if not pull:
        for aid in created:
            try:
                job_ids.append(jobs_ig.request_profile(user.id, rows[aid].handle, "competitor", account_id=aid))
            except ApiError:
                pass
    with db.read() as conn:
        accs = conn.execute(db.scoped(conn, a, user.id).where(a.c.id.in_(touched))).all()
        order = {aid: i for i, aid in enumerate(touched)}
        accs = sorted(accs, key=lambda x: order[x.id])
        summaries = _summaries(conn, user.id, accs)
        jrows = [jobs.get(j, user.id) for j in dict.fromkeys(job_ids)]
    out = {"accounts": summaries, "jobs": [jobs.to_api(r) for r in jrows if r is not None]}
    note = _schedule_note(slots)
    if note:
        out["schedule_note"] = note
    return out


# ============================================================================================== routes: accounts

@router.get("/api/accounts")
def list_accounts(user=Depends(security.current_user)):
    try:
        js.maybe_rescore(user.id)
    except Exception:  # noqa: BLE001
        log.exception("maybe_rescore failed")
    a = db.accounts
    with db.read() as conn:
        rows = conn.execute(db.scoped(conn, a, user.id).order_by(a.c.created_at.asc(), a.c.handle.asc())).all()
        summ = _summaries(conn, user.id, rows)
        limits = _limits(conn, user.id)
        ext = extension_state(conn, user.id)
    out = {"accounts": [s for s in summ if s["role"] == "competitor"], "limits": limits, "saves_line": SAVES_LINE,
           "extension": ext}
    me = next((s for s in summ if s["role"] == "self"), None)
    if me:
        out["self"] = me
    return out


@router.post("/api/accounts", status_code=201)
def create_accounts(body: Any = Body(None), user=Depends(security.current_user)):
    b = _body(body)
    return add_accounts(user, b.get("handles"), pull=bool(b.get("pull", True)), role=b.get("role") or "competitor")


def _get_account(conn, user_id: str, account_id: str):
    row = db.get_owned(conn, db.accounts, user_id, account_id)
    if row is None:
        raise not_found("We couldn't find that account.")
    return row


def data_note(conn, user_id: str, account_id: str) -> dict:
    r = db.reels
    rows = conn.execute(select(r.c.taken_at, r.c.band, r.c.is_pinned, r.c.counts_hidden)
                        .where(r.c.user_id == user_id, r.c.account_id == account_id)).all()
    dates = sorted(x.taken_at for x in rows if x.taken_at is not None)
    return {"reels_pulled": len(rows),
            "date_range": [dates[0].date().isoformat(), dates[-1].date().isoformat()] if dates else None,
            "excluded": {"too_new": sum(1 for x in rows if x.band == "growing"),
                         "pinned": sum(1 for x in rows if x.is_pinned),
                         "hidden_counts": sum(1 for x in rows if x.counts_hidden)},
            "saves_line": SAVES_LINE}


def account_detail(conn, user_id: str, acc) -> dict:
    s = _summaries(conn, user_id, [acc])[0]
    p = db.pulls
    pulls = conn.execute(db.scoped(conn, p, user_id).where(p.c.account_id == acc.id)
                         .order_by(p.c.started_at.desc().nulls_first(), p.c.finished_at.desc().nulls_first())
                         .limit(20)).all()
    s["bio"] = acc.bio
    s["category"] = acc.category
    s["pulls"] = [{"id": x.id, "mode": x.mode, "reels_new": x.reels_new, "reels_updated": x.reels_updated,
                   "stop_reason": x.stop_reason, "recovery": x.recovery, "run_after": db.iso(x.run_after),
                   "started_at": db.iso(x.started_at), "finished_at": db.iso(x.finished_at)} for x in pulls]
    r = db.reels
    ns = [x[0] for x in conn.execute(select(r.c.baseline_n).where(r.c.user_id == user_id, r.c.account_id == acc.id,
                                                                  r.c.score.is_not(None))).all() if x[0]]
    n = int(round(js.median_or_none(ns))) if ns else None
    s["normal_line"] = ol.normal_line(acc.baseline_views, n)
    snap = js.latest_snapshot(conn, user_id)
    sig = None
    if snap is not None:
        sig = next((x for x in (snap.data or {}).get("per_competitor", []) if x.get("account_id") == acc.id), None)
    s["signature"] = sig
    s["data_note"] = data_note(conn, user_id, acc.id)
    return s


@router.get("/api/accounts/{account_id}")
def get_account(account_id: str, user=Depends(security.current_user)):
    with db.read() as conn:
        acc = _get_account(conn, user.id, account_id)
        return {"account": account_detail(conn, user.id, acc)}


@router.patch("/api/accounts/{account_id}")
def patch_account(account_id: str, body: Any = Body(None), user=Depends(security.current_user)):
    with db.tx() as conn:
        acc = _get_account(conn, user.id, account_id)
        b = _body(body)
        vals: dict = {}
        if "status" in b and b["status"] is not None:
            if b["status"] not in ("active", "paused"):
                raise bad_input("status must be active or paused.")
            if acc.status in ("private", "not_found"):
                raise bad_input("ReelVault can't read this account. It is private or doesn't exist.")
            if b["status"] == "active" and acc.status == "paused":
                vals["status"] = "active" if acc.last_pull_at else "new"
            elif b["status"] == "paused":
                vals["status"] = "paused"
        if "note" in b:
            note = b["note"]
            if note is not None and not isinstance(note, str):
                raise bad_input("note must be text.")
            vals["note"] = (note or "").strip()[:1000] or None
        if vals:
            conn.execute(db.accounts.update().where(db.accounts.c.id == acc.id).values(**vals))
        acc = _get_account(conn, user.id, account_id)
        return {"account": account_detail(conn, user.id, acc)}


@router.delete("/api/accounts/{account_id}")
def delete_account(account_id: str, body: Any = Body(None), user=Depends(security.current_user)):
    r, ra, j = db.reels, db.reel_accounts, db.jobs
    with db.tx() as conn:
        acc = _get_account(conn, user.id, account_id)
        b = _body(body)
        delete_reels = b.get("delete_reels", True) is not False
        for row in conn.execute(db.scoped(conn, j, user.id).where(j.c.state.in_(db.ACTIVE_JOB_STATES),
                                                                 j.c.kind.in_(("ig_pull", "score_account",
                                                                               "pick_sample", "ig_profile")))).all():
            if (row.payload or {}).get("account_id") == acc.id:
                conn.execute(j.update().where(j.c.id == row.id).values(
                    state="canceled", finished_at=db.utcnow(), dedupe_done=row.dedupe_key, dedupe_key=None,
                    lease_owner=None, lease_until=None))
        removed: list[str] = []
        if delete_reels:
            mine = [x[0] for x in conn.execute(select(r.c.id).where(r.c.user_id == user.id,
                                                                    r.c.account_id == acc.id)).all()]
            others = {x[0] for x in conn.execute(select(ra.c.reel_id).where(
                ra.c.user_id == user.id, ra.c.reel_id.in_(mine or ["-"]), ra.c.account_id != acc.id)).all()}
            removed = [x for x in mine if x not in others]
            for i in range(0, len(removed), 500):
                conn.execute(r.delete().where(r.c.user_id == user.id, r.c.id.in_(removed[i:i + 500])))
            # collab reels kept for another tracked account move to that account
            for rid in others:
                other = conn.execute(select(ra.c.account_id).where(ra.c.reel_id == rid, ra.c.account_id != acc.id)
                                     .order_by((ra.c.role == "owner").desc())).first()
                if other:
                    conn.execute(r.update().where(r.c.id == rid).values(account_id=other[0]))
        conn.execute(db.accounts.delete().where(db.accounts.c.id == acc.id, db.accounts.c.user_id == user.id))
        js.queue_insights(user.id, conn=conn)
    for rid in removed:
        try:
            p = storage.member_path(user.id, f"reels/{rid}")
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except Exception:  # noqa: BLE001
            log.exception("could not remove files of %s", rid)
    if removed:
        try:
            storage.refresh_storage_bytes(user.id)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "reels_deleted": len(removed)}


@router.post("/api/accounts/{account_id}/pull")
def pull_account(account_id: str, body: Any = Body(None), user=Depends(security.current_user)):
    from . import jobs_ig

    with db.read() as conn:
        acc = _get_account(conn, user.id, account_id)
        b = _body(body)
        mode = b.get("mode") or "next"
        if mode not in ("next", "refresh"):
            raise bad_input("mode must be next or refresh.")
        if acc.status == "paused":
            raise bad_input("This account is paused. Turn it back on to pull reels.")
        if acc.status in ("private", "not_found"):
            raise bad_input("ReelVault can't read this account. It is private or doesn't exist.")
        if _active_pulls(conn, user.id).get(acc.id) is not None:
            raise ApiError(409, "busy", "ReelVault is already reading this account. Wait for it to finish.")
        if mode == "next" and acc.cursor_done:
            raise bad_input("You already have all reels from this account.")
        if mode == "next" and not acc.last_pull_at and not acc.cursor_max_id:
            mode = "first"
        per_day = int(db.get_setting("max_pulls_per_day", conn=conn) or 5)
        days = _pull_days(conn, user.id)
    when = _slots(days, 1, per_day)[0]
    job_id = jobs_ig.start_pull(user.id, acc.id, mode, run_after=when)
    out = {"job": jobs.to_api(jobs.get(job_id, user.id))}
    if when is not None:
        out["note"] = f"You've done {per_day} pulls today. This one starts tomorrow." if \
            (when.date() - _today()).days == 1 else f"You've used your pulls for the next days. This one starts " \
                                                    f"on {when.strftime('%A')}."
    return out


# ============================================================================================== routes: discover

@router.post("/api/discover")
def discover(body: Any = Body(None), user=Depends(security.current_user)):
    b = _body(body)
    mode = b.get("mode") or "niche"
    ratelimit.hit("discover", user.id, 10, "day",
                  message="You can search for new accounts 10 times a day. Try again tomorrow.")
    run_id = js.start_discover(user.id, mode, query=b.get("query"), seeds=b.get("seeds"))
    with db.read() as conn:
        rows = conn.execute(db.scoped(conn, db.jobs, user.id).where(db.jobs.c.kind.in_(("ig_discover", "ig_check")),
                                                                   db.jobs.c.state.in_(db.ACTIVE_JOB_STATES))
                            .order_by(db.jobs.c.created_at.desc())).all()
    mine = [x for x in rows if (x.payload or {}).get("run_id") == run_id]
    return {"run_id": run_id, "jobs": [jobs.to_api(x) for x in mine]}


def _get_run(conn, user_id: str, run_id: str):
    run = db.get_owned(conn, db.discover_runs, user_id, run_id)
    if run is None:
        raise not_found("We couldn't find that search.")
    return run


STATE_TEXT = {"queued": "Waiting for your Chrome extension", "searching": "Searching Instagram",
              "found": "Checking the best accounts", "checking": "Checking the best accounts",
              "done": "Done", "failed": "The search stopped. Try again later."}


@router.get("/api/discover/{run_id}")
def get_discover(run_id: str, user=Depends(security.current_user)):
    with db.read() as conn:
        run = _get_run(conn, user.id, run_id)
        rows = js.rank_candidates(conn, user.id, run.id)
    return {"id": run.id, "state": run.state, "state_text": STATE_TEXT.get(run.state, run.state), "mode": run.mode,
            "queries": run.queries or [], "seeds": run.seeds or [],
            "candidates": [js.candidate_json(x) for x in rows],
            "line": "Top 10 we found. Tick the ones to track."}


@router.post("/api/discover/{run_id}/track")
def track_candidates(run_id: str, body: Any = Body(None), user=Depends(security.current_user)):
    dc = db.discover_candidates
    with db.read() as conn:
        run = _get_run(conn, user.id, run_id)
        b = _body(body)
        ids = b.get("candidate_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(x, str) for x in ids):
            raise bad_input("Tick at least one account to track.")
        rows = conn.execute(db.scoped(conn, dc, user.id).where(dc.c.run_id == run.id, dc.c.id.in_(ids))).all()
    if len(rows) != len(set(ids)):
        raise not_found("Some of those accounts are not part of this search.")
    known = {x.handle: {"ig_pk": x.pk, "full_name": x.full_name, "followers": x.followers} for x in rows}
    out = add_accounts(user, [x.handle for x in rows], pull=True, role="competitor", known=known)
    with db.tx() as conn:
        conn.execute(dc.update().where(dc.c.user_id == user.id, dc.c.id.in_([x.id for x in rows]))
                     .values(status="tracked"))
    return JSONResponse(out, status_code=201)
