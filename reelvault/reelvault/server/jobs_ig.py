"""Instagram jobs for the member's Chrome extension: queueing, ingest, cooldowns (DESIGN-SAAS.md 5, 5.8). WP2.

Public API (other packages call these):
    start_pull(user_id, account_id, mode, *, run_after=None, priority=10) -> job_id     # pulls row + ig_pull (WP4)
    request_videos(user_id, reel_ids) -> job_id | None      # batches <= 30 reels into ig_videos jobs (WP3)
    request_profile(user_id, handle, purpose, account_id=None, source_id=None) -> job_id   # ig_profile (WP4, WP7)
    request_discover(user_id, run_id, mode, *, queries=None, seeds=None) -> job_id         # ig_discover (WP4)
    request_check(user_id, run_id, candidates) -> job_id                                   # ig_check (WP4)
    request_page_read(user_id, source_id, kind, url) -> job_id                             # page_read (WP7)
    schedule_need_extension(user_id) -> job_id | None       # reels with media_state=need_extension -> ig_videos
    ig_health() -> dict                                     # admin "Instagram health" (5.9)

Everything the extension uploads is handled here (api_ext.py only does HTTP): ingest_items, save_video, save_page,
save_member_bio, finish, fail, progress, lease. Ids are digit strings; igparse is the only interpreter of fields.
No transaction is held across file writes or calls into other packages.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError

from .. import igparse
from . import db, jobs, ratelimit
from .errors import ApiError, bad_input, not_found
from .settings import EXTENSION_MIN, get_settings

log = logging.getLogger("reelvault.jobs_ig")

# ============================================================================================== contract constants

EXT_KINDS = ("ig_pull", "ig_profile", "ig_videos", "ig_discover", "ig_check", "page_read")
IG_KINDS = ("ig_pull", "ig_profile", "ig_videos", "ig_discover", "ig_check")  # touch Instagram
PULL_TARGET = 100
PAGE_CAP = 12
LEASE_S = 300
IG_MAX_ATTEMPTS = 8

PACING = {"gap_ms": [4000, 9000], "page_gap_ms": [6000, 12000], "cdn_gap_ms": [2000, 5000],
          "retry_ms": [30000, 120000], "hour_max": 120, "day_max": 400, "media_info_max": 30, "cdn_day_max": 150,
          "discover_day_max": 60, "page_read_day_max": 5}
# RV_TEST_PACING=1 (tests only): short gaps. Only an extension built with BUILD="test" accepts them; store and ZIP
# builds always keep the stricter of their own numbers and the server's (5.6).
TEST_PACING = dict(PACING, gap_ms=[50, 100], page_gap_ms=[50, 100], cdn_gap_ms=[50, 100], retry_ms=[200, 400])

HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")
PK_RE = re.compile(r"^\d{1,20}$")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,20}$")
RVID_RE = re.compile(r"^[a-z]{2,5}_[a-z2-7]{16}$")
CURSOR_RE = re.compile(r"^[A-Za-z0-9_=-]{1,200}$")
QUERY_RE = re.compile(r"^[A-Za-z0-9 &\-'.]{1,60}$")
PAGE_URL_RE = re.compile(r"^https://www\.skool\.com/(@[A-Za-z0-9-]{1,60}|[a-z0-9-]{1,80}/about)$")
LINKEDIN_SELF = "https://www.linkedin.com/in/me/"
LINKEDIN_FINAL_RE = re.compile(r"^https://www\.linkedin\.com/in/[^/?#]+/?$")
PAGE_KINDS = ("skool_profile", "skool_group", "linkedin_self")
PULL_STOP_REASONS = ("target_reached", "no_more", "known_reel", "page_cap", "rate_limited", "signed_out",
                     "challenge", "error", "canceled")
RECOVERIES = ("feed_oldest_pk", "top_skip_known", "page_size_12")
FAIL_CODES = ("rate_limited", "signed_out", "challenge", "warning", "not_found", "private", "http_error",
              "parse_error", "permission_denied", "bad_job", "budget")
FORBIDDEN_KEYS = {"cookie", "cookies", "sessionid", "csrftoken"}
VIDEO_MAX = 50 * 1024 * 1024
COVER_MAX = 2 * 1024 * 1024
NEXT_DATA_MAX = int(1.5 * 1024 * 1024)
PAGE_TEXT_MAX = 20000
DEBUG_ENDPOINTS = ("clipsUser", "profileInfo", "currentUser", "chaining", "feedUser", "mediaInfo", "topsearch",
                   "userInfo", "keywordSerp")

# What the member sees for each stop (5.7). Cooldowns in seconds; None = no device cooldown.
MSG = {
    "rate_limited": "Instagram asked us to slow down. ReelVault will wait until tomorrow.",
    "warning": "Instagram asked us to slow down. ReelVault will wait until tomorrow.",
    "signed_out": "You're signed out of Instagram in Chrome. Sign in at instagram.com, then press Try again.",
    "challenge": "Instagram wants you to confirm it's you. Open instagram.com, follow the steps, then press Try "
                 "again tomorrow.",
    "suspended": "Instagram has limited your account. ReelVault stopped reading.",
    "http_error": "Instagram didn't answer. We'll try again soon.",
    "parse_error": "Instagram changed something. We've been told.",
    "bad_job": "This step could not run. We've been told.",
    "budget": "ReelVault reached its safe reading limit for now. It goes on by itself later.",
    "permission_denied": "ReelVault didn't get permission to read the page. Paste the text instead.",
    "page_signed_out": "Sign in to Skool (or LinkedIn) in Chrome, then press Try again.",
    "confirm_account": "Confirm which Instagram account ReelVault reads with, in Settings > Chrome extension.",
}
HTTP_ERROR_ROUNDS = 3       # 5xx/network rounds (3 Instagram tries each) before an Instagram job fails for good
HTTP_ERROR_FINAL = "Instagram kept giving errors for this. ReelVault stopped trying. Try again later."
COOLDOWN_S = {"rate_limited": 24 * 3600, "warning": 24 * 3600, "challenge": 24 * 3600, "signed_out": 30 * 60,
              "http_error": 15 * 60, "suspended": 30 * 24 * 3600}


def pacing() -> dict:
    return dict(TEST_PACING if get_settings().test_pacing else PACING)


# ============================================================================================== small helpers

def _now() -> datetime:
    return db.utcnow()


def _parse_dt(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def norm_handle(h: str) -> str:
    h = (h or "").strip().lstrip("@").lower()
    if not HANDLE_RE.match(h):
        raise bad_input("That Instagram name doesn't look right. Use letters, numbers, dots and underscores.")
    return h


def clean_query(q: str) -> str | None:
    """Make a search query pass validate.js: allowed characters only, single spaces, <= 60 chars."""
    q = re.sub(r"[^A-Za-z0-9 &\-'.]", " ", str(q or ""))
    q = re.sub(r"\s+", " ", q).strip()[:60].strip()
    return q or None


def find_forbidden_keys(obj: Any, depth: int = 0) -> str | None:
    """The server never receives Instagram cookies (11.4): any key named cookie/sessionid/csrftoken is refused."""
    if depth > 40:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in FORBIDDEN_KEYS:
                return k
            hit = find_forbidden_keys(v, depth + 1)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj[:5000]:
            hit = find_forbidden_keys(v, depth + 1)
            if hit:
                return hit
    return None


def ig_scope(dev_or_user_id) -> str:
    """Counter scope for a member's Instagram calls. Keyed by MEMBER, not device (fix round 1): a new pairing or a
    reinstalled extension must not start the day's counts from zero."""
    uid = dev_or_user_id if isinstance(dev_or_user_id, str) else dev_or_user_id.user_id
    return f"igu:{uid}"


def _server_count(conn, scope: str, what: str, window: str) -> int:
    t = db.usage_counters
    row = conn.execute(select(t.c.count).where(t.c.scope == scope, t.c.key == what,
                                               t.c.window == ratelimit.window_key(window))).first()
    return int(row[0]) if row else 0


def _bump_calls(dev, *, ig: int = 0, cdn: int = 0, conn=None) -> None:
    """Server-side count of Instagram calls it can infer (pages, profile lookups, videos), 5.6. Per member."""
    scope = ig_scope(dev)
    for window in ("hour", "day"):
        wk = ratelimit.window_key(window)
        if ig:
            db.bump(db.usage_counters, {"scope": scope, "key": "ig_calls", "window": wk}, conn=conn, count=ig)
        if cdn and window == "day":
            db.bump(db.usage_counters, {"scope": scope, "key": "cdn", "window": wk}, conn=conn, count=cdn)


CARRY_KEYS = ("cooldown_until", "cooldown_reason", "budget")


def carried_state(conn, user_id: str) -> dict:
    """Device state a NEW extension pairing starts with (fix round 1, 5.7): the member's active Instagram stop
    (the latest cooldown of any of their extension devices, revoked ones included) and the last reported budget.
    Disconnect + Connect, or a reinstall, must not clear a 24-hour stop."""
    st = dict(db.DEVICE_STATE_DEFAULT)
    now = _now()
    rows = conn.execute(select(db.devices).where(db.devices.c.user_id == user_id, db.devices.c.kind == "extension")
                        .order_by(db.devices.c.created_at.desc())).all()
    best = None
    for row in rows:
        ds = device_state(row)
        cd = _parse_dt(ds.get("cooldown_until"))
        if cd and cd > now and (best is None or cd > best[0]):
            best = (cd, ds.get("cooldown_reason"))
    if best:
        st["cooldown_until"], st["cooldown_reason"] = db.iso(best[0]), best[1]
    if rows:
        st["budget"] = dict(db.DEVICE_STATE_DEFAULT["budget"], **(device_state(rows[0]).get("budget") or {}))
    return st


def _clean_budget(b: Any) -> dict:
    out = {}
    if isinstance(b, dict):
        for k in ("hour_used", "day_used", "cdn_day_used"):
            v = b.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100000:
                out[k] = v
    return out


def device_state(dev) -> dict:
    st = dict(db.DEVICE_STATE_DEFAULT)
    st.update(dev.state or {})
    st["budget"] = dict(db.DEVICE_STATE_DEFAULT["budget"], **(st.get("budget") or {}))
    return st


def cooldown_active(st: dict, now: datetime | None = None) -> bool:
    cd = _parse_dt(st.get("cooldown_until"))
    return bool(cd and cd > (now or _now()))


def budget_view(conn, dev, state: dict | None = None) -> dict:
    """Budget numbers for /status and lease: the stricter of what the extension reported and what we counted."""
    st = state or device_state(dev)
    rep = st["budget"]
    p = pacing()
    return {"hour_used": max(int(rep.get("hour_used") or 0), _server_count(conn, ig_scope(dev), "ig_calls", "hour")),
            "hour_max": p["hour_max"],
            "day_used": max(int(rep.get("day_used") or 0), _server_count(conn, ig_scope(dev), "ig_calls", "day")),
            "day_max": p["day_max"],
            "cdn_day_used": max(int(rep.get("cdn_day_used") or 0), _server_count(conn, ig_scope(dev), "cdn", "day")),
            "cdn_day_max": p["cdn_day_max"]}


def _set_device_state(conn, dev_id: str, **changes) -> dict:
    row = conn.execute(select(db.devices).where(db.devices.c.id == dev_id)).first()
    st = device_state(row)
    st.update(changes)
    conn.execute(db.devices.update().where(db.devices.c.id == dev_id).values(state=st))
    return st


def apply_ig_report(conn, dev, ig: Any) -> dict:
    """Store the extension's cached sign-in result (hello/progress). Returns the new device fields."""
    if not isinstance(ig, dict):
        return {}
    vals: dict = {}
    st = device_state(dev)
    signed_in = ig.get("signed_in")
    if signed_in is False and ig.get("error") not in (None, "signed_out"):
        signed_in = None      # the check stopped on a 429/checkpoint/warning: that is not "signed out" (fix round 2)
    if isinstance(signed_in, bool):
        st["signed_in"] = signed_in
        if signed_in and st.get("cooldown_reason") == "signed_out":
            st["cooldown_until"], st["cooldown_reason"] = None, None
    checked = ig.get("checked_at")
    if isinstance(checked, (str, int, float)):
        st["ig_checked_at"] = (db.iso(datetime.fromtimestamp(checked / 1000, tz=_now().tzinfo))
                               if isinstance(checked, (int, float)) else str(checked)[:40])
    uid = ig.get("user_id")
    if isinstance(uid, str) and PK_RE.match(uid):
        vals["ig_user_id"] = uid
        uname = ig.get("username")
        if isinstance(uname, str) and HANDLE_RE.match(uname.lower()):
            vals["ig_username"] = uname.lower()
    vals["state"] = st
    conn.execute(db.devices.update().where(db.devices.c.id == dev.id).values(**vals))
    return vals


NO_JOB_STOPS = ("rate_limited", "warning", "challenge", "http_error")


def apply_stop_report(conn, dev, stop: Any) -> str | None:
    """hello {"stop": {"code", "http_status"?, "message"?}}: a 5.7 stop the extension hit OUTSIDE a job (the
    sign-in check at pairing, or the confirm-account re-check). Sets the device cooldown like /fail does, so lease
    answers and the web UI agree (fix round 2). Returns the cooldown code set, or None."""
    if not isinstance(stop, dict) or stop.get("code") not in NO_JOB_STOPS:
        return None
    code = stop["code"]
    message = stop.get("message") if isinstance(stop.get("message"), str) else ""
    if code == "challenge" and "suspended" in message.lower():
        code = "suspended"
    st = device_state(dev)
    now = _now()
    until = now + timedelta(seconds=COOLDOWN_S[code])
    cur = _parse_dt(st.get("cooldown_until"))
    if cur is not None and cur >= until:
        return st.get("cooldown_reason")
    _set_device_state(conn, dev.id, cooldown_until=db.iso(until), cooldown_reason=code)
    if code == "suspended":
        _pause_all_ig(conn, dev.user_id, MSG["suspended"])
    http_status = stop.get("http_status") if isinstance(stop.get("http_status"), int) else None
    db.audit("ig_fail", user_id=dev.user_id, conn=conn,
             detail={"job": None, "kind": "sign_in_check", "code": code, "http_status": http_status,
                     "device": dev.id, "raw_keys": [], "suspended": code == "suspended"})
    return code


def version_tuple(v) -> tuple:
    try:
        return tuple(int(x) for x in str(v or "0").split("."))
    except ValueError:
        return (0,)


def needs_update(dev) -> bool:
    """An extension older than EXTENSION_MIN gets no work until it is updated (it may not follow this contract)."""
    return bool(dev.version) and version_tuple(dev.version) < version_tuple(EXTENSION_MIN)


def is_confirmed(dev) -> bool:
    return bool(dev.ig_user_id) and dev.ig_user_id == dev.ig_confirmed_user_id


# ============================================================================================== queueing (public)

def _account(conn, user_id: str, account_id: str):
    row = db.get_owned(conn, db.accounts, user_id, account_id)
    if row is None:
        raise not_found()
    return row


def _pull_payload(acc, mode: str, target: int = PULL_TARGET) -> dict:
    p: dict = {"account_id": acc.id, "handle": acc.handle, "mode": mode, "cursor_source": acc.cursor_source or "clips",
               "target": int(target)}
    if acc.ig_pk and PK_RE.match(acc.ig_pk):
        p["ig_pk"] = acc.ig_pk
    if mode == "next":
        if acc.cursor_max_id and CURSOR_RE.match(acc.cursor_max_id):
            p["max_id"] = acc.cursor_max_id
        if acc.oldest_pk and PK_RE.match(acc.oldest_pk):
            p["oldest_pk"] = acc.oldest_pk
    else:
        p["cursor_source"] = "clips"
    return p


def start_pull(user_id: str, account_id: str, mode: str, *, run_after=None, priority: int = 10) -> str:
    """Create a pulls row and its ig_pull job (5.4). One active pull per account: returns the running one's job id.
    mode: first | next | refresh. WP4 checks the daily cap and passes run_after for later days."""
    if mode not in ("first", "next", "refresh"):
        raise bad_input("Unknown pull mode.")
    with db.tx() as conn:
        acc = _account(conn, user_id, account_id)
        norm_handle(acc.handle)
        if acc.status in ("private", "not_found"):
            raise bad_input("ReelVault can't read this account. It is private or doesn't exist.")
        if mode == "next" and acc.cursor_done:
            raise bad_input("You already have all reels from this account.")
        active = conn.execute(select(db.jobs.c.id).where(
            db.jobs.c.user_id == user_id, db.jobs.c.kind == "ig_pull", db.jobs.c.state.in_(db.ACTIVE_JOB_STATES),
            db.jobs.c.dedupe_key == f"ig_pull:{account_id}")).first()
        if active:
            return active[0]
        payload = _pull_payload(acc, mode)
        pull_id = db.new_id("pul")
        paired = conn.execute(select(db.devices.c.id).where(
            db.devices.c.user_id == user_id, db.devices.c.kind == "extension",
            db.devices.c.revoked_at.is_(None))).first() is not None
        msg = ("Keep Chrome open. ReelVault reads in a pinned Instagram tab; you can keep using Chrome." if paired
               else "Waiting for Chrome. Connect the extension in Settings > Chrome extension, then it reads these "
                    "reels.")
        job_id = jobs.enqueue(user_id, "ig_pull", payload, executor="extension", priority=priority,
                              dedupe_key=f"ig_pull:{account_id}", run_after=run_after,
                              max_attempts=IG_MAX_ATTEMPTS, conn=conn,
                              progress={"done": 0, "total": PULL_TARGET, "message": msg})
        conn.execute(db.pulls.insert().values(id=pull_id, user_id=user_id, account_id=account_id, job_id=job_id,
                                              mode=mode, from_max_id=payload.get("max_id"), run_after=run_after))
        if run_after is not None and run_after > _now() and acc.status == "new":
            conn.execute(db.accounts.update().where(db.accounts.c.id == account_id).values(status="scheduled"))
    return job_id


def request_profile(user_id: str, handle: str, purpose: str, account_id: str | None = None,
                    source_id: str | None = None) -> str:
    """ig_profile: look up one public profile. purpose competitor (fills accounts) or member_bio (WP7 source)."""
    if purpose not in ("competitor", "member_bio"):
        raise bad_input("Unknown profile purpose.")
    handle = norm_handle(handle)
    payload: dict = {"handle": handle, "purpose": purpose}
    with db.tx() as conn:
        if account_id:
            _account(conn, user_id, account_id)
            payload["account_id"] = account_id
        if source_id:
            if db.get_owned(conn, db.profile_sources, user_id, source_id) is None:
                raise not_found()
            payload["source_id"] = source_id
            conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == source_id)
                         .values(status="waiting", via="extension"))
        ref = account_id or source_id or handle
        jid = jobs.enqueue(user_id, "ig_profile", payload, executor="extension", priority=20,
                           dedupe_key=f"ig_profile:{user_id}:{purpose}:{ref}", max_attempts=IG_MAX_ATTEMPTS, conn=conn)
        if source_id:
            conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == source_id).values(job_id=jid))
    return jid


def request_videos(user_id: str, reel_ids: list[str]) -> str | None:
    """Batch reels (<= 30 per job) whose video the server can't fetch into ig_videos jobs (5.4, 5.8).
    Skips reels already in an active ig_videos job or already downloaded. Returns the first job id or None."""
    r = db.reels
    ids = [x for x in dict.fromkeys(reel_ids or []) if isinstance(x, str) and RVID_RE.match(x)]
    if not ids:
        return None
    first = None
    with db.tx() as conn:
        busy = set()
        for (payload,) in conn.execute(select(db.jobs.c.payload).where(
                db.jobs.c.user_id == user_id, db.jobs.c.kind == "ig_videos",
                db.jobs.c.state.in_(db.ACTIVE_JOB_STATES))).all():
            busy.update(it.get("reel_id") for it in (payload or {}).get("items", []))
        rows = conn.execute(db.scoped(conn, r, user_id).where(r.c.id.in_(ids))).all()
        by_id = {row.id: row for row in rows}
        todo = []
        for rid in ids:
            row = by_id.get(rid)
            if row is None or rid in busy or row.media_state in ("have", "deleted"):
                continue
            if not (PK_RE.match(row.ig_pk or "") and CODE_RE.match(row.code or "")):
                continue
            todo.append(row)
        for i in range(0, len(todo), 30):
            chunk = todo[i:i + 30]
            payload = {"items": [{"reel_id": x.id, "ig_pk": x.ig_pk, "code": x.code} for x in chunk],
                       "refresh_urls": True, "want_cover": any(not x.cover_path for x in chunk)}
            jid = jobs.enqueue(user_id, "ig_videos", payload, executor="extension", priority=40,
                               max_attempts=IG_MAX_ATTEMPTS, conn=conn)
            conn.execute(r.update().where(r.c.user_id == user_id, r.c.id.in_([x.id for x in chunk]))
                         .values(media_state="fetching"))
            first = first or jid
    return first


def schedule_need_extension(user_id: str) -> str | None:
    """WP2's scheduler: reels that the server could not download (media_state=need_extension) go to the extension in
    batches of <= 30, priority tier first, best score first. One active ig_videos job at a time."""
    r = db.reels
    with db.tx(immediate=False) as conn:
        active = conn.execute(select(func.count()).select_from(db.jobs).where(
            db.jobs.c.user_id == user_id, db.jobs.c.kind == "ig_videos",
            db.jobs.c.state.in_(db.ACTIVE_JOB_STATES))).scalar()
        if active:
            return None
        rows = conn.execute(db.scoped(conn, r, user_id).where(r.c.media_state == "need_extension")
                            .order_by((r.c.tier == "priority").desc(), r.c.score.desc().nulls_last(),
                                      r.c.taken_at.desc()).limit(30)).all()
    if not rows:
        return None
    return request_videos(user_id, [x.id for x in rows])


def request_discover(user_id: str, run_id: str, mode: str, *, queries: list[str] | None = None,
                     seeds: list[dict] | None = None) -> str:
    """ig_discover: search (<= 3 queries) or similar (<= 2 seeds). The run's 'niche' mode is a search."""
    mode = "search" if mode == "niche" else mode
    if mode not in ("search", "similar"):
        raise bad_input("Unknown search mode.")
    payload: dict = {"run_id": run_id, "mode": mode}
    with db.tx() as conn:
        if db.get_owned(conn, db.discover_runs, user_id, run_id) is None:
            raise not_found()
        if mode == "search":
            qs = [q for q in (clean_query(x) for x in (queries or [])) if q]
            qs = list(dict.fromkeys(qs))[:3]
            if not qs:
                raise bad_input("Give at least one search word.")
            payload["queries"] = qs
        else:
            ss = []
            for s in (seeds or [])[:2]:
                h = norm_handle(s.get("handle", "") if isinstance(s, dict) else str(s))
                item = {"handle": h}
                pk = s.get("pk") if isinstance(s, dict) else None
                if isinstance(pk, str) and PK_RE.match(pk):
                    item["pk"] = pk
                ss.append(item)
            if not ss:
                raise bad_input("Give one or two accounts to start from.")
            payload["seeds"] = ss
        jid = jobs.enqueue(user_id, "ig_discover", payload, executor="extension", priority=20,
                           dedupe_key=f"ig_discover:{run_id}", max_attempts=IG_MAX_ATTEMPTS, conn=conn)
        conn.execute(db.discover_runs.update().where(db.discover_runs.c.id == run_id).values(state="searching"))
    return jid


def request_check(user_id: str, run_id: str, candidates: list[dict]) -> str:
    """ig_check: profile + first clips page (12) for <= 15 candidates (6.6.1)."""
    out = []
    for c in (candidates or [])[:15]:
        cid = c.get("cand_id") or c.get("id")
        if not (isinstance(cid, str) and RVID_RE.match(cid)):
            continue
        item = {"cand_id": cid, "handle": norm_handle(c.get("handle", ""))}
        if isinstance(c.get("pk"), str) and PK_RE.match(c["pk"]):
            item["pk"] = c["pk"]
        out.append(item)
    if not out:
        raise bad_input("No accounts to check.")
    with db.tx() as conn:
        if db.get_owned(conn, db.discover_runs, user_id, run_id) is None:
            raise not_found()
        jid = jobs.enqueue(user_id, "ig_check", {"run_id": run_id, "candidates": out}, executor="extension",
                           priority=20, dedupe_key=f"ig_check:{run_id}", max_attempts=IG_MAX_ATTEMPTS, conn=conn)
        conn.execute(db.discover_runs.update().where(db.discover_runs.c.id == run_id).values(state="checking"))
    return jid


def request_page_read(user_id: str, source_id: str, kind: str, url: str) -> str:
    """page_read: the member's OWN Skool or LinkedIn page, read in their browser on their click (5.4)."""
    if kind not in PAGE_KINDS:
        raise bad_input("Unknown page kind.")
    if kind == "linkedin_self":
        url = LINKEDIN_SELF
    elif not PAGE_URL_RE.match(url or ""):
        raise bad_input("That Skool link doesn't look right. Use your profile link (skool.com/@you) or your "
                        "group's About page (skool.com/your-group/about).")
    ratelimit.hit("page_read", user_id, 5, "day", message="You can read 5 pages a day. Paste the text instead, "
                                                          "or try again tomorrow.")
    with db.tx() as conn:
        if db.get_owned(conn, db.profile_sources, user_id, source_id) is None:
            raise not_found()
        jid = jobs.enqueue(user_id, "page_read", {"source_id": source_id, "kind": kind, "url": url},
                           executor="extension", priority=10, dedupe_key=f"page_read:{source_id}", conn=conn,
                           progress={"done": 0, "total": 1, "message": "Waiting for your Chrome extension"})
        conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == source_id)
                     .values(status="waiting", via="extension", job_id=jid))
    return jid


# ============================================================================================== payload validation (server twin of validate.js)

def payload_problem(kind: str, p: Any) -> str | None:
    """Same rules as the extension's validate.js (5.4.0). None = fine."""
    if not isinstance(p, dict):
        return "payload"

    def pk_ok(v, opt=True):
        return (opt and v is None) or (isinstance(v, str) and PK_RE.match(v))

    if kind == "ig_pull":
        if not (isinstance(p.get("account_id"), str) and RVID_RE.match(p["account_id"])):
            return "account_id"
        if not (isinstance(p.get("handle"), str) and HANDLE_RE.match(p["handle"])):
            return "handle"
        if p.get("mode") not in ("first", "next", "refresh"):
            return "mode"
        if p.get("cursor_source", "clips") not in ("clips", "feed"):
            return "cursor_source"
        if not pk_ok(p.get("ig_pk")) or not pk_ok(p.get("oldest_pk")):
            return "pk"
        if p.get("max_id") is not None and not (isinstance(p["max_id"], str) and CURSOR_RE.match(p["max_id"])):
            return "max_id"
        t = p.get("target", PULL_TARGET)
        if not (isinstance(t, int) and 1 <= t <= 100):
            return "target"
    elif kind == "ig_profile":
        if not (isinstance(p.get("handle"), str) and HANDLE_RE.match(p["handle"])):
            return "handle"
        if p.get("purpose") not in ("competitor", "member_bio"):
            return "purpose"
    elif kind == "ig_videos":
        items = p.get("items")
        if not (isinstance(items, list) and 1 <= len(items) <= 30):
            return "items"
        for it in items:
            if not (isinstance(it, dict) and isinstance(it.get("reel_id"), str) and RVID_RE.match(it["reel_id"])
                    and pk_ok(it.get("ig_pk"), False) and isinstance(it.get("code"), str)
                    and CODE_RE.match(it["code"])):
                return "items"
    elif kind == "ig_discover":
        if p.get("mode") not in ("search", "similar"):
            return "mode"
        qs = p.get("queries") or []
        if not isinstance(qs, list) or len(qs) > 3 or any(not (isinstance(q, str) and QUERY_RE.match(q)) for q in qs):
            return "queries"
        ss = p.get("seeds") or []
        if not isinstance(ss, list) or len(ss) > 2:
            return "seeds"
        for s in ss:
            if not (isinstance(s, dict) and isinstance(s.get("handle"), str) and HANDLE_RE.match(s["handle"])
                    and pk_ok(s.get("pk"))):
                return "seeds"
    elif kind == "ig_check":
        cs = p.get("candidates")
        if not (isinstance(cs, list) and 1 <= len(cs) <= 15):
            return "candidates"
        for c in cs:
            if not (isinstance(c, dict) and isinstance(c.get("cand_id"), str) and RVID_RE.match(c["cand_id"])
                    and isinstance(c.get("handle"), str) and HANDLE_RE.match(c["handle"]) and pk_ok(c.get("pk"))):
                return "candidates"
    elif kind == "page_read":
        if p.get("kind") not in PAGE_KINDS:
            return "kind"
        u = p.get("url")
        if p["kind"] == "linkedin_self":
            if u != LINKEDIN_SELF:
                return "url"
        elif not (isinstance(u, str) and PAGE_URL_RE.match(u)):
            return "url"
    else:
        return "kind"
    return None


# ============================================================================================== lease

def ext_job(row) -> dict:
    """ExtJob: what the extension receives from lease (it re-validates the payload with validate.js)."""
    payload = {k: v for k, v in (row.payload or {}).items() if not k.startswith("_")}
    return {"id": row.id, "kind": row.kind, "payload": payload, "lease_until": db.iso(row.lease_until),
            "attempt": row.attempts}


def _prepare_lease(conn, job) -> bool:
    """Runs inside lease_external's BEGIN IMMEDIATE: refresh an ig_pull's payload from its account (the cursor may
    have moved since the job was queued) and reset this lease's ingest scratch."""
    if job.kind != "ig_pull":
        return True
    p = dict(job.payload or {})
    acc = conn.execute(select(db.accounts).where(db.accounts.c.id == p.get("account_id"),
                                                 db.accounts.c.user_id == job.user_id)).first()
    if acc is None:
        return True  # handled after the lease (job canceled)
    if acc.ig_pk and PK_RE.match(acc.ig_pk):
        p["ig_pk"] = acc.ig_pk
    if p.get("mode") == "next" and not p.get("_resumed"):
        fresh = _pull_payload(acc, "next", p.get("target", PULL_TARGET))
        for k in ("max_id", "oldest_pk", "cursor_source"):
            if k in fresh:
                p[k] = fresh[k]
            else:
                p.pop(k, None)
    elif acc.oldest_pk and PK_RE.match(acc.oldest_pk):
        p["oldest_pk"] = acc.oldest_pk
    now = _now()
    start = {"source": p.get("cursor_source", "clips") if p.get("mode") == "next" else "clips",
             "max_id": p.get("max_id") if p.get("mode") == "next" else None}
    scratch = dict((job.result or {}).get("ingest") or {})
    scratch.update({"lease_pages": 0, "req": start})
    keep = {k: v for k, v in (job.result or {}).items() if k == "http_error_rounds"}   # rounds survive a lease
    conn.execute(db.jobs.update().where(db.jobs.c.id == job.id).values(payload=p, result={"ingest": scratch, **keep}))
    conn.execute(db.pulls.update().where(db.pulls.c.job_id == job.id, db.pulls.c.started_at.is_(None))
                 .values(started_at=now))
    return True


def lease(dev, kinds: Any, budget: Any) -> dict:
    """POST /api/ext/v2/jobs/lease. Applies the confirm gate, cooldown, budget and daily caps (5.2, 5.6, 5.7)."""
    user_id = dev.user_id
    req = [k for k in (kinds if isinstance(kinds, list) else []) if k in EXT_KINDS]
    try:
        jobs.requeue_expired()
    except Exception:  # noqa: BLE001
        log.exception("requeue_expired failed")
    now = _now()
    with db.tx() as conn:
        dev = conn.execute(select(db.devices).where(db.devices.c.id == dev.id)).first()
        st = device_state(dev)
        rep = _clean_budget(budget)
        if rep:
            st["budget"] = dict(st["budget"], **rep)
            conn.execute(db.devices.update().where(db.devices.c.id == dev.id).values(state=st))
        bv = budget_view(conn, dev, st)
        page_reads_today = _server_count(conn, user_id, "page_read_lease", "day")
    reason = None
    ig_ok = True
    if needs_update(dev):
        return {"job": None, "poll_after_s": 300, "reason": "update"}
    if cooldown_active(st, now):
        ig_ok, reason = False, "cooldown"
    elif not is_confirmed(dev):
        ig_ok, reason = False, "confirm_account"
    elif bv["hour_used"] >= bv["hour_max"] or bv["day_used"] >= bv["day_max"]:
        ig_ok, reason = False, "budget"
    allowed = []
    for k in req:
        if k == "page_read":
            if page_reads_today < PACING["page_read_day_max"]:
                allowed.append(k)
        elif ig_ok:
            if k == "ig_videos" and bv["cdn_day_used"] >= bv["cdn_day_max"]:
                continue
            allowed.append(k)
    if ig_ok and "ig_videos" in allowed:
        try:
            schedule_need_extension(user_id)
        except Exception:  # noqa: BLE001
            log.exception("schedule_need_extension failed")
    for _ in range(6):
        row = jobs.lease_external(user_id, "extension", allowed, dev.id, LEASE_S, prepare=_prepare_lease)
        if row is None:
            break
        problem = payload_problem(row.kind, row.payload)
        if row.kind == "ig_pull" and problem is None:
            with db.tx(immediate=False) as conn:
                if db.get_owned(conn, db.accounts, user_id, row.payload["account_id"]) is None:
                    problem = "account_gone"
        if problem == "account_gone":
            jobs.cancel(row.id, user_id)
            continue
        if problem:
            log.warning("job %s has a bad payload (%s); failing it", row.id, problem)
            jobs.fail(row.id, "bad_job", MSG["bad_job"], owner=dev.id)
            db.audit("ig_bad_job", user_id=user_id, detail={"job": row.id, "field": problem, "side": "server"})
            continue
        if row.kind == "page_read":
            db.bump(db.usage_counters, {"scope": user_id, "key": "page_read_lease",
                                        "window": ratelimit.window_key("day")}, count=1)
        if row.kind == "ig_pull":
            jobs.set_progress(row.id, 0, PULL_TARGET, "Reading reels", owner=dev.id)
        return {"job": ext_job(row)}
    out: dict = {"job": None, "poll_after_s": 30}
    if reason:
        out["reason"] = reason
    return out


def has_work(user_id: str) -> bool:
    now = _now()
    j = db.jobs
    with db.tx(immediate=False) as conn:
        n = conn.execute(select(func.count()).select_from(j).where(
            j.c.user_id == user_id, j.c.executor == "extension", j.c.state.in_(("waiting", "queued")),
            (j.c.run_after.is_(None)) | (j.c.run_after <= now))).scalar()
    return bool(n)


# ============================================================================================== running-job access

def leased_job(dev, job_id: str, kinds: tuple[str, ...] | None = None):
    """The job if this device holds its lease; 404 for another member's id, 409 when the lease is gone."""
    if not (isinstance(job_id, str) and RVID_RE.match(job_id)):
        raise not_found()
    with db.tx(immediate=False) as conn:
        row = conn.execute(db.scoped(conn, db.jobs, dev.user_id).where(db.jobs.c.id == job_id)).first()
    if row is None or row.executor != "extension":
        raise not_found()
    if kinds and row.kind not in kinds:
        raise bad_input("This upload doesn't fit this job.")
    if row.state != "running" or row.lease_owner != dev.id:
        raise ApiError(409, "busy", "This job is no longer yours. Drop it and ask for new work.",
                       detail={"state": row.state})
    return row


def progress(dev, job_id: str, body: dict) -> dict:
    """POST /jobs/<id>/progress: renew the lease, keep the cached sign-in result, tell the extension to stop."""
    if not (isinstance(job_id, str) and RVID_RE.match(job_id)):
        raise not_found()
    with db.tx(immediate=False) as conn:
        row = conn.execute(db.scoped(conn, db.jobs, dev.user_id).where(db.jobs.c.id == job_id)).first()
    if row is None or row.executor != "extension":
        raise not_found()
    if row.state in ("canceled", "done", "failed"):
        return {"ok": True, "lease_until": None, "cancel": True}
    if row.state != "running" or row.lease_owner != dev.id:
        raise ApiError(409, "busy", "This job is no longer yours. Drop it and ask for new work.",
                       detail={"state": row.state})
    now = _now()
    until = now + timedelta(seconds=LEASE_S)
    with db.tx() as conn:
        d = conn.execute(select(db.devices).where(db.devices.c.id == dev.id)).first()
        vals = apply_ig_report(conn, d, body.get("ig"))
        rep = _clean_budget(body.get("budget"))
        if rep:
            st = dict(vals.get("state") or device_state(d))
            st["budget"] = dict(st["budget"], **rep)
            conn.execute(db.devices.update().where(db.devices.c.id == dev.id).values(state=st))
        d = conn.execute(select(db.devices).where(db.devices.c.id == dev.id)).first()
        if row.kind in IG_KINDS and not is_confirmed(d):
            conn.execute(db.jobs.update().where(db.jobs.c.id == job_id).values(
                state="waiting", lease_owner=None, lease_until=None,
                attempts=func.max(db.jobs.c.attempts - 1, 0) if db.is_sqlite() else func.greatest(db.jobs.c.attempts - 1, 0),
                error_message=MSG["confirm_account"]))
            return {"ok": True, "lease_until": None, "cancel": True, "reason": "confirm_account"}
        done = body.get("done")
        total = body.get("total")
        msg = body.get("message")
        prog = dict(row.progress or {})
        if isinstance(done, int) and not isinstance(done, bool):
            prog["done"] = max(0, min(done, 10**6))
        if isinstance(total, int) and not isinstance(total, bool):
            prog["total"] = max(0, min(total, 10**6))
        if isinstance(msg, str):
            prog["message"] = msg[:200]
        vals2: dict = {"progress": prog, "lease_until": until}
        cur = body.get("cursor")
        if isinstance(cur, dict) and isinstance(cur.get("max_id"), str) and CURSOR_RE.match(cur["max_id"]):
            res = dict(row.result or {})
            res["last_cursor"] = cur["max_id"]
            vals2["result"] = res
        conn.execute(db.jobs.update().where(db.jobs.c.id == job_id, db.jobs.c.lease_owner == dev.id).values(**vals2))
    return {"ok": True, "lease_until": db.iso(until), "cancel": False}


# ============================================================================================== ingest: accounts

def _apply_account(conn, user_id: str, acc, a: igparse.AccountIn) -> None:
    vals = {"ig_pk": a.pk, "full_name": a.full_name, "bio": a.biography, "category": a.category,
            "is_private": a.is_private}
    for k in ("followers", "following", "media_count"):
        if getattr(a, k) is not None:
            vals[k] = getattr(a, k)
    if a.is_private:
        vals["status"] = "private"
    elif acc.status in ("not_found", "error"):
        vals["status"] = "new"
    conn.execute(db.accounts.update().where(db.accounts.c.id == acc.id, db.accounts.c.user_id == user_id)
                 .values(**vals))


def _check_account_upload(acc, a: igparse.AccountIn) -> None:
    if acc.ig_pk and a.pk != acc.ig_pk:
        raise bad_input("This profile is not the account of this job.")
    if not acc.ig_pk and a.username != acc.handle:
        raise bad_input("This profile is not the account of this job.")


def _refresh_account_stats(conn, user_id: str, acc_id: str) -> None:
    """reels_count + newest/oldest owned, non-pinned reel (the deterministic fallback cursor, 5.4)."""
    r, ra = db.reels, db.reel_accounts
    n = conn.execute(select(func.count()).select_from(ra).where(ra.c.user_id == user_id,
                                                                ra.c.account_id == acc_id)).scalar() or 0
    owned = (select(r.c.ig_pk, r.c.taken_at).select_from(r.join(ra, and_(ra.c.reel_id == r.c.id,
                                                                          ra.c.account_id == acc_id)))
             .where(r.c.user_id == user_id, ra.c.role == "owner", r.c.is_pinned.is_(False),
                    r.c.taken_at.is_not(None)))
    oldest = conn.execute(owned.order_by(r.c.taken_at.asc()).limit(1)).first()
    newest = conn.execute(owned.order_by(r.c.taken_at.desc()).limit(1)).first()
    vals = {"reels_count": int(n)}
    if oldest:
        vals.update(oldest_pk=oldest.ig_pk, oldest_taken_at=oldest.taken_at)
    if newest:
        vals.update(newest_pk=newest.ig_pk, newest_taken_at=newest.taken_at)
    conn.execute(db.accounts.update().where(db.accounts.c.id == acc_id).values(**vals))


# ============================================================================================== ingest: reels

def _reel_values(ri: igparse.ReelIn, acc, now: datetime) -> dict:
    return dict(code=ri.code, url=ri.url, is_collab=ri.is_collab, taken_at=ri.taken_at, caption=ri.caption,
                duration=ri.duration, has_audio=ri.has_audio, is_paid_partnership=ri.is_paid_partnership,
                coauthors=ri.coauthors, audio=ri.audio, views=ri.views, likes=ri.likes, comments=ri.comments,
                views_field=ri.views_field, shares=ri.shares, reposts=ri.reposts, counts_hidden=ri.counts_hidden,
                followers_at_pull=acc.followers, video_url=ri.video_url, cover_url=ri.cover_url,
                video_url_expires_at=ri.video_url_expires_at, last_seen_at=now)


def upsert_reels(conn, user_id: str, acc, reels_in: list[igparse.ReelIn], *, seen_at: datetime,
                 remaining: int | None = None, refresh: bool = False) -> dict:
    """Upsert normalized reels of one page for account `acc` (5.8). Returns counters + cut index.

    remaining: how many NEW reels this pull may still take (None = no limit); when a page has more new reels than
    that, the rest of the page is left for the next pull (`cut`)."""
    r, ra = db.reels, db.reel_accounts
    now = _now()
    pks = list(dict.fromkeys(x.ig_pk for x in reels_in))
    existing = {row.ig_pk: row for row in conn.execute(
        db.scoped(conn, r, user_id).where(r.c.ig_pk.in_(pks))).all()} if pks else {}
    owner_pks = {x.owner_pk for x in reels_in if x.role == "coauthor"}
    owner_accs = {}
    if owner_pks:
        for row in conn.execute(db.scoped(conn, db.accounts, user_id).where(db.accounts.c.ig_pk.in_(owner_pks))).all():
            owner_accs[row.ig_pk] = row
    out = {"new": 0, "updated": 0, "collabs": 0, "known_hit": False, "cut": False, "pk_conflicts": 0,
           "known": 0, "seen": 0, "older_than_oldest": 0}
    done_pks = set()
    for ri in reels_in:
        if ri.ig_pk in done_pks:
            continue
        done_pks.add(ri.ig_pk)
        out["seen"] += 1
        if ri.pk_conflict:
            out["pk_conflicts"] += 1
        if ri.role == "coauthor":
            out["collabs"] += 1
        vals = _reel_values(ri, acc, now)
        row = existing.get(ri.ig_pk)
        if row is not None:
            out["known"] += 1
            out["updated"] += 1
            if refresh and ri.role == "owner" and not ri.is_pinned:
                out["known_hit"] = True
            upd = dict(vals)
            for k in ("views", "likes", "comments", "shares", "reposts"):  # keep a known number over a missing one
                if upd[k] is None and not ri.counts_hidden:
                    upd.pop(k)
            for k in ("video_url", "cover_url", "video_url_expires_at", "followers_at_pull"):
                if upd[k] is None:
                    upd.pop(k)
            if ri.role == "owner":
                upd["is_pinned"] = ri.is_pinned
                upd["account_id"] = acc.id  # the owner's account always wins (a collab never flips it back)
            conn.execute(r.update().where(r.c.id == row.id).values(**upd))
            reel_id = row.id
        else:
            if remaining is not None and remaining <= 0:
                out["cut"] = True
                break
            reel_id = db.new_id("reel")
            account_id = acc.id
            if ri.role == "coauthor" and ri.owner_pk in owner_accs:
                account_id = owner_accs[ri.owner_pk].id
            ins = dict(vals, id=reel_id, user_id=user_id, account_id=account_id, ig_pk=ri.ig_pk,
                       is_pinned=ri.is_pinned if ri.role == "owner" else False, first_seen_at=now,
                       media_state="none")
            try:
                with conn.begin_nested():
                    conn.execute(r.insert().values(**ins))
                out["new"] += 1
                if remaining is not None:
                    remaining -= 1
            except IntegrityError:  # another upload stored it a moment ago (Postgres only; SQLite is serialized)
                got = conn.execute(select(r.c.id).where(r.c.user_id == user_id, r.c.ig_pk == ri.ig_pk)).first()
                if got is None:
                    raise
                reel_id = got[0]
                out["known"] += 1
                out["updated"] += 1
        db.upsert(conn, ra, {"reel_id": reel_id, "account_id": acc.id, "user_id": user_id, "role": ri.role,
                             "first_seen_at": now}, do_nothing=True)
        if ri.role == "coauthor" and ri.owner_pk in owner_accs:
            db.upsert(conn, ra, {"reel_id": reel_id, "account_id": owner_accs[ri.owner_pk].id, "user_id": user_id,
                                 "role": "owner", "first_seen_at": now}, do_nothing=True)
        db.upsert(conn, db.reel_snapshots, {"reel_id": reel_id, "seen_at": seen_at, "user_id": user_id,
                                            "views": ri.views, "likes": ri.likes, "comments": ri.comments,
                                            "shares": ri.shares}, do_nothing=True)
    return out


def _normalize_items(items: Any, account_pk: str) -> tuple[list[igparse.ReelIn], int]:
    if not isinstance(items, list):
        raise bad_input("items must be a list.")
    if len(items) > 100:
        raise bad_input("Too many items in one page.")
    out = []
    dropped = 0
    for it in items:
        try:
            ri = igparse.normalize(it, account_pk)
        except igparse.IgParseError as e:
            raise bad_input(f"An item is not in the expected shape: {e}.") from None
        if ri is None:
            dropped += 1
        else:
            out.append(ri)
    return out, dropped


def _looks_stale(acc, reels_in: list[igparse.ReelIn], known_pks: set[str]) -> bool:
    """5.8: the first page of a 'next' pull restarted from the top when it holds the newest stored reel, or when
    >= 80% of its owned reels are known and none is older than the oldest stored one."""
    owned = [x for x in reels_in if x.role == "owner" and not x.is_pinned]
    if not owned:
        return False
    if acc.newest_pk and any(x.ig_pk == acc.newest_pk for x in owned):
        return True
    known = sum(1 for x in owned if x.ig_pk in known_pks)
    if known / len(owned) < 0.8:
        return False
    if acc.oldest_taken_at is None:
        return True
    return not any(x.taken_at is not None and x.taken_at < acc.oldest_taken_at for x in owned)


def ingest_items(dev, job_id: str, body: dict) -> dict:
    """POST /jobs/<id>/items for ig_pull, ig_profile (competitor) and ig_check."""
    job = leased_job(dev, job_id, ("ig_pull", "ig_profile", "ig_check"))
    if job.kind == "ig_check":
        return _ingest_check(dev, job, body)
    if job.kind == "ig_profile":
        return _ingest_profile(dev, job, body)
    return _ingest_pull(dev, job, body)


def _ingest_profile(dev, job, body: dict) -> dict:
    p = job.payload or {}
    if p.get("purpose") != "competitor" or not p.get("account_id"):
        raise bad_input("This upload doesn't fit this job.")
    try:
        a = igparse.parse_user(body.get("account") or {})
    except igparse.IgParseError as e:
        raise bad_input(f"The profile is not in the expected shape: {e}.") from None
    with db.tx() as conn:
        acc = _account(conn, dev.user_id, p["account_id"])
        _check_account_upload(acc, a)
        _apply_account(conn, dev.user_id, acc, a)
    _bump_calls(dev, ig=1)
    return {"ok": True, "accepted": 0, "known_hit": False, "stop": True}


def _ingest_check(dev, job, body: dict) -> dict:
    p = job.payload or {}
    cand = body.get("candidate") or {}
    cid = cand.get("cand_id") if isinstance(cand, dict) else None
    if cid not in {c["cand_id"] for c in p.get("candidates", [])}:
        raise bad_input("This candidate is not part of this job.")
    try:
        a = igparse.parse_user(body.get("account") or {})
    except igparse.IgParseError as e:
        raise bad_input(f"The profile is not in the expected shape: {e}.") from None
    reels_in, _ = _normalize_items(body.get("items") or [], a.pk)
    reels_in = reels_in[:12]
    with db.tx(immediate=False) as conn:
        crow = db.get_owned(conn, db.discover_candidates, dev.user_id, cid)
    if crow is None:
        raise not_found()
    if a.username != crow.handle and (crow.pk and a.pk != crow.pk):
        raise bad_input("This profile is not the candidate of this job.")
    _bump_calls(dev, ig=2)
    items = [x.to_dict() for x in reels_in]
    try:
        from . import jobs_score

        jobs_score.ingest_candidate(dev.user_id, cid, a.to_dict(), items)
    except NotImplementedError:
        _fallback_candidate(dev.user_id, cid, a, reels_in)
    return {"ok": True, "accepted": len(items), "known_hit": False, "stop": True}


def _fallback_candidate(user_id: str, cid: str, a: igparse.AccountIn, reels_in: list[igparse.ReelIn]) -> None:
    """Until WP4's ingest_candidate exists: store the profile numbers and mark the candidate checked."""
    taken = [x.taken_at for x in reels_in if x.taken_at]
    with db.tx() as conn:
        conn.execute(db.discover_candidates.update().where(db.discover_candidates.c.id == cid,
                                                           db.discover_candidates.c.user_id == user_id)
                     .values(pk=a.pk, full_name=a.full_name, followers=a.followers, is_private=a.is_private,
                             is_verified=a.is_verified, last_post_at=max(taken) if taken else None,
                             checked_at=_now(), status="checked"))


def _ingest_pull(dev, job, body: dict) -> dict:
    user_id = dev.user_id
    p = job.payload or {}
    paging = body.get("paging") if isinstance(body.get("paging"), dict) else {}
    source = paging.get("source", "clips")
    if source not in ("clips", "feed"):
        raise bad_input("paging.source must be clips or feed.")
    next_max = paging.get("max_id")
    if next_max is not None and not (isinstance(next_max, str) and CURSOR_RE.match(next_max)):
        raise bad_input("paging.max_id doesn't look right.")
    more = paging.get("more_available")
    more = bool(more) if isinstance(more, bool) else True
    req_max = paging.get("request_max_id", "__unset__")
    if req_max not in (None, "__unset__") and not (isinstance(req_max, str) and CURSOR_RE.match(req_max)):
        raise bad_input("paging.request_max_id doesn't look right.")
    account_up = body.get("account")
    a = None
    if account_up is not None:
        try:
            a = igparse.parse_user(account_up)
        except igparse.IgParseError as e:
            raise bad_input(f"The profile is not in the expected shape: {e}.") from None

    with db.tx() as conn:
        acc = _account(conn, user_id, p["account_id"])
        if a is not None:
            _check_account_upload(acc, a)
            _apply_account(conn, user_id, acc, a)
            acc = _account(conn, user_id, p["account_id"])
        if not acc.ig_pk:
            raise bad_input("Send the account (with its pk) in the first upload.")
        pull = conn.execute(select(db.pulls).where(db.pulls.c.job_id == job.id,
                                                   db.pulls.c.user_id == user_id)).first()
        if pull is None:
            raise not_found()
        cur_job = conn.execute(select(db.jobs.c.result).where(db.jobs.c.id == job.id)).first()
        scratch = dict(((cur_job[0] if cur_job else None) or {}).get("ingest") or {})
        reels_in, dropped = _normalize_items(body.get("items") or [], acc.ig_pk)
        if req_max == "__unset__":
            req = scratch.get("req") or {}
            req_max = req.get("max_id") if req.get("source", "clips") == source else None
        mode = p.get("mode")
        lease_pages = int(scratch.get("lease_pages") or 0)
        target = int(p.get("_pull_target") or p.get("target") or PULL_TARGET)  # for the whole pull
        pks = [x.ig_pk for x in reels_in]
        known_pks = {row[0] for row in conn.execute(select(db.reels.c.ig_pk).where(
            db.reels.c.user_id == user_id, db.reels.c.ig_pk.in_(pks)))} if pks else set()

        resume = None
        if (mode == "next" and lease_pages == 0 and pull.recovery != "top_skip_known"
                and not scratch.get("skip_known") and _looks_stale(acc, reels_in, known_pks)):
            if pull.recovery == "feed_oldest_pk" or source == "feed" or not acc.oldest_pk:
                resume = {"source": "clips", "max_id": None, "skip_known": True}
                recovery = "top_skip_known"
            else:
                resume = {"source": "feed", "max_id": f"{acc.oldest_pk}_{acc.ig_pk}"}
                recovery = "feed_oldest_pk"
            conn.execute(db.pulls.update().where(db.pulls.c.id == pull.id).values(recovery=recovery))

        remaining = max(0, target - int(pull.reels_new or 0))
        seen_at = pull.started_at or _now()
        res = upsert_reels(conn, user_id, acc, reels_in, seen_at=seen_at, remaining=remaining,
                           refresh=(mode == "refresh"))
        new_total = int(pull.reels_new or 0) + res["new"]
        pages = int(pull.pages or 0) + 1
        to_max = pull.to_max_id
        if res["cut"]:
            scratch["cut"] = {"source": source, "max_id": req_max}
            to_max = req_max
        elif resume is None:
            scratch["last"] = {"source": source, "max_id": next_max, "more": more}
            if next_max:
                to_max = next_max
        if resume is not None:
            scratch["req"] = {"source": resume["source"], "max_id": resume["max_id"]}
            if resume.get("skip_known"):
                scratch["skip_known"] = True
        else:
            scratch["req"] = {"source": source, "max_id": next_max}
        scratch["lease_pages"] = lease_pages + 1
        conn.execute(db.pulls.update().where(db.pulls.c.id == pull.id).values(
            pages=pages, reels_new=new_total, reels_updated=int(pull.reels_updated or 0) + res["updated"],
            collabs_seen=int(pull.collabs_seen or 0) + res["collabs"], to_max_id=to_max))
        conn.execute(db.jobs.update().where(db.jobs.c.id == job.id).values(
            result={"ingest": scratch},
            progress={"done": min(new_total, target), "total": target,
                      "message": f"Read {new_total} of {target} reels"}))
        _refresh_account_stats(conn, user_id, acc.id)
        if res["pk_conflicts"]:
            db.audit("ig_pk_conflict", user_id=user_id, detail={"job": job.id, "count": res["pk_conflicts"]},
                     conn=conn)
    _bump_calls(dev, ig=1)
    stop = bool(res["cut"] or new_total >= target or (mode == "refresh" and res["known_hit"]) or pages >= PAGE_CAP)
    out = {"ok": True, "accepted": res["new"], "known_hit": res["known_hit"], "stop": stop and resume is None}
    if resume is not None:
        out["resume"] = resume
    return out


# ============================================================================================== videos, pages, bios

def check_video_item(dev, job_id: str, reel_id: str):
    """Before the bytes are saved: the reel must be one of this ig_videos job's items."""
    job = leased_job(dev, job_id, ("ig_videos",))
    if reel_id not in {it.get("reel_id") for it in (job.payload or {}).get("items", [])}:
        raise bad_input("This reel is not part of this job.")
    with db.tx(immediate=False) as conn:
        row = db.get_owned(conn, db.reels, dev.user_id, reel_id)
    if row is None:
        raise not_found()
    return job, row


def save_video(dev, job_id: str, reel_id: str, video_tmp, video_size: int, cover_tmp=None, cover_size: int = 0) -> dict:
    """Move an uploaded video (and cover) into the member's folder and mark the reel `have` (3.17)."""
    from . import storage

    job, row = check_video_item(dev, job_id, reel_id)
    rel_v = f"reels/{reel_id}/video.mp4"
    dest = storage.member_path(dev.user_id, rel_v)
    dest.parent.mkdir(parents=True, exist_ok=True)
    video_tmp.replace(dest)
    vals = {"media_state": "have", "video_path": rel_v}
    added = video_size
    if cover_tmp is not None:
        rel_c = f"reels/{reel_id}/cover.jpg"
        cdest = storage.member_path(dev.user_id, rel_c)
        cover_tmp.replace(cdest)
        vals["cover_path"] = rel_c
        added += cover_size
    with db.tx() as conn:
        conn.execute(db.reels.update().where(db.reels.c.id == reel_id, db.reels.c.user_id == dev.user_id)
                     .values(**vals))
        conn.execute(db.users.update().where(db.users.c.id == dev.user_id)
                     .values(storage_bytes=db.users.c.storage_bytes + added))
        res = dict(job.result or {})
        up = list(res.get("uploaded_ids") or [])
        if reel_id not in up:
            up.append(reel_id)
        res["uploaded_ids"] = up
        items = (job.payload or {}).get("items", [])
        conn.execute(db.jobs.update().where(db.jobs.c.id == job.id).values(
            result=res, progress={"done": len(up), "total": len(items),
                                  "message": f"Saved {len(up)} of {len(items)} videos"}))
    _bump_calls(dev, ig=1, cdn=1)
    _advance(dev.user_id, reel_id)
    return {"ok": True}


def _advance(user_id: str, reel_id: str) -> None:
    try:
        from . import pipeline

        pipeline.advance(user_id, reel_id)
    except NotImplementedError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("pipeline.advance failed for %s", reel_id)


def _fallback_skool(next_data: dict) -> tuple[str, dict]:
    pp = ((next_data or {}).get("props") or {}).get("pageProps") or next_data or {}
    lines, meta = [], {}
    g = pp.get("currentGroup") or {}
    gm = g.get("metadata") or {}
    for k in ("displayName", "name"):
        if isinstance(gm.get(k) or g.get(k), str):
            meta["display_name"] = gm.get(k) or g.get(k)
            lines.append(meta["display_name"])
            break
    for k in ("description", "aboutDescription", "tagline"):
        if isinstance(gm.get(k), str):
            lines.append(gm[k])
    if isinstance(gm.get("totalMembers"), int):
        meta["total_members"] = gm["totalMembers"]
    u = pp.get("currentUser") or pp.get("user") or {}
    um = u.get("metadata") or {}
    name = " ".join(x for x in (u.get("firstName"), u.get("lastName")) if isinstance(x, str))
    if name:
        lines.append(name)
    for k in ("bio", "location", "linkWebsite", "linkLinkedin", "linkInstagram"):
        if isinstance(um.get(k), str) and um[k]:
            lines.append(um[k])
    return "\n".join(lines)[:PAGE_TEXT_MAX], meta


def _parse_with_wp7(kind: str, next_data, text) -> tuple[str, dict]:
    """WP7 parsers (importers.skool.parse_next_data / importers.linkedin.clean_page) with a small fallback."""
    try:
        if kind == "linkedin_self":
            from ..importers import linkedin

            out = linkedin.clean_page(text or "")
        else:
            from ..importers import skool

            out = skool.parse_next_data(next_data or {})
    except (NotImplementedError, AttributeError, ImportError):
        if kind == "linkedin_self":
            return re.sub(r"\s+", " ", text or "").strip()[:PAGE_TEXT_MAX], {}
        return _fallback_skool(next_data or {})
    if isinstance(out, str):
        return out[:PAGE_TEXT_MAX], {}
    if isinstance(out, dict):
        return str(out.get("text") or "")[:PAGE_TEXT_MAX], dict(out.get("meta") or {})
    return str(getattr(out, "text", "") or "")[:PAGE_TEXT_MAX], dict(getattr(out, "meta", {}) or {})


def save_page(dev, job_id: str, body: dict) -> dict:
    """POST /jobs/<id>/page: the member's own Skool/LinkedIn page -> profile_sources (via extension)."""
    job = leased_job(dev, job_id, ("page_read",))
    p = job.payload or {}
    final_url = body.get("final_url")
    if not isinstance(final_url, str) or len(final_url) > 500:
        raise bad_input("final_url is missing.")
    kind = p.get("kind")
    if kind == "linkedin_self":
        if not LINKEDIN_FINAL_RE.match(final_url):
            raise bad_input("That is not a LinkedIn profile page.")
        text = body.get("text")
        if not isinstance(text, str):
            raise bad_input("text is missing.")
        if len(text) > PAGE_TEXT_MAX:
            raise bad_input("The page text is too long.")
        next_data = None
    else:
        if not final_url.startswith("https://www.skool.com/"):
            raise bad_input("That is not a Skool page.")
        next_data = body.get("next_data")
        if not isinstance(next_data, dict):
            raise bad_input("next_data is missing.")
        if len(json.dumps(next_data)) > NEXT_DATA_MAX:
            raise bad_input("The page data is too big.")
        text = None
    parsed_text, meta = _parse_with_wp7(kind, next_data, text)
    title = body.get("title") if isinstance(body.get("title"), str) else ""
    meta = dict(meta, title=title[:300], final_url=final_url)
    with db.tx() as conn:
        src = db.get_owned(conn, db.profile_sources, dev.user_id, p.get("source_id"))
        if src is None:
            raise not_found()
        conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == src.id).values(
            text=parsed_text, meta=meta, status="ok" if parsed_text.strip() else "needs_paste", via="extension",
            error=None if parsed_text.strip() else "We couldn't find text on that page. Paste it instead.",
            fetched_at=_now()))
    return {"ok": True}


def save_member_bio(dev, body: dict) -> dict:
    """POST /ig-user: the member's own Instagram bio (ig_profile purpose member_bio) -> WP7 save_ig_bio."""
    job = leased_job(dev, body.get("job_id"), ("ig_profile",))
    p = job.payload or {}
    if p.get("purpose") != "member_bio" or not p.get("source_id"):
        raise bad_input("This upload doesn't fit this job.")
    try:
        a = igparse.parse_user(body.get("user") or {})
    except igparse.IgParseError as e:
        raise bad_input(f"The profile is not in the expected shape: {e}.") from None
    if a.username != p.get("handle"):
        raise bad_input("This profile is not the account of this job.")
    _bump_calls(dev, ig=1)
    try:
        from ..importers import instagram

        instagram.save_ig_bio(dev.user_id, p["source_id"], a.to_dict())
    except NotImplementedError:
        lines = [f"Instagram @{a.username}", a.full_name or "", a.biography or "", a.category or ""]
        lines += [f"Link: {b['url']}" for b in a.bio_links]
        if a.external_url:
            lines.append(f"Website: {a.external_url}")
        with db.tx() as conn:
            conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == p["source_id"],
                                                           db.profile_sources.c.user_id == dev.user_id)
                         .values(text="\n".join(x for x in lines if x)[:PAGE_TEXT_MAX], meta=a.to_dict(),
                                 status="ok", fetched_at=_now()))
    return {"ok": True}


def save_debug_sample(dev, body: dict) -> dict:
    """POST /debug-sample: the key tree of a response (values replaced by type names), once per endpoint (5.9)."""
    ep = body.get("endpoint")
    tree = body.get("keys_tree")
    if ep not in DEBUG_ENDPOINTS:
        raise bad_input("Unknown endpoint.")

    def ok(node, depth=0) -> bool:
        if depth > 30:
            return False
        if isinstance(node, dict):
            return all(isinstance(k, str) and len(k) < 100 and ok(v, depth + 1) for k, v in node.items())
        if isinstance(node, list):
            return len(node) <= 1 and all(ok(v, depth + 1) for v in node)
        return node in ("str", "int", "float", "bool", "null", "number", "string", "boolean", "object", "array")

    if not ok(tree) or len(json.dumps(tree)) > 64 * 1024:
        raise bad_input("keys_tree must hold keys and type names only.")
    key = f"internal.debug_sample.{dev.id}.{ep}"
    if db.get_setting(key) is None:
        db.set_setting(key, {"at": db.iso(_now()), "user_id": dev.user_id, "endpoint": ep, "keys_tree": tree})
    return {"ok": True}


# ============================================================================================== done / fail

def _close_pull(conn, job, stop_reason: str, recovery: str | None = None, *, final: bool = True) -> dict:
    """Pull bookkeeping (5.8): cursor, cursor_done, oldest/newest, last_pull_at. Returns the pull result."""
    p = job.payload or {}
    user_id = job.user_id
    pull = conn.execute(select(db.pulls).where(db.pulls.c.job_id == job.id)).first()
    acc = conn.execute(select(db.accounts).where(db.accounts.c.id == p.get("account_id"),
                                                 db.accounts.c.user_id == user_id)).first()
    if pull is None or acc is None:
        return {"pull_id": pull.id if pull else None, "reels_new": 0, "reels_updated": 0, "stop_reason": stop_reason}
    cur = conn.execute(select(db.jobs.c.result).where(db.jobs.c.id == job.id)).first()
    scratch = dict(((cur[0] if cur else None) or {}).get("ingest") or {})
    now = _now()
    pv: dict = {"stop_reason": stop_reason}
    if final:
        pv["finished_at"] = now
    if recovery in RECOVERIES and not pull.recovery:
        pv["recovery"] = recovery
    conn.execute(db.pulls.update().where(db.pulls.c.id == pull.id).values(**pv))
    av: dict = {}
    mode = pull.mode
    if mode in ("first", "next"):
        cut, last = scratch.get("cut"), scratch.get("last")
        if cut:
            av.update(cursor_max_id=cut.get("max_id"), cursor_source=cut.get("source", "clips"), cursor_done=False)
        elif last:
            if last.get("max_id"):
                av.update(cursor_max_id=last["max_id"], cursor_source=last.get("source", "clips"))
            if last.get("more") is False or stop_reason == "no_more":
                av["cursor_done"] = True
        elif stop_reason == "no_more":
            av["cursor_done"] = True
    if final:
        if mode == "refresh":
            av["last_refresh_at"] = now
        else:
            av["last_pull_at"] = now
        if acc.status in ("new", "scheduled", "error"):
            av["status"] = "active"
    if av:
        conn.execute(db.accounts.update().where(db.accounts.c.id == acc.id).values(**av))
    _refresh_account_stats(conn, user_id, acc.id)
    return {"pull_id": pull.id, "reels_new": int(pull.reels_new or 0), "reels_updated": int(pull.reels_updated or 0),
            "stop_reason": stop_reason}


def _queue_score(user_id: str, account_id: str, *, conn=None) -> None:
    jobs.enqueue(user_id, "score_account", {"account_id": account_id}, priority=20,
                 dedupe_key=f"score_account:{account_id}", conn=conn)


VIDEO_FAIL_FOR_GOOD = ("not_found", "mismatch", "no_video", "too_big")


def _bump_media_info(dev, media_info, n_items: int, n_uploaded: int) -> None:
    """Count the extension's mediaInfo calls that no upload counted (save_video counts 1 per upload), 5.6."""
    if not isinstance(media_info, int) or isinstance(media_info, bool):
        return
    extra = min(max(media_info, 0), 2 * max(n_items, 1)) - n_uploaded
    if extra > 0:
        _bump_calls(dev, ig=extra)


def finish(dev, job_id: str, body: dict) -> dict:
    """POST /jobs/<id>/done."""
    job = leased_job(dev, job_id)
    stop_reason = body.get("stop_reason")
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    user_id = dev.user_id
    if job.kind == "ig_pull":
        if stop_reason not in PULL_STOP_REASONS:
            stop_reason = "error"
        rec = result.get("recovery")
        with db.tx() as conn:
            out = _close_pull(conn, job, stop_reason, rec if isinstance(rec, str) else None)
            jobs.complete(job.id, out, owner=dev.id, conn=conn)
            _queue_score(user_id, job.payload["account_id"], conn=conn)
        return {"ok": True}
    if job.kind == "ig_profile":
        p = job.payload or {}
        found = False
        u = result.get("user")
        if p.get("purpose") == "competitor" and isinstance(u, dict) and p.get("account_id"):
            try:
                a = igparse.parse_user(u)
            except igparse.IgParseError as e:
                raise bad_input(f"The profile is not in the expected shape: {e}.") from None
            with db.tx() as conn:
                acc = _account(conn, user_id, p["account_id"])
                _check_account_upload(acc, a)
                _apply_account(conn, user_id, acc, a)
            _bump_calls(dev, ig=1)
            found = True
        jobs.complete(job.id, {"found": found or bool(result.get("found", True))}, owner=dev.id)
        return {"ok": True}
    if job.kind == "ig_videos":
        items = (job.payload or {}).get("items", [])
        ids = {it["reel_id"] for it in items}
        up = set((job.result or {}).get("uploaded_ids") or [])
        failed = [f for f in (result.get("failed") or []) if isinstance(f, dict) and f.get("reel_id") in ids]
        # Only reasons that can't change later fail a reel for good; a CDN error, a network drop or an upload
        # error leaves it waiting for another try (fix round 1: a short CDN throttle lost the whole batch).
        failed_ids = {f["reel_id"] for f in failed if str(f.get("reason") or "") in VIDEO_FAIL_FOR_GOOD} - up
        rest = ids - up - failed_ids
        _bump_media_info(dev, result.get("media_info"), len(items), len(up))
        with db.tx() as conn:
            r = db.reels
            if failed_ids:
                conn.execute(r.update().where(r.c.user_id == user_id, r.c.id.in_(failed_ids),
                                              r.c.media_state != "have").values(media_state="failed"))
            if rest:
                conn.execute(r.update().where(r.c.user_id == user_id, r.c.id.in_(rest),
                                              r.c.media_state == "fetching").values(media_state="need_extension"))
            jobs.complete(job.id, {"uploaded": len(up), "failed": [
                {"reel_id": f["reel_id"], "reason": str(f.get("reason") or "")[:60]} for f in failed]},
                owner=dev.id, conn=conn)
        return {"ok": True}
    if job.kind == "ig_discover":
        users = result.get("users") if isinstance(result.get("users"), list) else []
        n = ingest_discovered(user_id, job.payload["run_id"], users)
        jobs.complete(job.id, {"candidates": n}, owner=dev.id)
        _chain_check(user_id, job.payload["run_id"])
        return {"ok": True}
    if job.kind == "ig_check":
        n = len((job.payload or {}).get("candidates", []))
        with db.tx() as conn:
            conn.execute(db.discover_runs.update().where(db.discover_runs.c.id == job.payload["run_id"],
                                                         db.discover_runs.c.user_id == user_id).values(state="done"))
            jobs.complete(job.id, {"checked": n}, owner=dev.id, conn=conn)
        try:
            from . import jobs_score

            hook = getattr(jobs_score, "after_check", None)
            if hook:
                hook(user_id, job.payload["run_id"])
        except NotImplementedError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("jobs_score.after_check failed")
        return {"ok": True}
    if job.kind == "page_read":
        with db.tx() as conn:
            src = db.get_owned(conn, db.profile_sources, user_id, (job.payload or {}).get("source_id"))
            if src is not None and src.status == "waiting":
                conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == src.id).values(
                    status="needs_paste", error="We couldn't read that page. Paste the text instead."))
            jobs.complete(job.id, {"status": src.status if src else "missing"}, owner=dev.id, conn=conn)
        return {"ok": True}
    raise bad_input("Unknown job kind.")


def ingest_discovered(user_id: str, run_id: str, users: list) -> int:
    """ig_discover result -> discover_candidates (deduped, private and already-tracked dropped, <= 30)."""
    with db.tx() as conn:
        run = db.get_owned(conn, db.discover_runs, user_id, run_id)
        if run is None:
            raise not_found()
        tracked = {row[0] for row in conn.execute(select(db.accounts.c.handle).where(db.accounts.c.user_id == user_id))}
        have = {row[0] for row in conn.execute(select(db.discover_candidates.c.handle).where(
            db.discover_candidates.c.user_id == user_id, db.discover_candidates.c.run_id == run_id))}
        n = 0
        for u in users[:60]:
            if not isinstance(u, dict) or n >= 30:
                continue
            try:
                a = igparse.parse_user(u)
            except igparse.IgParseError:
                continue
            if a.is_private or a.username in tracked or a.username in have:
                continue
            src = u.get("source") if u.get("source") in ("search", "similar") else (
                "similar" if run.mode == "similar" else "search")
            seed = u.get("seed_handle") if isinstance(u.get("seed_handle"), str) and HANDLE_RE.match(
                u.get("seed_handle") or "") else None
            conn.execute(db.discover_candidates.insert().values(
                id=db.new_id("cand"), user_id=user_id, run_id=run_id, handle=a.username, pk=a.pk,
                full_name=a.full_name, followers=a.followers, is_private=False, is_verified=a.is_verified,
                source=src, seed_handle=seed, status="found"))
            have.add(a.username)
            n += 1
        conn.execute(db.discover_runs.update().where(db.discover_runs.c.id == run_id).values(state="found"))
    return n


def _chain_check(user_id: str, run_id: str) -> None:
    """ig_discover -> ig_check on the best <= 15 candidates. WP4 may choose them (jobs_score.pick_check_candidates)."""
    picked = None
    try:
        from . import jobs_score

        fn = getattr(jobs_score, "pick_check_candidates", None)
        if fn:
            picked = fn(user_id, run_id)
    except NotImplementedError:
        picked = None
    except Exception:  # noqa: BLE001
        log.exception("pick_check_candidates failed")
    if picked is None:
        dc = db.discover_candidates
        with db.tx(immediate=False) as conn:
            rows = conn.execute(db.scoped(conn, dc, user_id).where(dc.c.run_id == run_id, dc.c.status == "found")
                                .order_by(dc.c.followers.desc().nulls_last()).limit(15)).all()
        picked = [{"cand_id": r.id, "handle": r.handle, "pk": r.pk} for r in rows]
    if picked:
        request_check(user_id, run_id, picked)
    else:
        with db.tx() as conn:
            conn.execute(db.discover_runs.update().where(db.discover_runs.c.id == run_id).values(state="done"))


def _resume_payload(conn, job) -> dict:
    """After a cooldown an ig_pull continues where it stopped: the next pull page is the saved cursor."""
    p = dict(job.payload or {})
    pull = conn.execute(select(db.pulls).where(db.pulls.c.job_id == job.id)).first()
    cur = conn.execute(select(db.jobs.c.result).where(db.jobs.c.id == job.id)).first()
    scratch = dict(((cur[0] if cur else None) or {}).get("ingest") or {})
    nxt = scratch.get("cut") or scratch.get("req")
    if p.get("mode") in ("first", "next") and nxt and nxt.get("max_id"):
        p.update(mode="next", max_id=nxt["max_id"], cursor_source=nxt.get("source", "clips"), _resumed=True)
    if pull is not None:
        p.setdefault("_pull_target", int(p.get("target") or PULL_TARGET))
        p["target"] = max(1, int(p["_pull_target"]) - int(pull.reels_new or 0))
    return p


def fail(dev, job_id: str, body: dict) -> dict:
    """POST /jobs/<id>/fail: stop rules and cooldowns of 5.7."""
    job = leased_job(dev, job_id)
    code = body.get("code")
    if code not in FAIL_CODES:
        raise bad_input("Unknown failure code.")
    user_id = dev.user_id
    now = _now()
    message = body.get("message") if isinstance(body.get("message"), str) else ""
    http_status = body.get("http_status") if isinstance(body.get("http_status"), int) else None
    raw_keys = [k for k in (body.get("raw_keys") or []) if isinstance(k, str)][:50] \
        if isinstance(body.get("raw_keys"), list) else []
    suspended = code == "challenge" and "suspended" in message.lower()
    db.audit("ig_fail", user_id=user_id, detail={"job": job.id, "kind": job.kind, "code": code,
                                                 "http_status": http_status, "device": dev.id,
                                                 "raw_keys": raw_keys, "suspended": suspended})
    out: dict = {"ok": True}
    p = job.payload or {}

    # page_read never touches Instagram: no device cooldown.
    if job.kind == "page_read":
        with db.tx() as conn:
            src = db.get_owned(conn, db.profile_sources, user_id, p.get("source_id"))
            if code == "signed_out":
                jobs.fail(job.id, code, MSG["page_signed_out"], pause=True, owner=dev.id, conn=conn)
                err = MSG["page_signed_out"]
            elif code == "permission_denied":
                jobs.fail(job.id, code, MSG["permission_denied"], owner=dev.id, conn=conn)
                err = MSG["permission_denied"]
            else:
                jobs.fail(job.id, code, "We couldn't read that page. Paste the text instead.", owner=dev.id, conn=conn)
                err = "We couldn't read that page. Paste the text instead."
            if src is not None:
                conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == src.id)
                             .values(status="needs_paste", error=err))
        return out

    cooldown_code = "suspended" if suspended else code
    if job.kind == "ig_videos":
        items = p.get("items", [])
        up = (job.result or {}).get("uploaded_ids") or []
        _bump_media_info(dev, body.get("media_info"), len(items), len(up))
    if code == "budget":
        cs = body.get("cooldown_s")
        cs = int(cs) if isinstance(cs, (int, float)) and not isinstance(cs, bool) else 3600
        cs = max(60, min(cs, 24 * 3600))
        with db.tx() as conn:
            _wait_again(conn, job, now + timedelta(seconds=cs), MSG["budget"])
        out["cooldown_until"] = None
        return out

    give_up = False
    if cooldown_code in COOLDOWN_S:
        until = now + timedelta(seconds=COOLDOWN_S[cooldown_code])
        # http_error (5xx / network, after the extension's own 3 tries) spends a round: after HTTP_ERROR_ROUNDS
        # the job fails for good instead of repeating the retry ladder forever (fix round 2).
        rounds = int((job.result or {}).get("http_error_rounds") or 0) + 1 if code == "http_error" else 0
        give_up = code == "http_error" and rounds >= HTTP_ERROR_ROUNDS
        with db.tx() as conn:
            changes = {"cooldown_until": db.iso(until), "cooldown_reason": cooldown_code}
            if code == "signed_out":
                changes["signed_in"] = False
            _set_device_state(conn, dev.id, **changes)
            if job.kind == "ig_pull" and not give_up:
                stop = {"rate_limited": "rate_limited", "warning": "rate_limited", "signed_out": "signed_out",
                        "challenge": "challenge"}.get(code, "error")
                _close_pull(conn, job, stop, final=False)
            if suspended:
                _pause_all_ig(conn, user_id, MSG["suspended"])
            elif not give_up:
                _wait_again(conn, job, until, MSG.get(cooldown_code, MSG["http_error"]),
                            refund=code != "http_error", result_extra={"http_error_rounds": rounds} if rounds else None)
        if suspended:
            db.audit("ig_suspended", user_id=user_id, detail={"device": dev.id})
        out["cooldown_until"] = db.iso(until)
        if not give_up:
            return out

    # Final failures for this job (no cooldown).
    msg = {"not_found": f"We couldn't find @{p.get('handle', '')} on Instagram. Check the name.",
           "private": f"@{p.get('handle', '')} is private. ReelVault only reads public accounts.",
           "parse_error": MSG["parse_error"], "bad_job": MSG["bad_job"],
           "permission_denied": MSG["permission_denied"]}.get(code, MSG["http_error"])
    if give_up:
        msg = HTTP_ERROR_FINAL
    with db.tx() as conn:
        if job.kind in ("ig_pull", "ig_profile") and p.get("account_id") and code in ("not_found", "private"):
            conn.execute(db.accounts.update().where(db.accounts.c.id == p["account_id"],
                                                    db.accounts.c.user_id == user_id)
                         .values(status="not_found" if code == "not_found" else "private",
                                 is_private=(code == "private")))
        if job.kind == "ig_profile" and p.get("source_id"):
            conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == p["source_id"],
                                                           db.profile_sources.c.user_id == user_id)
                         .values(status="needs_paste", error=msg))
        if job.kind == "ig_pull":
            res = _close_pull(conn, job, "error")
            if res.get("reels_new"):
                _queue_score(user_id, p["account_id"], conn=conn)
        if job.kind == "ig_videos":
            ids = [it["reel_id"] for it in p.get("items", [])]
            conn.execute(db.reels.update().where(db.reels.c.user_id == user_id, db.reels.c.id.in_(ids),
                                                 db.reels.c.media_state == "fetching")
                         .values(media_state="need_extension" if code not in ("bad_job",) else "failed"))
        if job.kind in ("ig_discover", "ig_check"):
            conn.execute(db.discover_runs.update().where(db.discover_runs.c.id == p.get("run_id"),
                                                         db.discover_runs.c.user_id == user_id).values(state="failed"))
        jobs.fail(job.id, code, msg, owner=dev.id, conn=conn)
    if code == "bad_job":
        db.audit("ig_bad_job", user_id=user_id, detail={"job": job.id, "side": "extension"})
    return out


def _wait_again(conn, job, run_after: datetime, message: str, *, refund: bool = True,
                result_extra: dict | None = None) -> None:
    """Put an Instagram job back to waiting until run_after, without spending one of its attempts (refund=False:
    the attempt stays spent, used for http_error)."""
    vals = dict(state="waiting", lease_owner=None, lease_until=None, run_after=run_after, error_message=message)
    if refund:
        vals["attempts"] = func.max(db.jobs.c.attempts - 1, 0) if db.is_sqlite() \
            else func.greatest(db.jobs.c.attempts - 1, 0)
    if result_extra:
        cur = conn.execute(select(db.jobs.c.result).where(db.jobs.c.id == job.id)).first()
        vals["result"] = dict((cur[0] if cur else None) or {}, **result_extra)
    if job.kind == "ig_pull":
        vals["payload"] = _resume_payload(conn, job)
    if job.kind == "ig_videos":  # after the wait, only the reels not uploaded yet (no repeat mediaInfo calls)
        cur = conn.execute(select(db.jobs.c.result).where(db.jobs.c.id == job.id)).first()
        up = set(((cur[0] if cur else None) or {}).get("uploaded_ids") or [])
        p = dict(job.payload or {})
        left = [it for it in p.get("items", []) if it.get("reel_id") not in up]
        if up and left:
            p["items"] = left
            vals["payload"] = p
    conn.execute(db.jobs.update().where(db.jobs.c.id == job.id).values(**vals))


def _pause_all_ig(conn, user_id: str, message: str) -> None:
    j = db.jobs
    conn.execute(j.update().where(j.c.user_id == user_id, j.c.kind.in_(IG_KINDS),
                                  j.c.state.in_(("waiting", "queued", "running"))).values(
        state="paused", error_code="suspended", error_message=message, lease_owner=None, lease_until=None))


def try_again(user_id: str) -> dict:
    """The web UI's Try again after signed_out (5.7): clear that cooldown and let waiting jobs run now."""
    now = _now()
    n = 0
    with db.tx() as conn:
        dev = conn.execute(select(db.devices).where(db.devices.c.user_id == user_id, db.devices.c.kind == "extension",
                                                    db.devices.c.revoked_at.is_(None))
                           .order_by(db.devices.c.created_at.desc())).first()
        if dev is None:
            raise not_found("Connect the extension first.")
        st = device_state(dev)
        if st.get("cooldown_reason") != "signed_out":
            raise bad_input("Try again works after you sign in to Instagram. Other pauses end by themselves.")
        for d in conn.execute(select(db.devices).where(db.devices.c.user_id == user_id,
                                                       db.devices.c.kind == "extension")).all():
            if device_state(d).get("cooldown_reason") == "signed_out":  # old pairings too (carried_state)
                _set_device_state(conn, d.id, cooldown_until=None, cooldown_reason=None)
        n = conn.execute(db.jobs.update().where(db.jobs.c.user_id == user_id, db.jobs.c.kind.in_(IG_KINDS),
                                                db.jobs.c.state == "waiting", db.jobs.c.run_after > now)
                         .values(run_after=now, error_message=None)).rowcount
    return {"ok": True, "jobs": n}


# ============================================================================================== lease expiry (WP1 hook)

def _on_last_lease_expired(conn, job) -> None:
    """Runs inside jobs.requeue_expired's transaction. A lease that runs out with attempts left just goes back to
    `waiting` (the extension resumes it). After the LAST attempt the job fails, so tidy what it left behind."""
    if job.attempts < job.max_attempts:
        return
    p = job.payload or {}
    uid = job.user_id
    if job.kind == "ig_pull":
        res = _close_pull(conn, job, "error")
        if res.get("reels_new"):
            _queue_score(uid, p["account_id"], conn=conn)
    elif job.kind == "ig_videos":
        ids = [it.get("reel_id") for it in p.get("items", [])]
        conn.execute(db.reels.update().where(db.reels.c.user_id == uid, db.reels.c.id.in_(ids),
                                             db.reels.c.media_state == "fetching").values(media_state="need_extension"))
    elif job.kind in ("ig_discover", "ig_check"):
        conn.execute(db.discover_runs.update().where(db.discover_runs.c.id == p.get("run_id"),
                                                     db.discover_runs.c.user_id == uid).values(state="failed"))
    if p.get("source_id") and job.kind in ("page_read", "ig_profile"):
        conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == p["source_id"],
                                                       db.profile_sources.c.user_id == uid,
                                                       db.profile_sources.c.status == "waiting")
                     .values(status="needs_paste", error="We couldn't read that page. Paste the text instead."))


for _kind in EXT_KINDS:
    jobs.on_lease_expired(_kind)(_on_last_lease_expired)


# ============================================================================================== admin health (5.9)

def ig_health() -> dict:
    """For the admin "Instagram health" page: last key-tree sample per endpoint, last 50 failures, recoveries used,
    pk conflicts. No member content."""
    samples = {}
    with db.tx(immediate=False) as conn:
        for row in conn.execute(select(db.app_settings).where(db.app_settings.c.key.like("internal.debug_sample.%"))):
            v = row.value or {}
            ep = v.get("endpoint")
            if ep and (ep not in samples or (v.get("at") or "") > (samples[ep].get("at") or "")):
                samples[ep] = {"at": v.get("at"), "keys_tree": v.get("keys_tree")}
        fails = conn.execute(select(db.audit_log).where(db.audit_log.c.action == "ig_fail")
                             .order_by(db.audit_log.c.at.desc()).limit(50)).all()
        rec = dict(conn.execute(select(db.pulls.c.recovery, func.count()).where(db.pulls.c.recovery.is_not(None))
                                .group_by(db.pulls.c.recovery)).all())
        conflicts = conn.execute(select(db.audit_log.c.detail).where(db.audit_log.c.action == "ig_pk_conflict")).all()
    fields_seen = {}
    clips = (samples.get("clipsUser") or {}).get("keys_tree") or {}
    media = (((clips.get("items") or [{}])[0] or {}).get("media") or {}) if isinstance(clips, dict) else {}
    for f in ("reshare_count", "caption", "play_count", "ig_play_count", "media_repost_count"):
        fields_seen[f] = f in media if media else None
    return {"samples": samples,
            "fails": [{"at": db.iso(f.at), "code": (f.detail or {}).get("code"), "kind": (f.detail or {}).get("kind"),
                       "http_status": (f.detail or {}).get("http_status"),
                       "raw_keys": (f.detail or {}).get("raw_keys") or []} for f in fails],
            "recoveries": {k: int(v) for k, v in rec.items()},
            "pk_conflicts": sum(int((c[0] or {}).get("count") or 0) for c in conflicts),
            "fields_seen": fields_seen}
