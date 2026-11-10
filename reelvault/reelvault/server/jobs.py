"""Job queue, atomic fair leases, worker pools, handler registry, retries (DESIGN-SAAS.md 3.13). WP1.

Register a handler in your own jobs_*.py:

    from .jobs import handler, JobCtx, Retry, Pause, Fail

    @handler("score_account", job_class="light")
    def score_account(job, ctx: JobCtx) -> dict:
        ctx.progress(0, 1, "Scoring reels")
        ...                                   # never hold a db.tx() across slow work
        return {"scored": 100, "outliers": 14}  # -> jobs.result, state done

Raise Retry(delay_s) to try later, Pause("plain message") when the member must act, Fail("plain message") to stop.
Any other exception retries with backoff (attempts < max_attempts), then fails with a plain message.

Queue work:  job_id = jobs.enqueue(user_id, "score_account", {"account_id": acc}, dedupe_key=f"score:{acc}")
Tests:       jobs.drain()   runs every due server job in this thread (all pools), returns the finished rows.
Worker:      python -m reelvault.server.jobs     (RV_ROLE=worker)
"""
from __future__ import annotations

import importlib
import logging
import os
import random
import signal
import socket
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import func, literal, or_, select, update
from sqlalchemy.exc import IntegrityError

from . import db

log = logging.getLogger("reelvault.jobs")

# kind -> (job_class when executor is the server, default executor). 3.13.1
KINDS: dict[str, tuple[str, str]] = {
    "ig_pull": ("external", "extension"), "ig_profile": ("external", "extension"),
    "ig_videos": ("external", "extension"), "ig_discover": ("external", "extension"),
    "ig_check": ("external", "extension"), "page_read": ("external", "extension"),
    "fetch_video": ("media", "server"), "process_reel": ("media", "server"),
    "score_account": ("light", "server"), "pick_sample": ("light", "server"),
    "refresh_insights": ("light", "server"), "import_source": ("light", "server"),
    "tag_reels": ("ai", "server"), "retag_topics": ("ai", "server"), "analyze_reel": ("ai", "server"),
    "name_formats": ("ai", "server"), "make_output": ("ai", "server"), "draft_profile": ("ai", "server"),
    "suggest_topics": ("ai", "server"),
    "sweep_storage": ("light", "server"), "backup": ("light", "server"),
    "expire_members": ("light", "server"), "rotate_code": ("light", "server"),
}
EXTENSION_KINDS = ("ig_pull", "ig_profile", "ig_videos", "ig_discover", "ig_check", "page_read")
DAILY_KINDS = ("sweep_storage", "backup", "expire_members", "rotate_code")
POOLS = ("media", "ai", "light")
LEASE_S = {"media": 180, "ai": 900, "light": 600}
WAIT_MESSAGES = {"extension": "Waiting for your Chrome extension", "mcp": "Waiting for Claude",
                 "desktop": "Waiting for the desktop app"}
GENERIC_FAIL = "Something went wrong with this step. Press Retry, or try again later."
EPOCH = db.utcnow().replace(year=2000, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


class Retry(Exception):
    """Raise to run the job again after delay_s seconds (counts as an attempt)."""

    def __init__(self, delay_s: float = 60, message: str | None = None):
        super().__init__(message or f"retry in {delay_s}s")
        self.delay_s = float(delay_s)
        self.message = message


class Pause(Exception):
    """Raise when the member must act first (bad key, quota, cooldown). message is shown to the member."""

    def __init__(self, message: str, code: str = "paused"):
        super().__init__(message)
        self.message = message
        self.code = code


class Fail(Exception):
    """Raise to stop the job for good. message is shown to the member."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class Handler:
    kind: str
    job_class: str
    fn: Callable[[Any, "JobCtx"], dict | None]


HANDLERS: dict[str, Handler] = {}
_EXPIRE_HOOKS: dict[str, list[Callable]] = {}


def handler(kind: str, job_class: str):
    """Decorator: @handler("process_reel", job_class="media"). fn(job: Row, ctx: JobCtx) -> dict | None."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}; add it to DESIGN-SAAS.md 3.13.1 and jobs.KINDS")
    if job_class not in POOLS:
        raise ValueError(f"job_class must be one of {POOLS}")

    def deco(fn):
        HANDLERS[kind] = Handler(kind, job_class, fn)
        return fn

    return deco


def on_lease_expired(kind: str):
    """Decorator: fn(conn, job_row) runs inside the transaction that re-queues an expired lease of `kind`
    (WP5: put a tag batch's reels back to tag_state='queued')."""
    def deco(fn):
        _EXPIRE_HOOKS.setdefault(kind, []).append(fn)
        return fn

    return deco


HANDLER_MODULES = ("reelvault.server.jobs_ig", "reelvault.server.jobs_media", "reelvault.server.jobs_score",
                   "reelvault.server.jobs_ai", "reelvault.server.jobs_profile")


def load_handlers() -> list[str]:
    """Import every jobs_* module so their @handler decorators run. Returns the registered kinds."""
    for mod in HANDLER_MODULES:
        try:
            importlib.import_module(mod)
        except Exception:  # noqa: BLE001
            log.exception("could not load job handlers from %s", mod)
    return sorted(HANDLERS)


# ============================================================================================== enqueue

def class_for(kind: str, executor: str) -> str:
    if executor != "server":
        return "external"
    return KINDS[kind][0]


def enqueue(user_id: str | None, kind: str, payload: dict | None = None, *, executor: str | None = None,
            job_class: str | None = None, priority: int = 50, dedupe_key: str | None = None,
            parent_id: str | None = None, run_after=None, state: str | None = None, message: str | None = None,
            progress: dict | None = None, max_attempts: int = 3, conn=None) -> str:
    """Create a job and return its id. With dedupe_key, an ACTIVE job with the same key is returned instead.

    executor: server | extension | mcp | desktop; None = the kind's default (extension for ig_*/page_read,
    server for everything else). State defaults to 'queued' (server) or 'waiting' (others);
    pass state='paused' + message for "needs member action" jobs. Pass conn= to join an open transaction.
    user_id None is only for system jobs (daily maintenance).
    """
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    executor = executor or KINDS[kind][1]
    if executor not in ("server", "extension", "mcp", "desktop"):
        raise ValueError(f"bad executor {executor!r}")
    jc = job_class or class_for(kind, executor)
    st = state or ("queued" if executor == "server" else "waiting")
    prog = progress or {"done": 0, "total": 0, "message": WAIT_MESSAGES.get(executor, "")}
    vals = dict(user_id=user_id, kind=kind, executor=executor, state=st, priority=priority, payload=payload or {},
                progress=prog, job_class=jc, run_after=run_after, parent_id=parent_id, dedupe_key=dedupe_key,
                max_attempts=max_attempts, error_message=message, created_at=db.utcnow())

    def run(c) -> str:
        if dedupe_key:
            got = c.execute(select(db.jobs.c.id).where(db.jobs.c.dedupe_key == dedupe_key)).first()
            if got:
                return got[0]
        if user_id:
            db.upsert(c, db.job_turns, {"user_id": user_id, "job_class": jc, "last_served_at": EPOCH},
                      do_nothing=True)
        jid = db.new_id("job")
        try:
            with c.begin_nested():
                c.execute(db.jobs.insert().values(id=jid, **vals))
        except IntegrityError:
            got = c.execute(select(db.jobs.c.id).where(db.jobs.c.dedupe_key == dedupe_key)).first()
            if got:
                return got[0]
            raise
        return jid

    if conn is not None:
        return run(conn)
    with db.tx() as c:
        return run(c)


# ============================================================================================== leasing

def _ts(v):
    return literal(v, db.UTCDateTime())


def lease_server(job_class: str, owner: str, *, lease_s: int | None = None):
    """Atomically lease the next due server job of this class, round-robin across members (3.13.2). Row|None."""
    j, t, mb = db.jobs, db.job_turns, db.member_budget
    now = db.utcnow()
    today = now.strftime("%Y-%m-%d")
    sub = (select(j.c.id)
           .select_from(j.outerjoin(t, (t.c.user_id == j.c.user_id) & (t.c.job_class == j.c.job_class)))
           .where(j.c.state == "queued", j.c.executor == "server", j.c.job_class == job_class,
                  or_(j.c.run_after.is_(None), j.c.run_after <= now)))
    if job_class == "media":
        sub = sub.where(j.c.user_id.in_(select(mb.c.user_id).where(mb.c.day == today, mb.c.media_left > 0)))
    sub = sub.order_by(t.c.last_served_at.asc().nulls_first(), j.c.priority.asc(), j.c.created_at.asc()).limit(1)
    if not db.is_sqlite():
        sub = sub.with_for_update(skip_locked=True, of=j)
    until = now + timedelta(seconds=lease_s or LEASE_S.get(job_class, 600))
    stmt = (update(j).where(j.c.id == sub.scalar_subquery(), j.c.state == "queued")
            .values(state="running", lease_owner=owner, lease_until=until,
                    started_at=func.coalesce(j.c.started_at, _ts(now)), attempts=j.c.attempts + 1)
            .returning(*j.c))
    with db.tx() as conn:
        row = conn.execute(stmt).first()
        if row is None:
            return None
        if row.user_id:
            db.upsert(conn, t, {"user_id": row.user_id, "job_class": job_class, "last_served_at": now},
                      update={"last_served_at": now})
            if job_class == "media":
                conn.execute(mb.update().where(mb.c.user_id == row.user_id, mb.c.day == today)
                             .values(media_left=mb.c.media_left - 1, media_used=mb.c.media_used + 1))
        return row


class _Undo(Exception):
    pass


def lease_external(user_id: str, executor: str, kinds: Iterable[str], owner: str, lease_s: int, *,
                   prepare: Callable | None = None):
    """Lease the next waiting job for a device / the member's Claude / the desktop app (filtered by user_id).

    prepare(conn, job_row) runs inside the same BEGIN IMMEDIATE (WP5 forms tag batches here and may update the
    job's payload); return False from it to undo the lease and get None back. Returns the (re-read) Row or None.
    """
    j = db.jobs
    kinds = list(kinds)
    if not kinds:
        return None
    now = db.utcnow()
    sub = (select(j.c.id).where(j.c.user_id == user_id, j.c.executor == executor, j.c.kind.in_(kinds),
                                j.c.state.in_(("waiting", "queued")),
                                or_(j.c.run_after.is_(None), j.c.run_after <= now))
           .order_by(j.c.priority.asc(), j.c.created_at.asc()).limit(1))
    if not db.is_sqlite():
        sub = sub.with_for_update(skip_locked=True)
    stmt = (update(j).where(j.c.id == sub.scalar_subquery(), j.c.state.in_(("waiting", "queued")))
            .values(state="running", lease_owner=owner, lease_until=now + timedelta(seconds=lease_s),
                    started_at=func.coalesce(j.c.started_at, _ts(now)), attempts=j.c.attempts + 1)
            .returning(*j.c))
    try:
        with db.tx() as conn:
            row = conn.execute(stmt).first()
            if row is None:
                return None
            if prepare is not None:
                if prepare(conn, row) is False:
                    raise _Undo()
                row = conn.execute(select(j).where(j.c.id == row.id)).first()
            return row
    except _Undo:
        return None


def requeue_expired(now=None) -> int:
    """Expired leases go back to queued (server) / waiting (external); too many attempts -> failed."""
    j = db.jobs
    now = now or db.utcnow()
    n = 0
    with db.tx() as conn:
        rows = conn.execute(select(j).where(j.c.state == "running", j.c.lease_until.is_not(None),
                                            j.c.lease_until < now)).all()
        for r in rows:
            for hook in _EXPIRE_HOOKS.get(r.kind, []):
                try:
                    hook(conn, r)
                except Exception:  # noqa: BLE001
                    log.exception("lease-expired hook failed for %s", r.id)
            if r.attempts >= r.max_attempts:
                conn.execute(j.update().where(j.c.id == r.id, j.c.state == "running").values(
                    state="failed", error_code="lease_expired", lease_owner=None, lease_until=None,
                    error_message="This step stopped too many times. Press Retry to try again.",
                    finished_at=now, dedupe_done=j.c.dedupe_key, dedupe_key=None))
            else:
                conn.execute(j.update().where(j.c.id == r.id, j.c.state == "running").values(
                    state="queued" if r.executor == "server" else "waiting", lease_owner=None, lease_until=None))
            n += 1
    return n


# ============================================================================================== finishing

def _owned(q, owner: str | None):
    return q.where(db.jobs.c.lease_owner == owner) if owner else q


def complete(job_id: str, result: dict | None = None, *, owner: str | None = None, conn=None) -> bool:
    """running/waiting -> done. With owner, only if this owner still holds the lease. Clears dedupe_key."""
    j = db.jobs
    q = _owned(j.update().where(j.c.id == job_id, j.c.state.in_(db.ACTIVE_JOB_STATES)), owner).values(
        state="done", result=result or {}, finished_at=db.utcnow(), lease_owner=None, lease_until=None,
        error_code=None, error_message=None, dedupe_done=j.c.dedupe_key, dedupe_key=None)
    if conn is not None:
        return conn.execute(q).rowcount > 0
    with db.tx() as c:
        return c.execute(q).rowcount > 0


def fail(job_id: str, code: str, message: str, *, pause: bool = False, run_after=None, owner: str | None = None,
         conn=None) -> bool:
    """pause=True -> paused (member must act). run_after -> retry later if attempts remain. Else failed."""
    j = db.jobs

    def run(c) -> bool:
        row = c.execute(select(j).where(j.c.id == job_id)).first()
        if row is None or row.state not in db.ACTIVE_JOB_STATES or (owner and row.lease_owner != owner):
            return False
        base = _owned(j.update().where(j.c.id == job_id), owner)
        if pause:
            c.execute(base.values(state="paused", error_code=code, error_message=message, lease_owner=None,
                                  lease_until=None))
        elif run_after is not None and row.attempts < row.max_attempts:
            c.execute(base.values(state="queued" if row.executor == "server" else "waiting", run_after=run_after,
                                  error_code=code, error_message=None, lease_owner=None, lease_until=None))
        else:
            c.execute(base.values(state="failed", error_code=code, error_message=message, lease_owner=None,
                                  lease_until=None, finished_at=db.utcnow(), dedupe_done=j.c.dedupe_key,
                                  dedupe_key=None))
        return True

    if conn is not None:
        return run(conn)
    with db.tx() as c:
        return run(c)


def cancel(job_id: str, user_id: str | None = None):
    """Cancel an active job (member's own when user_id is given). Returns the row, or None if not found."""
    j = db.jobs
    with db.tx() as c:
        q = select(j).where(j.c.id == job_id)
        if user_id:
            q = q.where(j.c.user_id == user_id)
        row = c.execute(q).first()
        if row is None:
            return None
        if row.state in db.ACTIVE_JOB_STATES:
            c.execute(j.update().where(j.c.id == job_id).values(
                state="canceled", finished_at=db.utcnow(), lease_owner=None, lease_until=None,
                dedupe_done=j.c.dedupe_key, dedupe_key=None))
        return c.execute(select(j).where(j.c.id == job_id)).first()


def retry(job_id: str, user_id: str | None = None):
    """failed/paused/canceled -> queued (or waiting), attempts reset. Returns the row or None."""
    j = db.jobs
    with db.tx() as c:
        q = select(j).where(j.c.id == job_id)
        if user_id:
            q = q.where(j.c.user_id == user_id)
        row = c.execute(q).first()
        if row is None:
            return None
        if row.state in ("failed", "paused", "canceled"):
            key = row.dedupe_key or row.dedupe_done
            if key and c.execute(select(j.c.id).where(j.c.dedupe_key == key, j.c.id != job_id)).first():
                key = None
            c.execute(j.update().where(j.c.id == job_id).values(
                state="queued" if row.executor == "server" else "waiting", attempts=0, error_code=None,
                error_message=None, run_after=None, finished_at=None, lease_owner=None, lease_until=None,
                dedupe_key=key))
        return c.execute(select(j).where(j.c.id == job_id)).first()


def get(job_id: str, user_id: str | None = None):
    j = db.jobs
    with db.tx(immediate=False) as c:
        q = select(j).where(j.c.id == job_id)
        if user_id:
            q = q.where(j.c.user_id == user_id)
        return c.execute(q).first()


def set_progress(job_id: str, done: int, total: int, message: str = "", *, owner: str | None = None) -> str | None:
    """Write progress; returns the job's current state (e.g. 'canceled') or None if the job is gone."""
    j = db.jobs
    with db.tx() as c:
        c.execute(_owned(j.update().where(j.c.id == job_id), owner).values(
            progress={"done": int(done), "total": int(total), "message": str(message)[:300]}))
        st = c.execute(select(j.c.state).where(j.c.id == job_id)).first()
    return st[0] if st else None


def renew(job_id: str, owner: str, lease_s: int) -> bool:
    j = db.jobs
    with db.tx() as c:
        return c.execute(j.update().where(j.c.id == job_id, j.c.lease_owner == owner, j.c.state == "running")
                         .values(lease_until=db.utcnow() + timedelta(seconds=lease_s))).rowcount > 0


class JobCtx:
    """Handed to every handler: progress, cancel check and lease renewal."""

    def __init__(self, job, owner: str, lease_s: int):
        self.job = job
        self.owner = owner
        self.lease_s = lease_s
        self._last = 0.0
        self._state: str | None = "running"

    def progress(self, done: int, total: int, message: str = "") -> None:
        now = time.monotonic()
        if now - self._last < 1.0 and done < total:
            return
        self._last = now
        self._state = set_progress(self.job.id, done, total, message, owner=self.owner)

    def cancelled(self) -> bool:
        row = get(self.job.id)
        self._state = row.state if row else None
        return row is None or row.state != "running" or row.lease_owner != self.owner

    def renew(self) -> None:
        renew(self.job.id, self.owner, self.lease_s)


# ============================================================================================== running

_running: dict[str, tuple[str, int]] = {}  # job_id -> (owner, lease_s), for the lease renewer
_running_lock = threading.Lock()


def run_job(job, owner: str) -> None:
    """Run one leased job through its handler and record the outcome."""
    h = HANDLERS.get(job.kind)
    lease_s = LEASE_S.get(job.job_class, 600)
    if h is None:
        fail(job.id, "no_handler", GENERIC_FAIL, owner=owner)
        log.error("no handler for job kind %s (%s)", job.kind, job.id)
        return
    ctx = JobCtx(job, owner, lease_s)
    with _running_lock:
        _running[job.id] = (owner, lease_s)
    t0 = time.monotonic()
    try:
        result = h.fn(job, ctx)
        complete(job.id, result if isinstance(result, dict) else {}, owner=owner)
    except Retry as e:
        fail(job.id, "retry", e.message or GENERIC_FAIL, run_after=db.utcnow() + timedelta(seconds=e.delay_s),
             owner=owner)
    except Pause as e:
        fail(job.id, e.code, e.message, pause=True, owner=owner)
    except Fail as e:
        fail(job.id, e.code, e.message, owner=owner)
    except Exception:  # noqa: BLE001
        log.error("job %s (%s) crashed: %s", job.id, job.kind, traceback.format_exc(limit=8))
        delay = min(3600, 30 * 2 ** max(0, job.attempts - 1))
        fail(job.id, "error", GENERIC_FAIL, run_after=db.utcnow() + timedelta(seconds=delay), owner=owner)
    finally:
        with _running_lock:
            _running.pop(job.id, None)
        log.info("job %s %s finished in %.2fs", job.id, job.kind, time.monotonic() - t0)


def run_once(job_class: str, owner: str = "test:0") -> Any:
    """Lease and run one due server job of this class in the calling thread. Returns its final row or None."""
    job = lease_server(job_class, owner)
    if job is None:
        return None
    run_job(job, owner)
    return get(job.id)


def drain(classes: Iterable[str] = POOLS, *, max_jobs: int = 200, recompute_budget: bool = True) -> list:
    """Run every due server job (tests and the dev server). Returns the finished rows in run order."""
    load_handlers()
    if recompute_budget:
        from . import budget

        budget.recompute_shares()
    done = []
    progressed = True
    while progressed and len(done) < max_jobs:
        progressed = False
        for jc in classes:
            row = run_once(jc)
            if row is not None:
                done.append(row)
                progressed = True
                if jc == "media" and recompute_budget:
                    from . import budget

                    budget.recompute_shares()
    return done


def to_api(row, *, include_result: bool = False, with_eta: bool = True) -> dict:
    """The Job JSON of 4.1."""
    out = {"id": row.id, "kind": row.kind, "executor": row.executor, "state": row.state,
           "progress": row.progress or {"done": 0, "total": 0, "message": ""}, "eta": None,
           "run_after": db.iso(row.run_after), "error_message": row.error_message,
           "created_at": db.iso(row.created_at), "finished_at": db.iso(row.finished_at)}
    if with_eta and row.user_id and row.state in ("queued", "waiting"):
        try:
            from . import budget

            out["eta"] = budget.eta(row.user_id, job=row)
        except Exception:  # noqa: BLE001
            log.exception("eta failed")
    if include_result:
        out["result"] = row.result
    return out


# ============================================================================================== system jobs

@handler("sweep_storage", job_class="light")
def _sweep(job, ctx):
    from . import storage

    return storage.sweep_storage()


@handler("backup", job_class="light")
def _backup(job, ctx):
    from . import storage

    return storage.backup()


@handler("expire_members", job_class="light")
def _expire(job, ctx):
    from . import api_admin

    return api_admin.expire_members()


@handler("rotate_code", job_class="light")
def _rotate(job, ctx):
    from . import api_admin

    return api_admin.rotate_code_if_due()


def schedule_daily() -> list[str]:
    """Enqueue each daily system job once per UTC day across all workers (atomic counter)."""
    today = db.utcnow().strftime("%Y-%m-%d")
    out = []
    for kind in DAILY_KINDS:
        c = db.bump(db.usage_counters, {"scope": "global", "key": f"daily:{kind}", "window": today}, count=1)
        if c.get("count") == 1:
            out.append(enqueue(None, kind, {}, priority=90))
    return out


def worker_heartbeat() -> dict | None:
    return db.get_setting("internal.worker_heartbeat")


def _owner(pool: str, n: int) -> str:
    return f"worker:{socket.gethostname()}:{os.getpid()}:{pool}:{n}"


def run_worker(stop: threading.Event | None = None, *, pools: dict[str, int] | None = None,
               poll_s: float = 1.0, migrate: bool = True) -> None:
    """RV_ROLE=worker: pools media/ai/light + maintenance (lease expiry, budget, heartbeat, daily jobs)."""
    from . import budget
    from .settings import get_settings

    s = get_settings()
    stop = stop or threading.Event()
    if migrate:
        db.migrate()
    load_handlers()
    sizes = pools or {"media": s.whisper_concurrency, "ai": s.ai_concurrency, "light": s.light_concurrency}
    threads: list[threading.Thread] = []

    def pool_loop(pool: str, n: int) -> None:
        owner = _owner(pool, n)
        idle = 0
        while not stop.is_set():
            try:
                job = lease_server(pool, owner)
            except Exception:  # noqa: BLE001
                log.exception("lease failed in %s", owner)
                job = None
            if job is None:
                idle += 1
                if pool == "media" and idle % 30 == 0:
                    try:
                        budget.recompute_shares()
                    except Exception:  # noqa: BLE001
                        log.exception("budget recompute failed")
                stop.wait(poll_s + random.random() * poll_s * 0.5)
                continue
            idle = 0
            run_job(job, owner)

    def maintenance() -> None:
        last = {"expire": 0.0, "budget": 0.0, "beat": 0.0, "renew": 0.0, "daily": 0.0}
        while not stop.is_set():
            now = time.monotonic()
            try:
                if now - last["expire"] >= 15:
                    requeue_expired()
                    last["expire"] = now
                if now - last["budget"] >= 600 or (now - last["budget"] >= 60 and budget.has_queued_media()):
                    budget.recompute_shares()
                    last["budget"] = now
                if now - last["renew"] >= 60:
                    with _running_lock:
                        items = list(_running.items())
                    for jid, (owner, lease_s) in items:
                        renew(jid, owner, lease_s)
                    last["renew"] = now
                if now - last["beat"] >= 30:
                    db.set_setting("internal.worker_heartbeat", {"at": db.iso(db.utcnow()),
                                                                 "host": socket.gethostname(), "pid": os.getpid(),
                                                                 "pools": sizes})
                    last["beat"] = now
                if now - last["daily"] >= 300:
                    schedule_daily()
                    last["daily"] = now
            except Exception:  # noqa: BLE001
                log.exception("worker maintenance failed")
            stop.wait(1.0)

    for pool, size in sizes.items():
        for n in range(max(0, int(size))):
            th = threading.Thread(target=pool_loop, args=(pool, n), name=f"rv-{pool}-{n}", daemon=True)
            th.start()
            threads.append(th)
    mt = threading.Thread(target=maintenance, name="rv-maint", daemon=True)
    mt.start()
    threads.append(mt)
    log.info("worker started: %s", sizes)
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        stop.set()
        for th in threads:
            th.join(timeout=10)
        log.info("worker stopped")


def main() -> None:
    from .app import setup_logging, prepare_runtime

    setup_logging()
    prepare_runtime()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run_worker(stop, migrate=False)


if __name__ == "__main__":
    main()
