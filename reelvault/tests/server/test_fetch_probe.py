"""Fetch engine, server side: "Test my connection", member settings, status, pull list, admin health
(DESIGN-FETCH.md 4.4, 5.1-5.4, 6)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from _fetch_support import (ALL_LO, TIMELINE, Ext, add_account, confirm, device_row, job_row, li_user, lo_user,
                            load_json, log_row, mk, pull_row, set_read_mode, set_version, v1_reel)
from reelvault.server import db, fetch, jobs, jobs_ig
from reelvault.server.testing import BASE_URL

ROWS = load_json("probe_upload.json")["rows"]


@pytest.fixture
def anon(app):
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def ext(anon, member, make_device):
    """A paired extension 2.1.0 that has NOT confirmed an Instagram account (the test must still run)."""
    did, h = make_device(member)
    set_version(did, "2.1.0")
    return Ext(anon, h, did)


def row(strategy, status="ok", code=None, fields=(), handle="instagram"):
    return {"strategy": strategy, "layer": "logged_out" if strategy.startswith("lo_") else "logged_in",
            "handle": handle, "status": status, "code": code, "http": 200, "ms": 100, "items": 1,
            "fields": list(fields)}


# ============================================================================================== the probe

def test_probe_needs_a_paired_and_current_extension(member_client, member, make_device):
    r = member_client.post("/api/fetch/probe", json={})
    assert r.status_code == 409 and r.json() == {"error": "Connect the Chrome extension first.", "code": "busy"}
    assert member_client.get("/api/fetch/probe/latest").status_code == 404
    did, _ = make_device(member)                     # version 2.0.0: it cannot run the test
    r = member_client.post("/api/fetch/probe", json={})
    assert r.status_code == 409 and r.json()["error"] == "Update the Chrome extension first. Then run the test again."
    with db.tx(immediate=False) as conn:
        assert conn.execute(select(db.fetch_probes)).first() is None


def test_probe_end_to_end_with_the_contract_rows(ext, member, member_client):
    aid = add_account(member, status="active")
    r = member_client.post("/api/fetch/probe", json={})
    assert r.status_code == 201, r.text
    start = r.json()
    assert start["state"] == "waiting" and start["message"] == "Waiting for Chrome. The test starts within 30 seconds."
    pid, jid = start["probe_id"], start["job_id"]
    again = member_client.post("/api/fetch/probe", json={})          # an active probe is returned again
    assert again.status_code == 200 and again.json()["probe_id"] == pid and again.json()["job_id"] == jid
    got = member_client.get(f"/api/fetch/probe/{pid}").json()
    assert got["state"] == "waiting" and got["rows"] == [] and got["verdict"] is None
    assert got["handles"] == ["instagram", "scaleit.demo"] and got["progress"] == {"done": 0, "total": 11}

    # the device is signed out of nothing and confirmed nothing: the probe is still handed out, first of all
    jobs_ig.start_pull(member.id, aid, "first")
    lz = ext.lease()
    assert lz["job"]["id"] == jid and lz["job"]["kind"] == "ig_probe"
    assert lz["job"]["payload"] == {"probe_id": pid, "handles": ["instagram", "scaleit.demo"],
                                    "want": ALL_LO + ["li_session", "li_clips"], "read": "auto"}
    assert jobs_ig.payload_problem("ig_probe", lz["job"]["payload"]) is None
    assert ext.ok(f"/jobs/{jid}/progress", {"done": 0, "total": 11})["cancel"] is False

    # one row per call, posted as each finishes
    for i, one in enumerate(ROWS):
        out = ext.ok(f"/jobs/{jid}/probe", {"rows": [one]})
        assert out["ok"] is True and out["rows"] == i + 1
    running = member_client.get(f"/api/fetch/probe/{pid}").json()
    assert running["state"] == "running" and running["progress"] == {"done": 11, "total": 11}
    assert ext.ok(f"/jobs/{jid}/done", {})["ok"] is True

    done = member_client.get(f"/api/fetch/probe/{pid}").json()
    assert done["state"] == "done" and done["finished_at"] and member_client.get("/api/fetch/probe/latest").json() == done
    assert done["verdict"] == {"code": "best", "title": "The safest way works.",
                               "text": "ReelVault reads reels without your Instagram login on this connection.",
                               "next": "You don't need to do anything."}
    first = done["rows"][0]
    assert {k: first[k] for k in ("strategy", "layer", "label", "handle", "status", "status_label", "gives", "note",
                                  "ms")} == {
        "strategy": "lo_profile_page", "layer": "logged_out", "label": "Profile page", "handle": "instagram",
        "status": "ok", "status_label": "Works", "gives": "Followers and the newest posts", "note": None, "ms": 640}
    by = {(r["strategy"], r["handle"]): r for r in done["rows"]}
    assert by[("lo_reels_page", "instagram")]["status_label"] == "Works in part"
    assert by[("lo_reels_page", "instagram")]["note"] == "The page has no data inside."
    assert by[("lo_reels_gql", "instagram")]["status_label"] == "Blocked"
    assert by[("lo_reels_gql", "instagram")]["note"] == "Instagram asks for a login here."
    assert by[("lo_web_profile", "instagram")]["note"] == "Instagram asks ReelVault to wait."
    assert by[("li_session", None)]["layer"] == "logged_in" and by[("li_clips", "scaleit.demo")]["status"] == "ok"
    assert all(r["label"] and r["gives"] and r["status_label"] for r in done["rows"])
    assert jobs.get(jid).state == "done" and jobs.get(jid).result == {"verdict": "best", "rows": 11}

    # the rows count in the health numbers, and the blocks set the same cooldowns a pull would
    with db.tx(immediate=False) as conn:
        h = {r.strategy: (r.ok, r.partial, r.blocked) for r in conn.execute(select(db.fetch_health)).all()}
    assert h["lo_profile_page"] == (2, 0, 0) and h["lo_reels_gql"] == (0, 0, 1) and h["li_clips"] == (1, 0, 0)
    fs = fetch.device_fetch_state(device_row(ext.device_id))
    assert fs["lo_cooldown_reason"] == "rate" and "lo_reels_gql" in fs["strategy_off"]
    st = member_client.get("/api/fetch/status").json()
    assert st["last_probe"]["id"] == pid and st["last_probe"]["verdict"] == {"code": "best",
                                                                            "title": "The safest way works."}
    assert st["layers"][0]["state"] == "cooling" and st["layers"][0]["cooldown_until"]
    # the test made 9 logged-out calls and 2 logged-in ones: they count in the budgets
    with db.tx(immediate=False) as conn:
        counts = {k: jobs_ig._server_count(conn, jobs_ig.ig_scope(member.id), k, "day") for k in ("lo_calls", "ig_calls")}
    assert counts == {"lo_calls": 9, "ig_calls": 11}


def test_probe_day_limit(ext, member, member_client):
    for i in range(3):
        r = member_client.post("/api/fetch/probe", json={})
        assert r.status_code == 201
        jid = r.json()["job_id"]
        assert ext.lease(["ig_probe"])["job"]["id"] == jid
        ext.ok(f"/jobs/{jid}/probe", {"rows": [row("lo_profile_page", fields=["user"])]})
        ext.ok(f"/jobs/{jid}/done", {})
    r = member_client.post("/api/fetch/probe", json={})
    assert r.status_code == 429 and r.json() == {"error": "You ran 3 tests today. Try again tomorrow.",
                                                 "code": "limit_reached"}


@pytest.mark.parametrize("rows,code", [
    ([row("lo_reels_gql", fields=["reel_list", "views"])], "best"),
    ([row("lo_reels_page", fields=["reel_list", "views"]), row("li_clips", "blocked", "signed_out")], "best"),
    ([row("lo_posts_gql", fields=["reel_list"]), row("lo_post_gql", fields=["views", "likes"])], "best"),
    ([row("lo_profile_page", fields=["user", "reel_list"]), row("lo_media_gql", fields=["views"])], "best"),
    ([row("lo_posts_gql", fields=["reel_list"]), row("lo_post_gql", "partial", "no_views", ["likes"])], "partial"),
    # the profile page alone is not "works in part" (review fix): the login reads the reels here
    ([row("lo_profile_page", "partial", "no_data", ["lsd"]), row("li_clips", fields=["views"])], "login_needed"),
    ([row("lo_reels_gql", fields=["reel_list"]), row("lo_post_gql", "blocked", "login_wall")], "partial"),
    ([row("lo_profile_page", "blocked", "login_wall"), row("lo_reels_gql", "skipped", "no_lsd"),
      row("li_clips", fields=["views"])], "login_needed"),
    ([row("lo_profile_page", "blocked", "rate"), row("li_session", "blocked", "signed_out"),
      row("li_clips", "skipped", "signed_out")], "wait"),
    ([row("lo_profile_page", "error", "error")], "nothing"),
    ([row("lo_profile_page", "blocked", "login_wall"), row("li_session", fields=["signed_in"]),
      row("li_clips", "blocked", "challenge")], "nothing"),
])
def test_verdicts(rows, code):
    v = fetch.verdict_of(rows)
    assert v["code"] == code and v["title"] and v["text"] and v["next"]
    assert max(s.count(",") for part in (v["title"], v["text"], v["next"]) for s in part.split(". ")) <= 1


def test_probe_ok_rows_switch_strategies_back_on(ext, member, member_client, admin_client):
    with db.tx() as conn:
        fs = fetch.fetch_state(None)
        fs["strategy_off"] = {"lo_reels_gql": db.iso(db.utcnow() + timedelta(hours=5)),
                              "lo_posts_gql": db.iso(db.utcnow() + timedelta(hours=5))}
        fetch.save_fetch_state(conn, ext.device_id, fs)
        db.set_setting(fetch.KEY_STRATEGY_OFF, {
            "lo_reels_gql": {"until": db.iso(db.utcnow() + timedelta(hours=20)), "reason": "auto"},
            "lo_embed": {"until": None, "reason": "admin"}}, conn=conn)
    jid = member_client.post("/api/fetch/probe", json={}).json()["job_id"]
    lz = ext.lease(["ig_probe"])              # the probe ignores the switch-offs: it is the re-test
    assert lz["job"]["payload"]["want"][:8] == ALL_LO and sorted(lz["lo"]["strategy_off"]) == ["lo_posts_gql", "lo_reels_gql"]
    out = ext.ok(f"/jobs/{jid}/probe", {"rows": [row("lo_reels_gql", fields=["reel_list", "views"]),
                                                 row("lo_embed", fields=["views"]),
                                                 row("lo_posts_gql", "blocked", "changed")]})
    assert list(out["lo"]["strategy_off"]) == ["lo_posts_gql"]          # ok: back on. changed: off for 24 h
    assert fetch.global_off() == {"lo_embed": {"until": None, "reason": "admin"}}   # an admin's switch stays


def test_probe_rows_are_checked(ext, member, member_client):
    jid = member_client.post("/api/fetch/probe", json={}).json()["job_id"]
    ext.lease(["ig_probe"])
    for bad in ({}, {"rows": []}, {"rows": "x"}, {"rows": [{"strategy": "lo_magic", "status": "ok"}]},
                {"rows": [{"strategy": "lo_embed", "status": "great"}]}, {"rows": [row("lo_embed")] * 21},
                {"rows": [dict(row("lo_embed"), csrftoken="x")]}):
        assert ext.post(f"/jobs/{jid}/probe", bad).status_code == 400, bad
    # unknown words and odd values are dropped, never stored
    out = ext.ok(f"/jobs/{jid}/probe", {"rows": [dict(row("lo_embed", fields=["views", "secret", 7]), handle="Bad Handle!",
                                                      code="<script>", ms="fast", layer="logged_in")]})
    assert out["rows"] == 1
    with db.tx(immediate=False) as conn:
        stored = conn.execute(select(db.fetch_probes.c.rows)).scalar()
    assert stored == [{"strategy": "lo_embed", "layer": "logged_out", "handle": None, "status": "ok", "code": None,
                       "http": 200, "ms": None, "items": 1, "fields": ["views"]}]
    for _ in range(19):
        ext.ok(f"/jobs/{jid}/probe", {"rows": [row("lo_embed")]})
    assert ext.post(f"/jobs/{jid}/probe", {"rows": [row("lo_embed")]}).status_code == 400      # 20 rows at most
    # a pull job takes no probe rows
    confirm(ext.device_id)
    ext.ok(f"/jobs/{jid}/done", {})
    jid2 = jobs_ig.start_pull(member.id, add_account(member), "first")
    ext.lease(["ig_pull"])
    assert ext.post(f"/jobs/{jid2}/probe", {"rows": [row("lo_embed")]}).status_code == 400


def test_probe_that_fails_or_is_blocked(ext, member, member_client):
    # a failure before the logged-out part ended: failed, no verdict, rows kept
    pid, jid = (lambda j: (j["probe_id"], j["job_id"]))(member_client.post("/api/fetch/probe", json={}).json())
    ext.lease(["ig_probe"])
    ext.ok(f"/jobs/{jid}/probe", {"rows": [row("lo_profile_page", "blocked", "rate")]})
    out = ext.ok(f"/jobs/{jid}/fail", {"code": "lo_blocked", "lo": {"class": "rate", "strategy": "lo_profile_page"}})
    assert out["lo"]["cooldown_reason"] == "rate"
    got = member_client.get(f"/api/fetch/probe/{pid}").json()
    assert got["state"] == "failed" and got["verdict"] is None and len(got["rows"]) == 1
    assert got["message"] == "Instagram asks ReelVault to wait. It goes on by itself."
    assert job_row(jid).state == "failed"
    # a logged-in block AFTER the logged-out part: the test ends as done with a verdict, and never waits a day
    pid, jid = (lambda j: (j["probe_id"], j["job_id"]))(member_client.post("/api/fetch/probe", json={}).json())
    ext.lease(["ig_probe"])
    ext.ok(f"/jobs/{jid}/probe", {"rows": [row(s, "skipped", "cooling") for s in ALL_LO] + [row("li_session", fields=["signed_in"]),
                                                                                              row("li_clips", "blocked", "rate_limited")]})
    assert ext.ok(f"/jobs/{jid}/fail", {"code": "rate_limited", "http_status": 429})["cooldown_until"]
    got = member_client.get(f"/api/fetch/probe/{pid}").json()
    assert got["state"] == "done" and got["verdict"]["code"] == "wait" and job_row(jid).state == "failed"
    # now the login is blocked for a day: a new probe waits (only this holds a probe back)
    pid3, jid3 = (lambda j: (j["probe_id"], j["job_id"]))(member_client.post("/api/fetch/probe", json={}).json())
    assert ext.lease()["job"] is None and job_row(jid3).state == "waiting"
    # a canceled probe shows as failed
    jobs.cancel(jid3, member.id)
    assert member_client.get(f"/api/fetch/probe/{pid3}").json()["state"] == "failed"
    assert member_client.get("/api/fetch/status").json()["last_probe"]["id"] == pid      # the last one that ended


def test_probe_lease_expiry_closes_it(ext, member, member_client):
    pid, jid = (lambda j: (j["probe_id"], j["job_id"]))(member_client.post("/api/fetch/probe", json={}).json())
    for _ in range(3):
        assert ext.lease(["ig_probe"])["job"]["id"] == jid
        with db.tx() as conn:
            conn.execute(db.jobs.update().where(db.jobs.c.id == jid).values(lease_until=db.utcnow() - timedelta(seconds=5)))
        jobs.requeue_expired()
    assert job_row(jid).state == "failed"
    assert member_client.get(f"/api/fetch/probe/{pid}").json()["state"] == "failed"


def test_probe_is_private_to_its_member(ext, member, member_client, other_client, other_member, make_device, anon):
    pid, jid = (lambda j: (j["probe_id"], j["job_id"]))(member_client.post("/api/fetch/probe", json={}).json())
    assert other_client.get(f"/api/fetch/probe/{pid}").status_code == 404
    assert other_client.get("/api/fetch/probe/latest").status_code == 404
    assert other_client.get("/api/fetch/probe/not-an-id").status_code == 404
    did2, h2 = make_device(other_member)
    set_version(did2, "2.1.0")
    e2 = Ext(anon, h2, did2)
    assert e2.lease()["job"] is None                                           # never another member's probe
    assert e2.post(f"/jobs/{jid}/probe", {"rows": [row("lo_embed")]}).status_code == 404
    r2 = other_client.post("/api/fetch/probe", json={})
    assert r2.status_code == 201 and r2.json()["probe_id"] != pid and r2.json()["job_id"] != jid
    assert other_client.get("/api/fetch/status").json()["last_probe"] is None
    assert member_client.get("/api/fetch/probe/latest").json()["probe_id"] == pid


# ============================================================================================== settings (5.1)

def test_read_mode_settings(member_client, other_client, admin_client, member, other_member):
    s = member_client.get("/api/fetch/settings").json()
    assert s["read_mode"] == "auto" and s["default"] == "auto"
    assert [o["id"] for o in s["options"]] == ["auto", "logged_in_only", "logged_out_only"]
    assert s["options"][0] == {"id": "auto", "label": "Safest first", "recommended": True,
                               "text": "ReelVault reads without your login first. It uses your login only for what is missing."}
    for bad in ({}, {"read_mode": "fast"}, {"read_mode": None}, {"read_mode": ["auto"]}):
        assert member_client.put("/api/fetch/settings", json=bad).status_code == 400
    out = member_client.put("/api/fetch/settings", json={"read_mode": "logged_out_only"})
    assert out.status_code == 200 and out.json()["read_mode"] == "logged_out_only" and out.json()["options"] == s["options"]
    assert other_client.get("/api/fetch/settings").json()["read_mode"] == "auto"          # only this member changed
    # the other settings of the member are untouched
    with db.tx(immediate=False) as conn:
        st = conn.execute(select(db.users.c.settings).where(db.users.c.id == member.id)).scalar()
    assert st["timezone"] == "Europe/London" and st["read_mode"] == "logged_out_only"
    # the admin default applies to members who never chose; a member's own choice wins
    assert member_client.put("/api/admin/fetch-health/settings", json={"default_read_mode": "logged_in_only"}).status_code == 403
    assert admin_client.put("/api/admin/fetch-health/settings", json={"default_read_mode": "logged_in_only"}).json() == {"ok": True}
    assert other_client.get("/api/fetch/settings").json() == dict(s, read_mode="logged_in_only", default="logged_in_only")
    assert member_client.get("/api/fetch/settings").json()["read_mode"] == "logged_out_only"
    aid = add_account(other_member)
    jid = jobs_ig.start_pull(other_member.id, aid, "first")
    assert job_row(jid).payload["read"] == "logged_in_only" and pull_row(jid).read_mode == "logged_in_only"


def test_fetch_endpoints_need_a_session(client):
    for method, url in [("GET", "/api/fetch/settings"), ("PUT", "/api/fetch/settings"), ("GET", "/api/fetch/status"),
                        ("GET", "/api/fetch/pulls"), ("POST", "/api/fetch/probe"), ("GET", "/api/fetch/probe/latest"),
                        ("GET", "/api/admin/fetch-health"), ("POST", "/api/admin/fetch-health/strategy"),
                        ("PUT", "/api/admin/fetch-health/settings")]:
        assert client.request(method, url, json={}).status_code == 401, url


# ============================================================================================== status (5.1)

def test_status_tells_which_layer_works(anon, member, member_client, make_device):
    st = member_client.get("/api/fetch/status").json()
    assert st["read_mode"] == "auto" and st["device"] == {"connected": False, "version": None, "needs_update": False}
    assert [(x["id"], x["label"], x["state"]) for x in st["layers"]] == [
        ("logged_out", "Without your login", "unknown"), ("logged_in", "With your Instagram login", "unknown")]
    assert st["last_probe"] is None
    did, h = make_device(member)                    # 2.0.0, not confirmed
    e = Ext(anon, h, did)
    st = member_client.get("/api/fetch/status").json()
    assert st["device"] == {"connected": True, "version": "2.0.0", "needs_update": True}
    assert (st["layers"][0]["state"], st["layers"][0]["detail"]) == ("off", "Update the Chrome extension to read without your login.")
    assert st["layers"][1]["state"] == "blocked" and "Confirm" in st["layers"][1]["detail"]
    set_version(did, "2.1.0")
    confirm(did)
    st = member_client.get("/api/fetch/status").json()
    assert (st["layers"][0]["state"], st["layers"][0]["detail"]) == ("unknown", "Not tried yet. Run the test to check.")
    assert (st["layers"][1]["state"], st["layers"][1]["detail"]) == ("ok", "Works.")
    # after a read
    aid = add_account(member)
    jid = jobs_ig.start_pull(member.id, aid, "first")
    e.lease(["ig_pull"])
    e.lo_items(jid, [v1_reel(m) for m in TIMELINE[:12]], account=lo_user(), strategy="lo_reels_gql")
    lo = member_client.get("/api/fetch/status").json()["layers"][0]
    assert (lo["state"], lo["detail"]) == ("ok", "Works. Last read just now.") and lo["last_ok_at"] and not lo["last_blocked_at"]
    # a rate block: cooling, with the end time
    e.lo_items(jid, [], log=[log_row("lo_reels_gql", "blocked", "rate", 429)], strategy=None)
    lo = member_client.get("/api/fetch/status").json()["layers"][0]
    assert lo["state"] == "cooling" and lo["cooldown_until"] and lo["last_blocked_at"]
    assert lo["detail"] == "Instagram asks ReelVault to wait. It goes on by itself."
    # signed out of Instagram: only the logged-in layer is blocked
    e.ok(f"/jobs/{jid}/fail", {"code": "signed_out"})
    li = member_client.get("/api/fetch/status").json()["layers"][1]
    assert li["state"] == "blocked" and li["detail"] == jobs_ig.MSG["signed_out"]
    # read modes switch a layer off in the member's own words
    set_read_mode(member, "logged_in_only")
    assert member_client.get("/api/fetch/status").json()["layers"][0]["detail"] == "You turned this off in your settings."
    set_read_mode(member, "logged_out_only")
    st = member_client.get("/api/fetch/status").json()
    assert st["read_mode"] == "logged_out_only" and st["layers"][1]["state"] == "off"
    for layer in st["layers"]:
        assert set(layer) == {"id", "label", "state", "detail", "cooldown_until", "last_ok_at", "last_blocked_at"}


def test_status_partial_and_signed_out_lines_name_a_real_step(anon, member, member_client, make_device):
    did, h = make_device(member)
    set_version(did, "2.1.0")
    confirm(did)
    e = Ext(anon, h, did)
    aid = add_account(member)
    jid = jobs_ig.start_pull(member.id, aid, "first")
    e.lease(["ig_pull"])
    e.lo_items(jid, [v1_reel(m) for m in TIMELINE[:12]], account=lo_user(), strategy="lo_reels_gql")
    # a later block on one way only (no cooldown): "Works in part" with its own state, not a green "ok"
    e.lo_items(jid, [], log=[log_row("lo_posts_gql", "blocked", "changed", 200)], strategy=None)
    lo = member_client.get("/api/fetch/status").json()["layers"][0]
    assert (lo["state"], lo["detail"]) == ("partial", "Instagram blocks some ways right now. The others work.")
    # the extension reports "signed out" (no cooldown, so no Try again button): the line names a real action
    with db.tx() as conn:
        jobs_ig._set_device_state(conn, did, signed_in=False)
    li = member_client.get("/api/fetch/status").json()["layers"][1]
    assert li["state"] == "blocked" and "Try again" not in li["detail"]
    assert li["detail"] == "You're signed out of Instagram in Chrome. Sign in at instagram.com. ReelVault checks again by itself."


# ============================================================================================== pulls (5.2)

def test_pull_list_states_messages_paging_and_isolation(anon, member, other_member, member_client, other_client,
                                                        make_device):
    did, h = make_device(member)
    set_version(did, "2.1.0")
    confirm(did)
    e = Ext(anon, h, did)
    a1, a2 = add_account(member), add_account(member, "second.account")
    assert member_client.get("/api/fetch/pulls").json() == {"pulls": [], "next_cursor": None}
    # an old pull from before the fetch engine (no layer stored) reads as "with your login"
    with db.tx() as conn:
        conn.execute(db.pulls.insert().values(id=db.new_id("pul"), user_id=member.id, account_id=a2, mode="first",
                                              reels_new=90, pages=2, stop_reason="page_cap", layers={},
                                              started_at=db.utcnow() - timedelta(days=3),
                                              finished_at=db.utcnow() - timedelta(days=3)))
    jid = jobs_ig.start_pull(member.id, a1, "first")
    waiting = member_client.get("/api/fetch/pulls").json()["pulls"]
    assert [(p["state"], p["handle"], p["read"]["message"]) for p in waiting] == [
        ("waiting", "scaleit.demo", "Waiting for Chrome."),
        ("done", "second.account", "Read with your Instagram login.")]
    assert waiting[0]["read"]["mode"] == "auto" and waiting[1]["read"]["layer"] == "logged_in"
    e.lease(["ig_pull"])
    running = member_client.get(f"/api/fetch/pulls?account_id={a1}").json()["pulls"]
    assert len(running) == 1 and (running[0]["state"], running[0]["read"]["message"]) == ("running", "Reading without your login.")
    e.lo_items(jid, [v1_reel(m) for m in TIMELINE[:12]], account=lo_user(), strategy="lo_reels_gql")
    assert e.stage_done(jid, "blocked")["resume"]
    e.li_page(jid, TIMELINE[:12], max_id=mk.cursor_for(12), account=li_user())
    p = member_client.get(f"/api/fetch/pulls?account_id={a1}").json()["pulls"][0]
    assert p["read"]["message"] == "Reading with your Instagram login." and p["reels_new"] == 12
    # a logged-in cooldown: it waits and says so
    e.ok(f"/jobs/{jid}/fail", {"code": "rate_limited"})
    p = member_client.get(f"/api/fetch/pulls?account_id={a1}").json()["pulls"][0]
    assert (p["state"], p["read"]["message"]) == ("waiting", "Instagram asks ReelVault to wait. It goes on by itself.")
    assert set(p) == {"id", "account_id", "handle", "mode", "state", "started_at", "finished_at", "reels_new", "read"}
    assert set(p["read"]) == {"mode", "layer", "logged_out", "logged_in", "message", "stop_label"}
    # paging
    page1 = member_client.get("/api/fetch/pulls?limit=1").json()
    assert len(page1["pulls"]) == 1 and page1["next_cursor"] == "1"
    page2 = member_client.get("/api/fetch/pulls?limit=1&cursor=1").json()
    assert len(page2["pulls"]) == 1 and page2["next_cursor"] is None and page2["pulls"][0]["id"] != page1["pulls"][0]["id"]
    assert member_client.get("/api/fetch/pulls?cursor=x").status_code == 400
    # another member sees none of it
    assert other_client.get("/api/fetch/pulls").json() == {"pulls": [], "next_cursor": None}
    assert other_client.get(f"/api/fetch/pulls?account_id={a1}").status_code == 404
    assert member_client.get("/api/fetch/pulls?account_id=nope").status_code == 404


# ============================================================================================== admin (5.4)

def test_admin_fetch_health_card(anon, member, member_client, admin_client, make_device):
    assert member_client.get("/api/admin/fetch-health").status_code == 403
    empty = admin_client.get("/api/admin/fetch-health").json()
    assert [s["id"] for s in empty["strategies"]] == ALL_LO + list(fetch.LI_STRATEGIES)
    assert all(s["ok"] == 0 and s["rate"] is None and s["enabled"] and s["off"] is None for s in empty["strategies"])
    assert {s["id"]: s["verified"] for s in empty["strategies"]}["lo_reels_gql"] == "guess"
    assert empty["layers"] == [{"id": x, "pulls": 0, "reels": 0} for x in ("logged_out", "logged_in", "mixed")]
    assert empty["settings"] == {"default_read_mode": "auto", "doc_ids": fetch.DOC_IDS, "probe_handle": "instagram"}
    did, h = make_device(member)
    set_version(did, "2.1.0")
    confirm(did)
    e = Ext(anon, h, did)
    jid = jobs_ig.start_pull(member.id, add_account(member), "first")
    e.lease(["ig_pull"])
    log = [log_row("lo_profile_page", items=12), log_row("lo_reels_page", "partial", "no_data"),
           log_row("lo_reels_gql", "blocked", "login_wall", 401)] + [log_row("lo_post_gql", items=1)] * 3
    e.lo_items(jid, [v1_reel(m, strategy="lo_post_gql") for m in TIMELINE[:3]], account=lo_user(), log=log)
    assert e.stage_done(jid, "budget")["resume"]
    for i in range(2):
        e.li_page(jid, TIMELINE[i * 50:(i + 1) * 50], max_id=mk.cursor_for((i + 1) * 50), account=li_user() if i == 0 else None)
    e.ok(f"/jobs/{jid}/done", {"stop_reason": "target_reached", "fetch_log": [log_row("li_clips", items=50)] * 2})
    card = admin_client.get("/api/admin/fetch-health").json()
    by = {s["id"]: s for s in card["strategies"]}
    assert (by["lo_post_gql"]["ok"], by["lo_post_gql"]["rate"], by["lo_post_gql"]["devices"], by["lo_post_gql"]["label"]) == (
        3, 1.0, 1, "Reel detail")
    assert (by["lo_reels_gql"]["blocked"], by["lo_reels_gql"]["rate"], by["lo_reels_gql"]["last_code"]) == (1, 0.0, "login_wall")
    assert by["lo_reels_gql"]["last_blocked_at"] and by["lo_reels_page"]["partial"] == 1 and by["li_clips"]["ok"] == 2
    assert by["lo_profile_page"]["layer"] == "logged_out" and by["li_clips"]["layer"] == "logged_in"
    assert card["layers"] == [{"id": "logged_out", "pulls": 0, "reels": 0}, {"id": "logged_in", "pulls": 0, "reels": 0},
                              {"id": "mixed", "pulls": 1, "reels": 100}]
    assert card["devices"] == [{"device_id": did, "user_email": "member@example.com", "version": "2.1.0",
                                "lo_state": "ok", "lo_cooldown_until": None, "lo_cooldown_reason": None,
                                "li_cooldown_until": None, "strategy_off": ["lo_reels_gql"],
                                "last_probe_verdict": None, "last_seen_at": card["devices"][0]["last_seen_at"]}]
    assert card["recent_blocks"] == [{"at": card["recent_blocks"][0]["at"], "strategy": "lo_reels_gql",
                                      "code": "login_wall", "http": 401, "device_id": did}]
    assert "caption" not in str(card) and TIMELINE[0]["code"] not in str(card)          # numbers only


def test_admin_fetch_settings_and_doc_ids_reach_the_extension(anon, member, admin_client, make_device):
    did, h = make_device(member)
    set_version(did, "2.1.0")
    e = Ext(anon, h, did)
    put = lambda body: admin_client.put("/api/admin/fetch-health/settings", json=body)  # noqa: E731
    for bad in ({"doc_ids": {"lo_post_gql": ["12"]}}, {"doc_ids": {"lo_profile_page": ["1234567890123"]}},
                {"doc_ids": {"lo_post_gql": "1234567890123"}}, {"doc_ids": []}, {"probe_handle": "bad handle"},
                {"default_read_mode": "turbo"}, {"other": 1}, {"doc_ids": {"lo_post_gql": ["1234567890123"] * 5}}):
        assert put(bad).status_code == 400, bad
    assert put({"doc_ids": {"lo_post_gql": ["99999999999999999", "27128499623469141"]}, "probe_handle": "@NatGeo"}).json() == {"ok": True}
    hello = e.ok("/hello", {"version": "2.1.0"})
    assert hello["fetch"]["doc_ids"]["lo_post_gql"] == ["99999999999999999", "27128499623469141"]
    assert hello["fetch"]["doc_ids"]["lo_reels_gql"] == fetch.DOC_IDS["lo_reels_gql"]      # the others keep the built-in ids
    assert hello["fetch"]["probe_handle"] == "natgeo"
    assert admin_client.get("/api/admin/fetch-health").json()["settings"]["probe_handle"] == "natgeo"
    assert put({"doc_ids": {"lo_post_gql": []}}).json() == {"ok": True}                     # empty: back to the built-in ids
    assert e.ok("/hello", {"version": "2.1.0"})["fetch"]["doc_ids"] == fetch.DOC_IDS
    # these settings stay out of the general settings form and out of /api/me
    assert not any(k.startswith(("fetch", "internal")) for k in admin_client.get("/api/admin/settings").json())


# ============================================================================================== review fixes: verdicts

LO_WALL_ROWS = [row("lo_profile_page", "blocked", "login_wall"), row("lo_reels_page", "blocked", "login_wall"),
                row("lo_reels_gql", "skipped", "no_user"), row("lo_web_profile", "blocked", "login_wall"),
                row("lo_posts_gql", "skipped", "no_user"), row("lo_post_gql", "skipped", "no_reel"),
                row("lo_media_gql", "skipped", "no_reel"), row("lo_embed", "skipped", "no_reel"),
                row("lo_profile_page", "blocked", "login_wall", handle="scaleit.demo")]


def _one_comma(v):
    return max(s.count(",") for part in (v["title"], v["text"], v["next"]) for s in part.split(". ")) <= 1


def test_verdict_logged_out_only_never_says_sign_in():
    v = fetch.verdict_of(LO_WALL_ROWS, "logged_out_only")
    assert v["code"] == "lo_only_blocked" and _one_comma(v)
    assert v["title"] == "Instagram asks for a login here. You chose to read without it."
    assert v["next"] == "Try again later, or pick Safest first in Settings."
    assert "sign in" not in (v["text"] + v["next"]).lower()
    # works in part, logged-out only: no word about the login
    part = fetch.verdict_of([row("lo_reels_gql", fields=["reel_list"]), row("lo_post_gql", "blocked", "login_wall")],
                            "logged_out_only")
    assert part["code"] == "partial" and "login" not in (part["text"] + part["next"]).replace("without your login", "")
    # the same rows in auto mode keep the auto words
    assert fetch.verdict_of([row("lo_reels_gql", fields=["reel_list"])], "auto")["text"] == fetch.VERDICTS["partial"]["text"]


def test_verdict_signed_out_and_blocked_are_two_cases():
    signed_out = LO_WALL_ROWS + [row("li_session", "blocked", "signed_out", handle=None),
                                 row("li_clips", "blocked", "signed_out")]
    v = fetch.verdict_of(signed_out, "auto")
    assert v["code"] == "signed_out" and v["title"] == "You're signed out of Instagram in Chrome."
    assert v["next"] == "Sign in at instagram.com in Chrome. Then run the test again." and _one_comma(v)
    blocked = LO_WALL_ROWS + [row("li_session", fields=["signed_in"], handle=None), row("li_clips", "blocked", "challenge")]
    v = fetch.verdict_of(blocked, "auto")
    assert v["code"] == "nothing" and v["title"] == "Instagram blocks reads right now."
    assert v["next"] == "ReelVault waits by itself. Run the test again in a few hours."
    assert "sign in" not in (v["text"] + v["next"]).lower()


def test_verdict_rate_limit_is_a_wait_not_works_in_part():
    # the exact rows of the lo_rate_429 review run: only the profile page worked
    rows = ([row("lo_profile_page", fields=["user", "followers", "reel_list", "lsd"]),
             row("lo_reels_page", "blocked", "rate")]
            + [row(s, "skipped", "cooling") for s in ALL_LO[2:]]
            + [row("lo_profile_page", "skipped", "cooling", handle="scaleit.demo"),
               row("li_session", "skipped", "cooling", handle=None), row("li_clips", "skipped", "cooling")])
    v = fetch.verdict_of(rows, "auto")
    assert v["code"] == "wait" and v["title"] == "Instagram asks ReelVault to wait."
    assert v["next"] == "ReelVault goes on by itself. Run the test again in a few hours."


def test_verdict_best_with_a_signed_out_login_says_sign_in():
    rows = [row(s, fields=["reel_list", "views"]) for s in ALL_LO] + [
        row("li_session", "blocked", "signed_out", handle=None), row("li_clips", "blocked", "signed_out")]
    v = fetch.verdict_of(rows, "auto")
    assert v["code"] == "best" and v["title"] == "The safest way works."
    assert v["next"] == "Sign in to Instagram in Chrome. ReelVault uses your login to fill in what is missing."
    assert fetch.verdict_of(rows[:8], "auto")["next"] == "You don't need to do anything."


def test_probe_stores_the_read_mode_for_its_verdict(ext, member, member_client):
    assert member_client.put("/api/fetch/settings", json={"read_mode": "logged_out_only"}).status_code == 200
    pid, jid = (lambda j: (j["probe_id"], j["job_id"]))(member_client.post("/api/fetch/probe", json={}).json())
    lz = ext.lease(["ig_probe"])
    assert lz["job"]["payload"]["read"] == "logged_out_only" and "li_session" not in lz["job"]["payload"]["want"]
    ext.ok(f"/jobs/{jid}/probe", {"rows": LO_WALL_ROWS})
    ext.ok(f"/jobs/{jid}/done", {})
    got = member_client.get(f"/api/fetch/probe/{pid}").json()
    assert got["verdict"]["code"] == "lo_only_blocked"
    assert member_client.get("/api/fetch/status").json()["last_probe"]["verdict"]["code"] == "lo_only_blocked"
