"""Job queue, leases, fairness, budget, counters (DESIGN-SAAS.md 3.13, 12.1/12.2 test_jobs.py)."""
import re
import subprocess
import sys
import textwrap
import threading
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import select

from reelvault.server import budget, db, jobs


@pytest.fixture
def score_handler(monkeypatch):
    """Register a temporary handler for 'score_account' whose behaviour each test sets."""
    box = {"fn": lambda job, ctx: {"ok": True}}
    monkeypatch.setitem(jobs.HANDLERS, "score_account",
                        jobs.Handler("score_account", "light", lambda job, ctx: box["fn"](job, ctx)))
    return box


def state(jid):
    return jobs.get(jid)


def test_enqueue_defaults_and_dedupe_only_blocks_active(rv_env, make_user):
    u = make_user()
    a = jobs.enqueue(u.id, "score_account", {"account_id": "acc_1"}, dedupe_key="score:acc_1")
    assert jobs.enqueue(u.id, "score_account", {"account_id": "acc_1"}, dedupe_key="score:acc_1") == a
    row = state(a)
    assert (row.state, row.executor, row.job_class, row.attempts) == ("queued", "server", "light", 0)
    ext = jobs.enqueue(u.id, "ig_pull", {"account_id": "acc_1"})
    r = state(ext)
    assert (r.state, r.job_class, r.progress["message"]) == ("waiting", "external",
                                                            "Waiting for your Chrome extension")
    mcp = jobs.enqueue(u.id, "tag_reels", {}, executor="mcp")
    assert state(mcp).job_class == "external"
    paused = jobs.enqueue(u.id, "tag_reels", {}, state="paused",
                          message="Choose how ReelVault uses AI in Settings > AI.")
    assert state(paused).error_message.startswith("Choose how")
    assert jobs.complete(a, {"scored": 1})
    done = state(a)
    assert done.dedupe_key is None and done.dedupe_done == "score:acc_1"
    b = jobs.enqueue(u.id, "score_account", {"account_id": "acc_1"}, dedupe_key="score:acc_1")
    assert b != a
    with pytest.raises(ValueError):
        jobs.enqueue(u.id, "not_a_kind", {})


def test_enqueue_inside_an_open_transaction(rv_env, make_user):
    u = make_user()
    with db.tx() as conn:
        jid = jobs.enqueue(u.id, "score_account", {}, conn=conn)
        assert conn.execute(select(db.jobs.c.id).where(db.jobs.c.id == jid)).first()
    assert state(jid) is not None


def test_handler_outcomes_complete_retry_pause_fail(rv_env, make_user, score_handler):
    u = make_user()

    score_handler["fn"] = lambda job, ctx: (ctx.progress(1, 1, "Scored"), {"scored": 3})[1]
    jid = jobs.enqueue(u.id, "score_account", {})
    row = jobs.run_once("light")
    assert row.id == jid and row.state == "done" and row.result == {"scored": 3}
    assert row.progress["message"] == "Scored"

    def boom(job, ctx):
        raise RuntimeError("secret internals")

    score_handler["fn"] = boom
    jid = jobs.enqueue(u.id, "score_account", {})
    row = jobs.run_once("light")
    assert row.state == "queued" and row.attempts == 1 and row.run_after > db.utcnow()
    with db.tx() as conn:  # make it due again twice; third crash fails it with a plain message
        conn.execute(db.jobs.update().where(db.jobs.c.id == jid).values(run_after=None))
    jobs.run_once("light")
    with db.tx() as conn:
        conn.execute(db.jobs.update().where(db.jobs.c.id == jid).values(run_after=None))
    row = jobs.run_once("light")
    assert row.state == "failed" and row.attempts == 3
    assert "secret" not in (row.error_message or "") and row.error_message.startswith("Something went wrong")

    def pause(job, ctx):
        raise jobs.Pause("Your AI key stopped working. Paste it again in Settings > AI.", code="bad_key")

    score_handler["fn"] = pause
    jid = jobs.enqueue(u.id, "score_account", {})
    row = jobs.run_once("light")
    assert (row.state, row.error_code) == ("paused", "bad_key")

    def stop(job, ctx):
        raise jobs.Fail("That account is private. We never read private accounts.")

    score_handler["fn"] = stop
    jobs.enqueue(u.id, "score_account", {})
    assert jobs.run_once("light").state == "failed"

    def later(job, ctx):
        raise jobs.Retry(120)

    score_handler["fn"] = later
    jobs.enqueue(u.id, "score_account", {})
    row = jobs.run_once("light")
    assert row.state == "queued" and row.run_after > db.utcnow() + timedelta(seconds=100)
    assert jobs.run_once("light") is None  # not due yet


def test_unknown_kind_without_handler_fails_plainly(rv_env, make_user, monkeypatch):
    u = make_user()
    monkeypatch.delitem(jobs.HANDLERS, "pick_sample", raising=False)
    jobs.enqueue(u.id, "pick_sample", {})
    row = jobs.run_once("light")
    assert row.state == "failed" and row.error_code == "no_handler"


def test_round_robin_across_members_with_uneven_backlogs(rv_env, make_user):
    a, b, c = make_user(), make_user(), make_user()
    for _ in range(10):
        jobs.enqueue(a.id, "score_account", {})
    for _ in range(3):
        jobs.enqueue(b.id, "score_account", {})
    jobs.enqueue(c.id, "score_account", {})
    order = []
    while True:
        row = jobs.lease_server("light", "test:0")
        if row is None:
            break
        order.append(row.user_id)
        jobs.complete(row.id, {})
    assert len(order) == 14
    assert set(order[:3]) == {a.id, b.id, c.id}
    assert order[3:7] == [a.id, b.id, a.id, b.id]
    assert order[7:] == [a.id] * 7


def test_priority_within_a_member(rv_env, make_user):
    u = make_user()
    low = jobs.enqueue(u.id, "score_account", {}, priority=90)
    high = jobs.enqueue(u.id, "score_account", {}, priority=10)
    assert jobs.lease_server("light", "t").id == high
    assert jobs.lease_server("light", "t").id == low


def test_lease_expiry_requeues_and_runs_hooks(rv_env, make_user, monkeypatch):
    u = make_user()
    seen = []
    monkeypatch.setitem(jobs._EXPIRE_HOOKS, "tag_reels", [lambda conn, job: seen.append(job.id)])
    jid = jobs.enqueue(u.id, "tag_reels", {}, executor="mcp")
    row = jobs.lease_external(u.id, "mcp", ["tag_reels"], "mcp:test", 60)
    assert row.id == jid and row.state == "running" and row.attempts == 1
    assert jobs.requeue_expired() == 0
    assert jobs.requeue_expired(now=db.utcnow() + timedelta(seconds=61)) == 1
    assert state(jid).state == "waiting" and seen == [jid]
    for _ in range(2):
        jobs.lease_external(u.id, "mcp", ["tag_reels"], "mcp:test", 60)
        jobs.requeue_expired(now=db.utcnow() + timedelta(seconds=61))
    assert state(jid).state == "failed"


def test_lease_external_filters_by_member_kind_and_prepare(rv_env, make_user):
    a, b = make_user(), make_user()
    ja = jobs.enqueue(a.id, "ig_pull", {"n": 1})
    jobs.enqueue(b.id, "ig_pull", {"n": 2})
    assert jobs.lease_external(a.id, "extension", ["ig_videos"], "dev_a", 300) is None
    assert jobs.lease_external(a.id, "desktop", ["ig_pull"], "dev_a", 300) is None

    def undo(conn, job):
        return False

    assert jobs.lease_external(a.id, "extension", ["ig_pull"], "dev_a", 300, prepare=undo) is None
    assert state(ja).state == "waiting"

    def fill(conn, job):
        conn.execute(db.jobs.update().where(db.jobs.c.id == job.id).values(payload={"n": 1, "reel_ids": ["r1"]}))

    row = jobs.lease_external(a.id, "extension", ["ig_pull"], "dev_a", 300, prepare=fill)
    assert row.id == ja and row.payload["reel_ids"] == ["r1"] and row.lease_owner == "dev_a"
    assert jobs.lease_external(a.id, "extension", ["ig_pull"], "dev_a", 300) is None
    assert not jobs.complete(ja, {}, owner="someone_else")
    assert jobs.complete(ja, {"ok": True}, owner="dev_a")


def test_cancel_and_retry(rv_env, make_user):
    u = make_user()
    jid = jobs.enqueue(u.id, "score_account", {}, dedupe_key="k1")
    assert jobs.cancel(jid, "usr_someoneelse0000") is None
    assert jobs.cancel(jid, u.id).state == "canceled"
    row = jobs.retry(jid, u.id)
    assert row.state == "queued" and row.attempts == 0 and row.dedupe_key == "k1"


def test_media_lease_respects_budget_and_water_filling(rv_env, make_user, monkeypatch):
    from reelvault.server import settings

    assert budget.water_fill({"a": 100, "b": 10, "c": 1000}, 300) == {"a": 100, "b": 10, "c": 190}
    assert budget.water_fill({"a": 5, "b": 5}, 4) == {"a": 2, "b": 2}
    assert sum(budget.water_fill({"a": 7, "b": 9, "c": 11}, 20).values()) == 20

    monkeypatch.setenv("SERVER_MEDIA_PER_DAY", "4")
    settings.reload()
    a, b = make_user(), make_user()
    for _ in range(5):
        jobs.enqueue(a.id, "process_reel", {})
    jobs.enqueue(b.id, "process_reel", {})
    assert jobs.lease_server("media", "t") is None  # no share computed yet today
    shares = budget.recompute_shares()
    assert shares == {a.id: 3, b.id: 1}
    got = Counter()
    while (row := jobs.lease_server("media", "t")) is not None:
        got[row.user_id] += 1
        jobs.complete(row.id, {})
    assert got == {a.id: 3, b.id: 1}
    assert budget.member_share(a.id) == 3
    eta = jobs.to_api(jobs.get(jobs.enqueue(a.id, "process_reel", {})))["eta"]
    assert eta["text"].startswith("Ready in about")


def test_to_api_shape(rv_env, make_user):
    u = make_user()
    row = jobs.get(jobs.enqueue(u.id, "ig_pull", {}, run_after=db.utcnow() + timedelta(days=1)))
    out = jobs.to_api(row, include_result=True)
    assert set(out) == {"id", "kind", "executor", "state", "progress", "eta", "run_after", "error_message",
                        "created_at", "finished_at", "result"}
    assert out["run_after"].endswith("Z") and out["created_at"].endswith("Z")
    mcp = jobs.to_api(jobs.get(jobs.enqueue(u.id, "tag_reels", {}, executor="mcp")))
    assert "Claude" in mcp["eta"]["text"]


def test_drain_runs_chains(rv_env, make_user, score_handler, monkeypatch):
    u = make_user()

    def first(job, ctx):
        jobs.enqueue(job.user_id, "refresh_insights", {}, parent_id=job.id)
        return {"ok": 1}

    score_handler["fn"] = first
    monkeypatch.setitem(jobs.HANDLERS, "refresh_insights",
                        jobs.Handler("refresh_insights", "light", lambda job, ctx: {"snapshot_id": "snap_x"}))
    jobs.enqueue(u.id, "score_account", {})
    done = jobs.drain()
    assert [r.kind for r in done] == ["score_account", "refresh_insights"]
    assert all(r.state == "done" for r in done)


def test_daily_jobs_are_scheduled_once_per_day(rv_env):
    first = jobs.schedule_daily()
    assert len(first) == 4 and jobs.schedule_daily() == []
    kinds = {jobs.get(j).kind for j in first}
    assert kinds == {"sweep_storage", "backup", "expire_members", "rotate_code"}
    done = jobs.drain(["light"])
    assert sorted(r.kind for r in done) == sorted(kinds) and all(r.state == "done" for r in done), \
        [(r.kind, r.state, r.error_code) for r in done]
    assert list((rv_env / "backups").glob("db-*.sqlite"))


def test_jobs_api_is_member_scoped(member_client, other_client, member):
    jid = jobs.enqueue(member.id, "score_account", {"account_id": "acc_a"})
    r = member_client.get("/api/jobs?active=1")
    assert [j["id"] for j in r.json()["jobs"]] == [jid]
    assert member_client.get(f"/api/jobs/{jid}").json()["id"] == jid
    assert other_client.get(f"/api/jobs/{jid}").status_code == 404
    assert other_client.post(f"/api/jobs/{jid}/cancel", json={}).status_code == 404
    assert other_client.get("/api/jobs").json() == {"jobs": []}
    assert member_client.post(f"/api/jobs/{jid}/retry", json={}).status_code == 400
    assert member_client.post(f"/api/jobs/{jid}/cancel", json={}).json()["state"] == "canceled"
    assert member_client.post(f"/api/jobs/{jid}/retry", json={}).json()["state"] == "queued"


# ---------------------------------------------------------------------------------- concurrency on one SQLite file

WORKER = textwrap.dedent("""
    import sys
    from reelvault.server import db, jobs
    db.init()
    got = []
    misses = 0
    while misses < 20:
        row = jobs.lease_server("light", "proc:" + sys.argv[1])
        if row is None:
            misses += 1
            continue
        misses = 0
        got.append(row.id)
        jobs.complete(row.id, {})
    for _ in range(int(sys.argv[2])):
        db.bump(db.usage_counters, {"scope": "t", "key": "k", "window": "w"}, count=1)
    print(" ".join(got))
""")


def test_atomic_lease_8_threads_2_processes_no_double_lease_no_lock_errors(rv_env, make_user, tmp_path):
    users = [make_user() for _ in range(3)]
    n_jobs = 240
    with db.tx() as conn:
        for i in range(n_jobs):
            jobs.enqueue(users[i % 3].id, "score_account", {"i": i}, conn=conn)
    script = tmp_path / "lease_worker.py"
    script.write_text(WORKER)
    procs = [subprocess.Popen([sys.executable, str(script), str(n), "50"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for n in range(2)]
    leased: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()

    def thread(n):
        try:
            misses = 0
            while misses < 20:
                row = jobs.lease_server("light", f"thread:{n}")
                if row is None:
                    misses += 1
                    continue
                misses = 0
                with lock:
                    leased.append(row.id)
                jobs.complete(row.id, {})
            for _ in range(50):
                db.bump(db.usage_counters, {"scope": "t", "key": "k", "window": "w"}, count=1)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    ths = [threading.Thread(target=thread, args=(n,)) for n in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err[-2000:]
        assert "database is locked" not in err
        leased.extend(out.split())
    assert not errors, errors
    assert len(leased) == n_jobs and len(set(leased)) == n_jobs
    with db.tx(immediate=False) as conn:
        states = Counter(r[0] for r in conn.execute(select(db.jobs.c.state)))
        bumped = conn.execute(select(db.usage_counters.c.count).where(db.usage_counters.c.scope == "t")).scalar()
    assert states == {"done": n_jobs}
    assert bumped == 8 * 50 + 2 * 50


# ---------------------------------------------------------------------------------- schema (first migration)

def test_migration_matches_the_table_definitions(rv_env):
    """The Alembic head (copied into every test DB) has exactly the tables, columns and CHECKs of db.py."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    with db.get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
        names = set(db.get_engine().dialect.get_table_names(conn)) - {"alembic_version"}
        checks = {c["name"] for c in db.get_engine().dialect.get_check_constraints(conn, "jobs")}
    assert diff == [], diff
    assert names == set(db.metadata.tables) and len(names) == 34
    assert {"ck_jobs_kind", "ck_jobs_state", "ck_jobs_executor", "ck_jobs_job_class"} <= checks


def test_enum_checks_are_enforced(rv_env, make_user):
    import sqlalchemy.exc

    u = make_user()
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with db.tx() as conn:
            conn.execute(db.jobs.insert().values(id=db.new_id("job"), user_id=u.id, kind="score_account",
                                                 executor="server", state="bogus", job_class="light"))


def test_ids_and_times(rv_env):
    i = db.new_id("usr")
    assert re.match(r"^usr_[a-z2-7]{16}$", i)
    assert db.iso(db.utcnow()).endswith("Z")
