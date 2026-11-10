"""WP1 second pass: client-keyed rate limits (11.1, 11.6), budget fair share (3.13.3), storage restore, supervisor
health, bench (12.5)."""
import json
import shutil
import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy import select
from starlette.requests import Request

from reelvault.server import api_auth, budget, db, jobs, ratelimit, security, settings, storage, supervise


def fake_request(peer: str, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234),
                    "query_string": b""})


# ============================================================================================== client keys

def test_client_key_groups_ipv6_by_64_and_unwraps_mapped_ipv4(rv_env):
    assert ratelimit.norm_ip("203.0.113.7") == "ip:203.0.113.7"
    assert ratelimit.norm_ip("::ffff:203.0.113.7") == "ip:203.0.113.7"
    a = ratelimit.norm_ip("2001:db8:1:2:aaaa::1")
    b = ratelimit.norm_ip("2001:db8:1:2:ffff:1:2:3")
    assert a == b == "ip6:2001:db8:1:2::/64"
    assert ratelimit.norm_ip("2001:db8:1:3::1") != a
    assert ratelimit.norm_ip("not-an-ip").startswith("raw:")
    assert ratelimit.client_key(fake_request("198.51.100.4")) == "ip:198.51.100.4"


def test_client_key_trusts_forwarded_for_only_from_trusted_proxies(rv_env, monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1,172.16.0.0/12")
    settings.reload()
    assert ratelimit.client_key(fake_request("172.29.57.2", "203.0.113.9")) == "ip:203.0.113.9"
    assert ratelimit.client_key(fake_request("203.0.113.50", "1.2.3.4")) == "ip:203.0.113.50"  # forged, ignored


def test_composite_keys_and_anthropic_range():
    assert ratelimit.key("1.2.3.4", "https://claude.ai/cb", "Claude") == "1.2.3.4|https://claude.ai/cb|Claude"
    long = ratelimit.key("x" * 300)
    assert long.startswith("h:") and len(long) == 66
    assert ratelimit.ip_limit(30, "160.79.104.10") == 300
    assert ratelimit.ip_limit(30, "160.79.111.255") == 300
    assert ratelimit.ip_limit(30, "160.79.112.0") == 30
    assert ratelimit.ip_limit(30, "garbage") == 30


def test_hit_all_counts_every_counter_then_raises(rv_env):
    req = fake_request("198.51.100.9")
    ratelimit.hit_all(req, [("a", None, 1), ("b", "fixed", 5)], "hour")
    with pytest.raises(ratelimit.TooMany):
        ratelimit.hit_all(req, [("a", None, 1), ("b", "fixed", 5)], "hour")
    assert ratelimit.count("a", "ip:198.51.100.9", "hour") == 2
    assert ratelimit.count("b", "fixed", "hour") == 2  # still counted although "a" was over


def test_login_limit_per_client_counts_every_email_tried(client, member):
    for i in range(20):
        r = client.post("/api/auth/login", json={"email": f"nobody{i}@example.com", "password": "wrong-password-1"})
        assert r.status_code in (401, 429)
    # TestClient's peer is "testclient": all 20 failures above landed on one client key, so the next is refused
    r = client.post("/api/auth/login", json={"email": member.email, "password": member.password})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_one_members_lockout_does_not_lock_out_others_behind_the_same_ip(client, make_user):
    a, b = make_user("a@example.com"), make_user("b@example.com")
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": a.email, "password": "wrong-password-1"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": a.email, "password": a.password}).status_code == 429
    assert client.post("/api/auth/login", json={"email": b.email, "password": b.password}).status_code == 200


def test_device_calls_are_limited_per_device(rv_env, member, make_device, monkeypatch):
    monkeypatch.setattr(security, "DEVICE_PER_MINUTE", 3)
    did, hdrs = make_device(member)
    req = Request({"type": "http", "method": "GET", "path": "/api/ext/v2/hello", "query_string": b"",
                   "headers": [(b"authorization", hdrs["Authorization"].encode())], "client": ("1.2.3.4", 1)})
    for _ in range(3):
        assert security.current_device(req, "extension").id == did
    with pytest.raises(ratelimit.TooMany):
        security.current_device(req, "extension")
    did2, hdrs2 = make_device(member, kind="desktop")
    req2 = Request({"type": "http", "method": "GET", "path": "/api/worker/lease", "query_string": b"",
                    "headers": [(b"authorization", hdrs2["Authorization"].encode())], "client": ("1.2.3.4", 1)})
    assert security.current_device(req2, "desktop").id == did2  # another device on the same IP is fine


def test_common_password_list_is_the_full_list():
    words = security._common_passwords()  # noqa: SLF001
    assert len(words) >= 10_000
    assert security.password_problem("1234567890") is not None
    assert security.password_problem("password12") is not None
    assert security.password_problem("plum-river-velvet-42") is None


# ============================================================================================== budget

def _media_job(uid, state="queued"):
    jid = jobs.enqueue(uid, "process_reel", {"reel_id": db.new_id("reel")})
    if state != "queued":
        with db.tx() as conn:
            conn.execute(db.jobs.update().where(db.jobs.c.id == jid).values(state=state))
    return jid


def test_running_jobs_are_not_counted_twice_in_the_share(rv_env, make_user, monkeypatch):
    monkeypatch.setenv("SERVER_MEDIA_PER_DAY", "100")
    settings.reload()
    u = make_user()
    for _ in range(3):
        _media_job(u.id)
    assert budget.recompute_shares()[u.id] == 3
    job = jobs.lease_server("media", "test:1")      # 1 used, 2 queued, 1 running
    assert job is not None
    shares = budget.recompute_shares()
    assert shares[u.id] == 3                         # 2 queued + 1 used (not 4)
    with db.tx(immediate=False) as conn:
        row = conn.execute(select(db.member_budget).where(db.member_budget.c.user_id == u.id)).first()
    assert (row.media_used, row.media_left) == (1, 2)


def test_has_queued_media_and_eta(rv_env, make_user):
    u = make_user()
    assert budget.has_queued_media() is False
    assert budget.eta(u.id) is None
    for _ in range(4):
        _media_job(u.id)
    assert budget.has_queued_media() is True
    budget.recompute_shares()
    e = budget.eta(u.id)
    assert e["text"].startswith("Ready in about") and e["at"].endswith("Z")


def test_water_fill_is_fair_and_exact():
    s = budget.water_fill({"a": 1000, "b": 10, "c": 0, "d": 500}, 600)
    assert s["b"] == 10 and s["c"] == 0 and abs(s["a"] - s["d"]) <= 1 and sum(s.values()) == 600
    assert budget.water_fill({"a": 5}, 0) == {"a": 0}


# ============================================================================================== storage restore

def test_backup_is_one_file_and_restore_round_trips(rv_env, make_user):
    make_user("before@example.com")
    out = storage.backup()
    bfile = storage.backups_dir() / out["db"]
    assert bfile.exists() and not (storage.backups_dir() / (out["db"] + "-wal")).exists()
    make_user("after@example.com")
    db.get_engine().dispose()
    res = storage.restore_db(bfile)
    assert res["previous_kept_as"].startswith("before-restore-")
    db.init()
    with db.tx(immediate=False) as conn:
        emails = {r[0] for r in conn.execute(select(db.users.c.email))}
    assert "before@example.com" in emails and "after@example.com" not in emails
    names = [b["name"] for b in storage.list_backups()]
    assert out["db"] in names and not any(n.endswith(("-wal", "-shm")) for n in names)


def test_restore_refuses_files_that_are_not_backups(rv_env, tmp_path):
    bad = tmp_path / "x.sqlite"
    bad.write_bytes(b"hello, not a database" * 100)
    with pytest.raises(SystemExit):
        storage.restore_db(bad)
    other = tmp_path / "other.sqlite"
    sqlite3.connect(other).execute("create table t (x)").connection.commit()
    with pytest.raises(SystemExit):
        storage.restore_db(other)
    with pytest.raises(SystemExit):
        storage.restore_db(tmp_path / "missing.sqlite")


def test_storage_cli(rv_env, capsys):
    assert storage._main(["backup"]) == 0  # noqa: SLF001
    assert json.loads(capsys.readouterr().out)["db"].startswith("db-")
    assert storage._main(["list"]) == 0  # noqa: SLF001
    assert "db-" in capsys.readouterr().out
    assert storage._main(["nope"]) == 2  # noqa: SLF001


# ============================================================================================== supervisor health

def test_worker_health_follows_the_heartbeat(rv_env, monkeypatch):
    monkeypatch.setenv("RV_ROLE", "worker")
    assert supervise.health() == 1
    db.set_setting("internal.worker_heartbeat", {"at": db.iso(db.utcnow())})
    assert supervise.health() == 0
    db.set_setting("internal.worker_heartbeat", {"at": db.iso(db.utcnow() - timedelta(minutes=10))})
    assert supervise.health() == 1


def test_web_health_needs_an_answer(rv_env, monkeypatch):
    monkeypatch.setenv("RV_ROLE", "web")
    monkeypatch.setenv("PORT", "9139")  # nothing listens there
    assert supervise.health() == 1


# ============================================================================================== bench

def test_bench_suggestions_are_capped_for_the_synthetic_clip():
    from reelvault.server import bench

    m = {"all_lanes": {"per_reel_s": {"priority": 2.0, "sample": 1.0}}, "whisper": True}
    s = bench.suggest(m, cpus=4, lanes=2, clip_s=40, ram_gb=8.0, synthetic=True)
    assert s["suggest"] == {"WHISPER_CONCURRENCY": 2, "SERVER_MEDIA_PER_DAY": 4800}
    real = bench.suggest(m, cpus=4, lanes=2, clip_s=40, ram_gb=8.0, synthetic=False)
    assert real["suggest"]["SERVER_MEDIA_PER_DAY"] == real["reels_per_day_capacity"] // 2 > 4800
    assert real["ram_ok"] is True and "Put these in your .env" in bench.report(real, m)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_bench_quick_run_without_whisper(capsys):
    from reelvault.server import bench

    assert bench.main(["--quick", "--no-whisper", "--json", "--lanes", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["lanes"] == 1 and out["summary"]["seconds_per_priority_reel"] > 0
    steps = out["detail"]["single_lane"]["priority"]["steps"]
    assert {"probe", "cuts", "frames", "audio"} <= set(steps)


def test_api_auth_uses_client_keys_not_raw_ips():
    import inspect

    src = inspect.getsource(api_auth)
    for scope in ("login_ip", "ip_code_fail", "reset_ip", "invite_check_ip", "reset_req_ip"):
        assert f'"{scope}", ip,' not in src, scope


def test_only_reel_processing_spends_the_media_share(rv_env, make_user, monkeypatch):
    """Integration finding: fetch_video (a paced CDN download, or a skip to the extension) must not spend the
    member's daily share, or every reel costs 2 of the 150 and processing stalls after 75 reels."""
    monkeypatch.setenv("SERVER_MEDIA_PER_DAY", "100")
    settings.reload()
    u = make_user()
    fetch = jobs.enqueue(u.id, "fetch_video", {"reel_id": db.new_id("reel")})
    proc = _media_job(u.id)
    budget.recompute_shares()
    with db.tx() as conn:   # share used up
        conn.execute(db.member_budget.update().where(db.member_budget.c.user_id == u.id).values(media_left=0))
    got = jobs.lease_server("media", "test:1")
    assert got is not None and got.id == fetch          # the download still runs
    assert jobs.lease_server("media", "test:1") is None  # the processing waits for tomorrow's share
    with db.tx(immediate=False) as conn:
        row = conn.execute(select(db.member_budget).where(db.member_budget.c.user_id == u.id)).first()
    assert row.media_used == 0
    with db.tx() as conn:
        conn.execute(db.member_budget.update().where(db.member_budget.c.user_id == u.id).values(media_left=1))
    assert jobs.lease_server("media", "test:1").id == proc


def test_eta_is_not_pushed_to_tomorrow_by_a_stale_share(rv_env, make_user):
    """Integration finding: jobs queued after the last share recompute made the ETA jump to 'tomorrow' (19 h)."""
    u = make_user()
    for _ in range(3):
        _media_job(u.id)
    budget.recompute_shares()          # share = 3
    for _ in range(10):                # 10 more arrive before the next recompute
        _media_job(u.id)
    e = budget.eta(u.id)
    assert "minutes" in e["text"] or e["text"] == "Ready in about 1 hour", e


def test_missing_share_is_seen_right_away(rv_env, make_user):
    u = make_user()
    assert budget.missing_share() is False
    _media_job(u.id)
    assert budget.missing_share() is True    # queued processing, no share row yet: the worker recomputes in seconds
    budget.recompute_shares()
    assert budget.missing_share() is False


def test_used_up_share_under_the_cap_is_recomputed_soon(rv_env, make_user, monkeypatch):
    monkeypatch.setenv("SERVER_MEDIA_PER_DAY", "100")
    settings.reload()
    u = make_user()
    _media_job(u.id)
    budget.recompute_shares()
    assert jobs.lease_server("media", "t") is not None     # share 1, now used up
    assert budget.missing_share() is False                  # nothing waits
    _media_job(u.id)                                        # new work after the recompute
    assert budget.missing_share() is True
    budget.recompute_shares()
    assert budget.missing_share() is False and jobs.lease_server("media", "t") is not None


def test_waiting_for_claude_line_names_the_output(rv_env, make_user):
    """Integration finding: an output waiting for Claude said 'say: Analyze my new ReelVault reels'."""
    u = make_user()
    jid = jobs.enqueue(u.id, "make_output", {"output_id": "out_abc"}, executor="mcp")
    e = budget.eta(u.id, job=jobs.get(jid))
    assert e["text"] == "Waiting for Claude. Open Claude and say: Write my ReelVault output out_abc"
    t = jobs.enqueue(u.id, "tag_reels", {}, executor="mcp")
    assert "Analyze my new ReelVault reels" in budget.eta(u.id, job=jobs.get(t))["text"]
