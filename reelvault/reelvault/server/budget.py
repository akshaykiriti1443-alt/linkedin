"""Server work budget, member fair share and ETA (DESIGN-SAAS.md 3.13.3). WP1.

SERVER_MEDIA_PER_DAY caps all media work per UTC day. The day's budget is split evenly across members with media
work (water-filling: unused shares flow to members who still have work), each capped by member_media_per_day.
member_budget(user_id, day).media_left is what the media lease checks (jobs.lease_server).
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from . import db
from .settings import get_settings

DEFAULT_SEC_PER_REEL = 28.0


def today() -> str:
    return db.utcnow().strftime("%Y-%m-%d")


def water_fill(demands: dict[str, int], total: int) -> dict[str, int]:
    """Split `total` across members: everyone gets min(demand, s) with the largest s that fits. Integers."""
    shares = {u: 0 for u in demands}
    left = max(0, int(total))
    active = {u: d for u, d in demands.items() if d > 0}
    while left > 0 and active:
        per = max(1, left // len(active))
        for u in sorted(active, key=lambda x: active[x]):
            if left <= 0:
                break
            give = min(per, active[u] - shares[u], left)
            shares[u] += give
            left -= give
        active = {u: d for u, d in active.items() if shares[u] < d}
    return shares


def recompute_shares(day: str | None = None) -> dict[str, int]:
    """Recompute every member's media share for the day (every 10 min in the worker). Returns share per member."""
    day = day or today()
    j, mb = db.jobs, db.member_budget
    s = get_settings()
    cap_member = int(db.get_setting("member_media_per_day") or 150)
    with db.tx() as conn:
        queued = dict(conn.execute(
            select(j.c.user_id, func.count()).where(j.c.job_class == "media", j.c.executor == "server",
                                                   j.c.state == "queued", j.c.user_id.is_not(None))
            .group_by(j.c.user_id)).all())
        used = dict(conn.execute(select(mb.c.user_id, mb.c.media_used).where(mb.c.day == day)).all())
        # demand = what is still waiting + what was already used today (running jobs were counted as used when
        # they were leased, so they are not counted twice)
        users = set(queued) | set(used)
        demand = {u: min(cap_member, int(queued.get(u, 0)) + int(used.get(u, 0))) for u in users}
        shares = water_fill(demand, s.server_media_per_day)
        for u in users:
            share = shares.get(u, 0)
            left = max(0, share - int(used.get(u, 0)))
            db.upsert(conn, mb, {"user_id": u, "day": day, "media_left": left, "media_used": int(used.get(u, 0))},
                      update={"media_left": left})
    return shares


def member_share(user_id: str, day: str | None = None) -> int:
    """Today's share (used + left) for one member; 0 when none was computed yet."""
    mb = db.member_budget
    with db.tx(immediate=False) as conn:
        row = conn.execute(select(mb.c.media_left, mb.c.media_used).where(
            mb.c.user_id == user_id, mb.c.day == (day or today()))).first()
    return int(row.media_left + row.media_used) if row else 0


def sec_per_reel() -> float:
    """Measured seconds per process_reel job (mean of the last 50), else the 12.5 default."""
    j = db.jobs
    with db.tx(immediate=False) as conn:
        rows = conn.execute(select(j.c.started_at, j.c.finished_at).where(
            j.c.kind == "process_reel", j.c.state == "done", j.c.started_at.is_not(None),
            j.c.finished_at.is_not(None)).order_by(j.c.finished_at.desc()).limit(50)).all()
    secs = [(r.finished_at - r.started_at).total_seconds() for r in rows if r.finished_at and r.started_at]
    secs = [x for x in secs if x > 0]
    return max(1.0, round(sum(secs) / len(secs), 1)) if secs else DEFAULT_SEC_PER_REEL


def _hours_text(hours: float) -> str:
    if hours < 1:
        mins = max(5, int(round(hours * 60 / 5.0)) * 5)
        return f"Ready in about {mins} minutes"
    if hours < 36:
        h = max(1, int(round(hours)))
        return f"Ready in about {h} hour{'s' if h != 1 else ''}"
    d = max(2, int(round(hours / 24)))
    return f"Ready in about {d} days"


def eta(user_id: str, job=None) -> dict | None:
    """{"text": "Ready in about 3 hours", "at": iso} for the member's queued media work, or the plain waiting
    line for Claude/desktop jobs; None when nothing is queued."""
    if job is not None and job.executor == "mcp":
        return {"text": "Waiting for Claude. Open Claude and say: Analyze my new ReelVault reels.", "at": None}
    if job is not None and job.executor == "desktop":
        return {"text": "Waiting for the desktop app. Open ReelVault on your computer.", "at": None}
    if job is not None and job.executor == "extension":
        return None
    j, mb = db.jobs, db.member_budget
    s = get_settings()
    with db.tx(immediate=False) as conn:
        q = conn.execute(select(func.count()).where(j.c.user_id == user_id, j.c.job_class == "media",
                                                    j.c.executor == "server", j.c.state == "queued")).scalar() or 0
        if not q:
            return None
        members = conn.execute(select(func.count(func.distinct(j.c.user_id))).where(
            j.c.job_class == "media", j.c.executor == "server", j.c.state == "queued")).scalar() or 1
        row = conn.execute(select(mb.c.media_left).where(mb.c.user_id == user_id, mb.c.day == today())).first()
    left_today = int(row[0]) if row else 0
    lanes = max(1, s.whisper_concurrency)
    per_hour_server = lanes * 3600.0 / sec_per_reel()
    per_hour = max(1.0, per_hour_server / max(1, members))
    now = db.utcnow()
    if row is None or q <= left_today:  # no share computed yet, or it all fits in today's share
        hours = q / per_hour
    else:
        end_of_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        hours = (end_of_day - now).total_seconds() / 3600 + (q - left_today) / per_hour
    return {"text": _hours_text(hours), "at": db.iso(now + timedelta(hours=hours))}


def load() -> dict:
    """Admin > Health 'load' block (4.3 stats)."""
    j, mb = db.jobs, db.member_budget
    with db.tx(immediate=False) as conn:
        queued = conn.execute(select(func.count()).where(j.c.job_class == "media", j.c.state == "queued")).scalar()
        used = conn.execute(select(func.coalesce(func.sum(mb.c.media_used), 0)).where(mb.c.day == today())).scalar()
    return {"media_queued": int(queued or 0), "budget_used_today": int(used or 0),
            "budget_today": get_settings().server_media_per_day, "sec_per_reel": sec_per_reel()}


def has_queued_media() -> bool:
    """True when any member has server media work waiting (the worker then recomputes shares every minute, so a
    member who just queued work gets a share quickly instead of waiting up to 10 minutes)."""
    j = db.jobs
    with db.tx(immediate=False) as conn:
        return conn.execute(select(j.c.id).where(j.c.job_class == "media", j.c.executor == "server",
                                                 j.c.state == "queued").limit(1)).first() is not None
