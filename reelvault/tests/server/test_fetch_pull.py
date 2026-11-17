"""Fetch engine, server side: plan at lease time, logged-out ingest, merge with the logged-in top-up, cooldowns
(DESIGN-FETCH.md 2.2, 2.3, 4.1, 4.2). The fake extension here is version 2.1.0 unless a test says otherwise."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from _fetch_support import (ALL_LO, PK, TIMELINE, Ext, account_row, add_account, confirm, count_reels, device_row,
                            gql_item, job_row, li_user, lo, lo_user, load_json, log_row, mk, pull_row, reel_rows,
                            run_now, set_read_mode, set_version, v1_detail, v1_reel)
from reelvault.server import db, fetch, jobs, jobs_ig, jobs_score
from reelvault.server.testing import BASE_URL


@pytest.fixture
def anon(app):
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def ext(anon, member, make_device):
    """A paired, confirmed extension 2.1.0."""
    did, h = make_device(member)
    set_version(did, "2.1.0")
    confirm(did)
    return Ext(anon, h, did)


@pytest.fixture
def old_ext(anon, member, make_device):
    """A paired, confirmed extension 2.0.0 (before the fetch engine)."""
    did, h = make_device(member)
    confirm(did)
    return Ext(anon, h, did)


def leased(e, member, mode="first", aid=None, read=None):
    aid = aid or add_account(member)
    if read:
        set_read_mode(member, read)
    jid = jobs_ig.start_pull(member.id, aid, mode)
    lz = e.lease(["ig_pull"])
    assert lz["job"] and lz["job"]["id"] == jid, lz
    return aid, jid, lz["job"]["payload"]


def li_pages(e, jid, n_pages, size=50, start=0):
    out = None
    for i in range(n_pages):
        off = start + i * size
        r = e.li_page(jid, TIMELINE[off:off + size], max_id=mk.cursor_for(off + size), account=li_user() if i == 0 else None)
        assert r.status_code == 200, r.text
        out = r.json()
        if out["stop"]:
            break
    return out


def first_login_pull(e, member, n=100):
    """Store the first `n` reels with a plain logged-in pull (read mode logged_in_only for this one pull)."""
    aid, jid, _ = leased(e, member, read="logged_in_only")
    li_pages(e, jid, 2)
    e.ok(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})
    set_read_mode(member, "auto")
    return aid


def fetch_of(device_id) -> dict:
    return fetch.device_fetch_state(device_row(device_id))


def hours_left(iso) -> float:
    return (fetch._dt(iso) - db.utcnow()).total_seconds() / 3600


# ============================================================================================== hello + plan (4.1)

def test_hello_adds_fetch_config_pacing_and_lo_state(ext):
    b = ext.ok("/hello", {"version": "2.1.0"})
    assert b["fetch"]["layers"] == ["logged_out", "logged_in"] and b["fetch"]["strategies"] == ALL_LO
    assert b["fetch"]["off"] == [] and b["fetch"]["probe_handle"] == "instagram"
    assert b["fetch"]["doc_ids"] == load_json("manifest.json")["doc_ids"]
    p = b["pacing"]
    assert (p["lo_gap_ms"], p["lo_page_gap_ms"], p["layer_gap_ms"]) == ([5000, 11000], [8000, 15000], [20000, 40000])
    assert (p["lo_hour_max"], p["lo_day_max"], p["lo_detail_max"], p["probe_day_max"]) == (60, 200, 36, 3)
    assert (p["gap_ms"], p["hour_max"], p["day_max"]) == ([4000, 9000], 120, 400)          # the old rules stay
    assert p["lo_gap_ms"][0] >= p["gap_ms"][0] and p["lo_hour_max"] <= p["hour_max"]       # stricter or equal
    assert b["lo"] == {"cooldown_until": None, "cooldown_reason": None, "strategy_off": {}}


def test_plan_per_read_mode_and_pull_mode(ext, member):
    aid, jid, p = leased(ext, member)
    assert p["read"] == "auto" and p["plan"] == {"lo": True, "li": True, "lo_budget": 40, "strategies": ALL_LO}
    assert pull_row(jid).read_mode == "auto" and jobs_ig.payload_problem("ig_pull", job_row(jid).payload) is None
    jobs.cancel(jid, member.id)
    # logged_out_only may use 80 calls per pull, but never more than the hour still allows (60 an hour)
    for read, mode, want in [("auto", "refresh", (True, True, 16)), ("logged_out_only", "first", (True, False, 60)),
                             ("logged_in_only", "first", (False, True, 0)), ("auto", "next", (False, True, 0)),
                             ("logged_out_only", "next", (True, False, 60))]:
        _, jid, p = leased(ext, member, mode, aid=aid, read=read)
        assert (p["plan"]["lo"], p["plan"]["li"], p["plan"]["lo_budget"]) == want, (read, mode)
        assert p["read"] == read and pull_row(jid).read_mode == read
        assert p["plan"]["strategies"] == (ALL_LO if want[0] else [])
        jobs.cancel(jid, member.id)


def test_old_extension_gets_the_old_payload_and_never_a_logged_out_plan(old_ext, member):
    aid, jid, p = leased(old_ext, member)
    assert p == {"account_id": aid, "handle": "scaleit.demo", "mode": "first", "cursor_source": "clips", "target": 100}
    assert job_row(jid).payload["plan"] == {"lo": False, "li": True, "lo_budget": 0, "strategies": []}
    # a plain logged-in pull works exactly as before
    out = li_pages(old_ext, jid, 3)
    assert out["stop"] is True and "resume" not in out and count_reels(member.id) == 100
    assert old_ext.post(f"/jobs/{jid}/done", {"stop_reason": "target_reached"}).json() == {"ok": True}
    pull = pull_row(jid)
    assert pull.layer == "logged_in" and pull.layers == {"logged_in": {"reels": 100, "updated": 0, "pages": 2}}
    rows = reel_rows(member.id)
    assert {r.read_layer for r in rows.values()} == {"logged_in"} and not any(r.partial for r in rows.values())
    assert {r.read_strategy for r in rows.values()} == {"li_clips"}


def test_logged_out_only_waits_for_an_extension_update(old_ext, member):
    aid = add_account(member)
    set_read_mode(member, "logged_out_only")
    jid = jobs_ig.start_pull(member.id, aid, "first")
    lz = old_ext.lease(["ig_pull"])
    assert lz["job"] is None
    j = job_row(jid)
    assert j.state == "waiting" and j.attempts == 0 and j.run_after > db.utcnow()
    assert j.error_message == "Update the Chrome extension to read without your login."


def test_unconfirmed_device_reads_logged_out_but_never_with_the_login(anon, member, make_device):
    did, h = make_device(member)
    set_version(did, "2.1.0")
    e = Ext(anon, h, did)
    aid = add_account(member)
    jid = jobs_ig.start_pull(member.id, aid, "first")
    lz = e.lease()
    assert lz["job"]["id"] == jid and lz["job"]["payload"]["plan"] == {"lo": True, "li": False, "lo_budget": 40,
                                                                      "strategies": ALL_LO}
    # the confirm gate guards the login only: this logged-out job keeps its lease
    assert e.ok(f"/jobs/{jid}/progress", {"done": 1, "total": 100})["cancel"] is False
    r = e.lo_items(jid, [v1_reel(m) for m in TIMELINE[:12]], account=lo_user(), strategy="lo_reels_gql")
    assert r.json()["accepted"] == 12
    # nothing is left to top up with (no login): the stage end closes the pull
    assert e.stage_done(jid, "budget") == {"ok": True, "accepted": 0, "known_hit": False, "stop": True,
                                           "lo": {"cooldown_until": None, "cooldown_reason": None, "strategy_off": {}}}
    # a pull that needs the login is not handed out, other kinds neither
    jobs.cancel(jid, member.id)
    set_read_mode(member, "logged_in_only")
    jid2 = jobs_ig.start_pull(member.id, aid, "first")
    lz = e.lease()
    assert lz["job"] is None and lz["reason"] == "confirm_account"
    # not leased at all (as before the fetch engine): no attempt spent, and it runs at once after the confirm
    assert job_row(jid2).state == "waiting" and job_row(jid2).attempts == 0 and job_row(jid2).run_after is None


def test_signed_out_cooldown_still_leases_logged_out_pulls(ext, member):
    aid, jid, _ = leased(ext, member, read="logged_in_only")
    assert ext.ok(f"/jobs/{jid}/fail", {"code": "signed_out"})["cooldown_until"]
    assert ext.lease()["reason"] == "cooldown"          # the logged-in pull waits
    jobs.cancel(jid, member.id)
    set_read_mode(member, "auto")
    jid2 = jobs_ig.start_pull(member.id, aid, "first")
    lz = ext.lease()
    assert lz["job"]["id"] == jid2 and lz["job"]["payload"]["plan"]["li"] is False and lz["job"]["payload"]["plan"]["lo"]


def test_logged_in_block_also_stops_logged_out_reads(ext, member):
    aid, jid, _ = leased(ext, member)
    ext.ok(f"/jobs/{jid}/fail", {"code": "rate_limited", "http_status": 429})
    run_now(jid)
    lz = ext.lease()
    assert lz["job"] is None and lz["reason"] == "cooldown"
    assert job_row(jid).state == "waiting"


def test_logged_out_budget_limits_the_plan(ext, member):
    aid = add_account(member)
    jid = jobs_ig.start_pull(member.id, aid, "first")
    lz = ext.lease(budget={"hour_used": 3, "day_used": 3, "cdn_day_used": 0, "lo_hour_used": 57, "lo_day_used": 60})
    assert lz["job"]["payload"]["plan"]["lo_budget"] == 3
    assert fetch_of(ext.device_id)["budget"] == {"lo_hour_used": 57, "lo_day_used": 60}
    jobs.cancel(jid, member.id)
    jid = jobs_ig.start_pull(member.id, aid, "first")
    lz = ext.lease(budget={"hour_used": 3, "day_used": 3, "cdn_day_used": 0, "lo_hour_used": 60, "lo_day_used": 60})
    assert lz["job"]["payload"]["plan"] == {"lo": False, "li": True, "lo_budget": 0, "strategies": []}
    jobs.cancel(jid, member.id)
    # logged-out calls also count in the old hour and day budgets, on the server too
    jid = jobs_ig.start_pull(member.id, aid, "first")
    ext.lease(budget={"hour_used": 0, "day_used": 0, "cdn_day_used": 0, "lo_hour_used": 0, "lo_day_used": 0})
    log = [log_row("lo_profile_page", items=12)] + [log_row("lo_post_gql", items=1)] * 4 + [log_row("lo_embed", "skipped", "no_reel")]
    ext.lo_items(jid, [v1_detail(m) for m in TIMELINE[:4]], account=lo_user(), log=log)
    with db.tx(immediate=False) as conn:
        counts = {k: jobs_ig._server_count(conn, jobs_ig.ig_scope(member.id), k, "hour") for k in ("lo_calls", "ig_calls")}
    assert counts == {"lo_calls": 5, "ig_calls": 5}        # the skipped row made no call
    assert pull_row(jid).layers["logged_out"]["calls"] == 5
    assert pull_row(jid).layers["logged_out"]["strategies"] == {"lo_profile_page": 1, "lo_post_gql": 4}


# ============================================================================================== ingest (4.2)

def test_contract_upload_is_stored_as_expected(ext, member):
    aid, jid, _ = leased(ext, member)
    body = load_json("upload_items.json")
    r = ext.post(f"/jobs/{jid}/items", body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["accepted"] == 4 and out["stop"] is False and out["known_hit"] is False and "resume" not in out
    rows = reel_rows(member.id)
    for exp in load_json("expected_reels.json"):
        row = rows[exp["ig_pk"]]
        got = {"ig_pk": row.ig_pk, "code": row.code, "taken_at": db.iso(row.taken_at), "views": row.views,
               "views_field": row.views_field, "likes": row.likes, "comments": row.comments, "partial": row.partial,
               "read_layer": row.read_layer, "read_strategy": row.read_strategy,
               "has_video_url": row.video_url is not None}
        assert got == {k: exp[k] for k in got}
    acc = account_row(aid)
    assert (acc.ig_pk, acc.followers, acc.media_count) == (PK, 182000, 640)
    assert acc.cursor_max_id is None and pull_row(jid).to_max_id is None and pull_row(jid).pages == 0   # no cursor work
    # the fetch_log of the example is counted: one login wall on the reels list switches that strategy off for 6 h
    off = out["lo"]["strategy_off"]
    assert list(off) == ["lo_reels_gql"] and 5.9 < hours_left(off["lo_reels_gql"]) <= 6
    with db.tx(immediate=False) as conn:
        health = {r.strategy: (r.ok, r.partial, r.blocked, r.error, r.last_code) for r in conn.execute(
            select(db.fetch_health).where(db.fetch_health.c.device_id == ext.device_id)).all()}
        snaps = conn.execute(select(func.count()).select_from(db.reel_snapshots)).scalar()
    assert health == {"lo_profile_page": (1, 0, 0, 0, None), "lo_reels_gql": (0, 0, 1, 0, "login_wall"),
                      "lo_post_gql": (1, 0, 0, 0, None)}
    assert snaps == 2          # only the two reels with a real play count get a snapshot


def test_bulk_logged_out_pull_reaches_100_without_the_login(ext, member, member_client):
    aid, jid, _ = leased(ext, member)
    out = None
    for i in range(9):
        r = ext.lo_items(jid, [v1_reel(m) for m in TIMELINE[i * 12:(i + 1) * 12]],
                         account=lo_user() if i == 0 else None, strategy="lo_reels_gql")
        out = r.json()
        if out["stop"]:
            break
    assert i == 8 and out["stop"] is True and "resume" not in out and count_reels(member.id) == 100
    assert ext.ok(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})["ok"]
    pull = pull_row(jid)
    assert (pull.reels_new, pull.pages, pull.layer, pull.stop_reason) == (100, 0, "logged_out", "target_reached")
    assert pull.layers["logged_out"]["reels"] == 100 and pull.layers["logged_out"]["stop"] == "target"
    rows = reel_rows(member.id)
    assert all(r.read_layer == "logged_out" and r.read_strategy == "lo_reels_gql" and not r.partial and r.views
               and r.taken_at is not None for r in rows.values())
    acc = account_row(aid)
    assert acc.status == "active" and acc.reels_count == 100 and acc.newest_pk == str(TIMELINE[0]["pk"])
    listed = member_client.get(f"/api/fetch/pulls?account_id={aid}").json()["pulls"]
    assert listed[0]["read"] == {"mode": "auto", "layer": "logged_out",
                                 "logged_out": {"reels": 100, "calls": 9, "stop": "target"},
                                 "logged_in": {"reels": 0, "pages": 0},
                                 "message": "Read without your Instagram login.", "stop_label": None}


def test_mixed_pull_merges_both_layers_without_duplicates(ext, member, member_client):
    aid, jid, _ = leased(ext, member)
    # stage 1: 24 details without view counts (they are partial)
    for i in range(4):
        r = ext.lo_items(jid, [v1_detail(m, views="none") for m in TIMELINE[i * 6:(i + 1) * 6]],
                         account=lo_user() if i == 0 else None)
        assert r.json() == {"ok": True, "accepted": 6, "known_hit": False, "stop": False, "lo": r.json()["lo"]}
    rows = reel_rows(member.id)
    assert len(rows) == 24 and all(r.partial and r.views is None and r.likes for r in rows.values())
    done = ext.stage_done(jid, "no_views")
    assert done["stop"] is False and done["resume"] == {"source": "clips", "max_id": None, "skip_known": True,
                                                        "layer": "logged_in"}
    # stage 2: the logged-in top-up from the top. Known reels get their numbers and do not count as new.
    out = li_pages(ext, jid, 3)
    assert out["stop"] is True and count_reels(member.id) == 100
    ext.ok(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})
    rows = reel_rows(member.id)
    assert len(rows) == 100 and set(rows) == {str(m["pk"]) for m in TIMELINE[:100]}
    for m in TIMELINE[:100]:
        row = rows[str(m["pk"])]
        assert (row.views, row.views_field, row.partial, row.read_layer) == (m["play_count"], "play_count", False,
                                                                             "logged_in")
    pull = pull_row(jid)
    assert pull.layer == "mixed" and pull.reels_new == 100 and pull.pages == 2
    assert pull.layers["logged_out"] == {"reels": 24, "updated": 0, "calls": 4, "strategies": {"lo_post_gql": 4},
                                         "stop": "no_views"}
    assert pull.layers["logged_in"] == {"reels": 76, "updated": 24, "pages": 2}
    acc = account_row(aid)
    assert acc.cursor_max_id == mk.cursor_for(100) and acc.cursor_source == "clips" and acc.reels_count == 100
    with db.tx(immediate=False) as conn:
        snaps = conn.execute(select(func.count(), func.count(db.reel_snapshots.c.views))
                             .select_from(db.reel_snapshots)).first()
        links = conn.execute(select(func.count()).select_from(db.reel_accounts)).scalar()
    assert tuple(snaps) == (100, 100) and links == 100         # one snapshot per reel, each with real views
    read = member_client.get("/api/fetch/pulls").json()["pulls"][0]["read"]
    assert read["layer"] == "mixed" and read["message"] == ("Read 24 reels without your login. "
                                                            "Your login filled in the other 76.")


def test_logged_out_item_never_downgrades_or_clears_stored_values(ext, member):
    aid = first_login_pull(ext, member)
    before = reel_rows(member.id)
    m0, m1, m2, m3 = TIMELINE[0], TIMELINE[1], TIMELINE[2], TIMELINE[3]
    jid = jobs_ig.start_pull(member.id, aid, "refresh")
    ext.lease(["ig_pull"])
    items = [gql_item(m0),                                             # only the smaller logged-out view_count
             v1_reel(dict(m1, play_count=m1["play_count"] + 500)),     # sparse: no caption, duration or video
             v1_detail(m2, views="none"),                              # no views at all
             dict(v1_detail(m3, views="view_count"), like_count=None, comment_count=None, caption=None)]
    out = ext.lo_items(jid, items, strategy="lo_reels_gql").json()
    assert out["accepted"] == 0 and out["known_hit"] is True and out["stop"] is True and "resume" not in out
    after = reel_rows(member.id)
    for m in (m0, m2, m3):                 # a play count is never replaced by a view_count or by nothing
        row = after[str(m["pk"])]
        assert (row.views, row.views_field, row.partial, row.read_layer) == (m["play_count"], "play_count", False,
                                                                             "logged_in")
    r1 = after[str(m1["pk"])]              # a newer play count IS taken, and it says where it came from
    assert (r1.views, r1.views_field, r1.read_layer, r1.read_strategy) == (m1["play_count"] + 500, "play_count",
                                                                           "logged_out", "lo_reels_gql")
    for m in (m0, m1, m2, m3):             # nothing stored is cleared by a missing value
        a, b = after[str(m["pk"])], before[str(m["pk"])]
        assert (a.caption, a.duration, a.cover_url is not None, a.taken_at, a.audio) == (
            b.caption, b.duration, True, b.taken_at, b.audio)
        assert a.likes is not None and a.comments is not None and a.video_url and a.video_url_expires_at
    assert after[str(m1["pk"])].video_url == before[str(m1["pk"])].video_url     # the sparse item had none
    assert count_reels(member.id) == 100


def test_logged_in_page_always_wins_over_logged_out_values(ext, member):
    aid, jid, _ = leased(ext, member)
    ext.lo_items(jid, [gql_item(m) for m in TIMELINE[:6]], account=lo_user(), strategy="lo_web_profile")
    rows = reel_rows(member.id)
    assert all(r.views_field == "view_count" and r.partial and r.read_strategy == "lo_web_profile" for r in rows.values())
    assert ext.stage_done(jid, "budget")["resume"]["skip_known"] is True
    li_pages(ext, jid, 1, size=12)
    rows = reel_rows(member.id)
    for m in TIMELINE[:6]:
        r = rows[str(m["pk"])]
        assert (r.views, r.views_field, r.partial, r.read_layer, r.read_strategy) == (
            m["play_count"], "play_count", False, "logged_in", "li_clips")


def test_logged_out_refresh_stops_at_a_known_reel(ext, member):
    aid = first_login_pull(ext, member)
    with db.tx() as conn:        # forget the 3 newest: they are "new since the last pull"
        conn.execute(db.reels.delete().where(db.reels.c.ig_pk.in_([str(m["pk"]) for m in TIMELINE[:3]])))
    jid = jobs_ig.start_pull(member.id, aid, "refresh")
    p = ext.lease(["ig_pull"])["job"]["payload"]
    assert p["mode"] == "refresh" and p["plan"]["lo_budget"] == 16
    out = ext.lo_items(jid, [v1_reel(m, strategy="lo_reels_page") for m in TIMELINE[:12]], strategy="lo_reels_page").json()
    assert (out["accepted"], out["known_hit"], out["stop"]) == (3, True, True) and "resume" not in out
    ext.ok(f"/jobs/{jid}/done", {"stop_reason": "known_reel"})
    assert count_reels(member.id) == 100 and pull_row(jid).layer == "logged_out"
    assert account_row(aid).last_refresh_at is not None


def test_logged_out_refresh_with_partial_new_reels_asks_for_the_login(ext, member):
    aid = first_login_pull(ext, member)
    with db.tx() as conn:
        conn.execute(db.reels.delete().where(db.reels.c.ig_pk.in_([str(m["pk"]) for m in TIMELINE[:2]])))
    jid = jobs_ig.start_pull(member.id, aid, "refresh")
    ext.lease(["ig_pull"])
    out = ext.lo_items(jid, [v1_detail(m, views="none") for m in TIMELINE[:4]]).json()
    assert out["accepted"] == 2 and out["known_hit"] is True and out["stop"] is False
    assert out["resume"]["layer"] == "logged_in"
    # the top-up: this pull's own two new reels do not count as "known", the older ones do
    r = ext.li_page(jid, TIMELINE[:12], max_id=mk.cursor_for(12)).json()
    assert r["known_hit"] is True and r["stop"] is True and r["accepted"] == 0
    rows = reel_rows(member.id)
    assert not any(x.partial for x in rows.values()) and rows[str(TIMELINE[0]["pk"])].views == TIMELINE[0]["play_count"]


def test_logged_out_only_pull_closes_with_what_it_has(ext, member, member_client):
    aid, jid, p = leased(ext, member, read="logged_out_only")
    assert p["plan"]["li"] is False
    ext.lo_items(jid, [v1_detail(m, views="none") for m in TIMELINE[:12]], account=lo_user())
    done = ext.stage_done(jid, "no_views")
    assert done["stop"] is True and "resume" not in done           # never a top-up in this mode
    ext.ok(f"/jobs/{jid}/done", {"stop_reason": "lo_stage"})       # an unknown reason: the server maps it
    pull = pull_row(jid)
    assert (pull.stop_reason, pull.layer, pull.reels_new) == ("page_cap", "logged_out", 12)
    rows = reel_rows(member.id)
    assert all(r.partial and r.views is None for r in rows.values())          # partial reels stay partial, never 0
    read = member_client.get("/api/fetch/pulls").json()["pulls"][0]["read"]
    assert read["mode"] == "logged_out_only" and read["message"] == (
        "Read 12 reels without your login. Instagram shows no more without a login.")


def test_logged_out_only_blocked_with_no_reels_says_so_plainly(ext, member, member_client):
    aid, jid, p = leased(ext, member, read="logged_out_only")
    done = ext.stage_done(jid, "blocked")
    assert done["stop"] is True and "resume" not in done
    ext.ok(f"/jobs/{jid}/done", {"stop_reason": "lo_stage"})
    read = member_client.get("/api/fetch/pulls").json()["pulls"][0]["read"]
    assert read["layer"] is None
    assert read["message"] == ("Instagram asks for a login right now. You chose to read without it, so ReelVault "
                               "read no reels. Try again later or pick Safest first in Settings.")
    # the Result column says why it stopped in plain words, not "Stopped at the page limit" (review fix)
    pull = member_client.get("/api/fetch/pulls").json()["pulls"][0]
    assert read["stop_label"] == "Instagram asked for a login" and pull_row(jid).stop_reason == "page_cap"


def test_stage_end_with_everything_read_needs_no_login(ext, member):
    aid, jid, _ = leased(ext, member)
    ext.lo_items(jid, [v1_reel(m) for m in TIMELINE[:30]], account=lo_user(), strategy="lo_reels_gql")
    done = ext.stage_done(jid, "no_more")          # the account has 30 reels: all read, all with views
    assert done["stop"] is True and "resume" not in done
    ext.ok(f"/jobs/{jid}/done", {"stop_reason": "no_more"})
    assert account_row(aid).cursor_done is True and pull_row(jid).layer == "logged_out"


def test_blocked_logged_out_stage_falls_back_to_the_login_at_once(ext, member, member_client):
    aid, jid, _ = leased(ext, member)
    log = [log_row("lo_profile_page", "blocked", "login_wall", 200), log_row("lo_web_profile", "error", "refused", 400)]
    done = ext.stage_done(jid, "blocked", log=log)
    assert done["resume"]["source"] == "clips" and done["stop"] is False
    li_pages(ext, jid, 3)
    ext.ok(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})
    pull = pull_row(jid)
    assert pull.layer == "logged_in" and pull.layers["logged_out"] == {"reels": 0, "updated": 0, "calls": 2,
                                                                       "strategies": {"lo_profile_page": 1, "lo_web_profile": 1},
                                                                       "stop": "blocked"}
    read = member_client.get("/api/fetch/pulls").json()["pulls"][0]["read"]
    assert read["message"] == "Instagram asks for a login right now. ReelVault used yours."


def test_bad_logged_out_uploads_are_refused(ext, member):
    aid, jid, _ = leased(ext, member)
    ok_item = v1_detail(TIMELINE[0])

    def post(items, account=lo_user(), **paging):
        return ext.lo_items(jid, items, account=account, extra_paging=paging)

    assert post([ok_item], max_id="abc").status_code == 400                       # logged-out uploads carry no cursor
    assert post([dict(ok_item, shape="v9")]).status_code == 400
    assert post([dict(ok_item, strategy="lo_unknown")]).status_code == 400
    assert post([dict(ok_item, pk=int(ok_item["pk"]))]).status_code == 400        # a JSON number id
    assert post([ok_item], lo_stop="because").status_code == 400
    assert post([ok_item], strategy="li_clips").status_code == 400
    assert post([ok_item], account=None).status_code == 400                       # items need the account first
    r = post([dict(ok_item, cookies="sessionid=1")])
    assert r.status_code == 400 and "cookies" in r.json()["error"].lower()
    body = {"layer": "rented", "items": [ok_item], "account": lo_user(), "paging": {"source": "lo"}}
    assert ext.post(f"/jobs/{jid}/items", body).status_code == 400                # only a server fetcher may say that
    wrong = lo.xig_user(mk.OTHER, followers=5)
    assert post([ok_item], account={"pk": str(wrong["pk"]), "username": wrong["username"]}).status_code == 400
    assert count_reels(member.id) == 0
    other = mk.media(mk.OTHER, TIMELINE[0]["taken_at"], 3, views=10, likes=1, comments=0)
    out = post([ok_item, v1_detail(other), gql_item(other)]).json()               # someone else's reels are dropped
    assert out["accepted"] == 1 and count_reels(member.id) == 1
    # a broken fetch_log never fails an upload
    r = ext.lo_items(jid, [v1_detail(TIMELINE[1])], log=[{"strategy": "x"}, "junk", {"strategy": "lo_embed", "status": "weird"}])
    assert r.status_code == 200 and r.json()["accepted"] == 1


def test_another_members_device_cannot_touch_the_pull(ext, member, other_member, make_device, anon):
    aid, jid, _ = leased(ext, member)
    did2, h2 = make_device(other_member)
    set_version(did2, "2.1.0")
    confirm(did2, "62000111", "other.reader")
    e2 = Ext(anon, h2, did2)
    assert e2.lo_items(jid, [v1_detail(TIMELINE[0])], account=lo_user()).status_code == 404
    assert e2.post(f"/jobs/{jid}/fail", {"code": "lo_blocked", "lo": {"class": "rate"}}).status_code == 404
    assert e2.post(f"/jobs/{jid}/probe", {"rows": load_json("probe_upload.json")["rows"]}).status_code == 404
    assert fetch_of(ext.device_id)["lo_cooldown_until"] is None and count_reels(member.id) == 0
    # the same public reel read by two members is two rows, each with its own numbers and layer
    ext.lo_items(jid, [v1_detail(TIMELINE[0], views="none")], account=lo_user())
    aid2 = add_account(other_member)
    jid2 = jobs_ig.start_pull(other_member.id, aid2, "first")
    assert e2.lease(["ig_pull"])["job"]["id"] == jid2
    e2.lo_items(jid2, [v1_reel(TIMELINE[0])], account=lo_user(), strategy="lo_reels_gql")
    mine, theirs = reel_rows(member.id), reel_rows(other_member.id)
    pk = str(TIMELINE[0]["pk"])
    assert mine[pk].id != theirs[pk].id and mine[pk].partial and not theirs[pk].partial
    assert (mine[pk].views, theirs[pk].views) == (None, TIMELINE[0]["play_count"])
    # health and cooldowns are per device
    e2.lo_items(jid2, [], log=[log_row("lo_reels_gql", "blocked", "rate", 429)], strategy="lo_reels_gql")
    assert fetch_of(did2)["lo_cooldown_until"] and fetch_of(ext.device_id)["lo_cooldown_until"] is None
    with db.tx(immediate=False) as conn:
        owners = {r.device_id: r.user_id for r in conn.execute(select(db.fetch_health)).all()}
    assert owners == {ext.device_id: member.id, did2: other_member.id}


def test_server_fetcher_seam_uses_the_same_ingest(ext, member):
    """Layer 3 (not built): a fetcher calls ingest_lo with no device. Health rows then belong to "server"."""
    aid, jid, _ = leased(ext, member)
    body = {"layer": "rented", "account": lo_user(), "items": [v1_reel(m) for m in TIMELINE[:5]],
            "paging": {"source": "lo", "strategy": "lo_reels_gql"}, "fetch_log": [log_row("lo_reels_gql", items=5)]}
    out = jobs_ig.ingest_lo(member.id, job_row(jid), body, device=None)
    assert out == {"ok": True, "accepted": 5, "known_hit": False, "stop": False}
    assert {r.read_layer for r in reel_rows(member.id).values()} == {"rented"}
    with db.tx(immediate=False) as conn:
        row = conn.execute(select(db.fetch_health)).first()
    assert (row.device_id, row.strategy, row.ok) == ("server", "lo_reels_gql", 1)
    assert fetch_of(ext.device_id)["lo_blocks"] == 0


# ============================================================================================== cooldowns (2.3)

def test_rate_block_doubles_and_resets_after_an_ok(ext, member):
    aid, jid, _ = leased(ext, member)
    rate = [log_row("lo_reels_gql", "blocked", "rate", 429)]
    want = [6, 12, 24, 48, 48]
    for i, h in enumerate(want):
        with db.tx() as conn:        # a later block, not the same one reported twice
            fs = fetch_of(ext.device_id)
            fs["lo_cooldown_until"], fs["lo_cooldown_set_at"] = None, None
            fetch.save_fetch_state(conn, ext.device_id, fs)
        out = ext.lo_items(jid, [], log=rate, strategy=None).json()
        assert h - 0.1 < hours_left(out["lo"]["cooldown_until"]) <= h and out["lo"]["cooldown_reason"] == "rate"
    # the same block reported again within minutes (fetch_log, then fail) does not double it
    ext.lo_items(jid, [], log=rate, strategy=None)
    assert fetch_of(ext.device_id)["lo_blocks"] == 5
    ext.lo_items(jid, [], log=[log_row("lo_profile_page")], strategy=None)        # the first ok
    assert fetch_of(ext.device_id)["lo_blocks"] == 0


def test_login_wall_switches_one_strategy_off_then_all_logged_out_reads(ext, member):
    aid, jid, _ = leased(ext, member)
    out = ext.lo_items(jid, [], log=[log_row("lo_reels_page", "blocked", "login_wall")], strategy=None).json()
    assert list(out["lo"]["strategy_off"]) == ["lo_reels_page"] and out["lo"]["cooldown_until"] is None
    assert 5.9 < hours_left(out["lo"]["strategy_off"]["lo_reels_page"]) <= 6
    hello = ext.ok("/hello", {"version": "2.1.0"})
    assert hello["fetch"]["off"] == ["lo_reels_page"] and "lo_reels_page" in hello["fetch"]["strategies"]
    # a second strategy hits the wall in the same job: all logged-out reads rest for 2 hours
    out = ext.lo_items(jid, [], log=[log_row("lo_reels_gql", "blocked", "login_wall", 401)], strategy=None).json()
    assert sorted(out["lo"]["strategy_off"]) == ["lo_reels_gql", "lo_reels_page"]
    assert 1.9 < hours_left(out["lo"]["cooldown_until"]) <= 2 and out["lo"]["cooldown_reason"] == "login_wall"
    # the wall on one strategy falls back to the login at once (no wait)
    assert ext.stage_done(jid, "blocked")["resume"]["layer"] == "logged_in"
    ext.ok(f"/jobs/{jid}/fail", {"code": "bad_job"})
    # while it rests, the next pull is planned without the logged-out layer
    jid2 = jobs_ig.start_pull(member.id, aid, "first")
    assert ext.lease(["ig_pull"])["job"]["payload"]["plan"] == {"lo": False, "li": True, "lo_budget": 0, "strategies": []}
    # a repeat of the wall inside two days costs 24 hours for that strategy
    fs = fetch_of(ext.device_id)
    fetch.apply_block(fs, "login_wall", "lo_reels_page", db.utcnow() + timedelta(hours=7), job_id="job_other")
    assert 30.9 < hours_left(fs["strategy_off"]["lo_reels_page"]) <= 31


def test_changed_empty_and_no_views_rules(ext, member):
    aid, jid, p = leased(ext, member)
    log = [log_row("lo_posts_gql", "blocked", "changed"),                       # doc_id rotated: off for 24 h
           log_row("lo_media_gql", "partial", "empty"), log_row("lo_media_gql", "partial", "empty"),
           log_row("lo_embed", "partial", "no_data"), log_row("lo_web_profile", "error", "refused", 400),
           log_row("lo_profile_page", "error", "error", 502)]
    off = ext.lo_items(jid, [], log=log, strategy=None).json()["lo"]["strategy_off"]
    assert list(off) == ["lo_posts_gql"] and 23.9 < hours_left(off["lo_posts_gql"]) <= 24
    off = ext.lo_items(jid, [], log=[log_row("lo_media_gql", "partial", "empty")], strategy=None).json()["lo"]["strategy_off"]
    assert 5.9 < hours_left(off["lo_media_gql"]) <= 6                           # empty three times in a row
    # three details in a row without views: the detail strategies rest for pulls (not for the test, not for videos)
    ext.lo_items(jid, [], log=[log_row("lo_post_gql", "partial", "no_views", items=1)] * 3, strategy=None)
    fs = fetch_of(ext.device_id)
    assert 23.9 < hours_left(fs["detail_off_until"]) <= 24 and "lo_post_gql" not in fs["strategy_off"]
    ext.ok(f"/jobs/{jid}/fail", {"code": "bad_job"})
    jid2 = jobs_ig.start_pull(member.id, aid, "first")
    plan = ext.lease(["ig_pull"])["job"]["payload"]["plan"]
    assert plan["lo"] is True and plan["strategies"] == ["lo_profile_page", "lo_reels_page", "lo_reels_gql", "lo_web_profile"]
    with db.tx(immediate=False) as conn:
        h = {r.strategy: (r.ok, r.partial, r.blocked, r.error) for r in conn.execute(select(db.fetch_health)).all()}
    assert h["lo_media_gql"] == (0, 3, 0, 0) and h["lo_profile_page"] == (0, 0, 0, 1) and h["lo_post_gql"] == (0, 3, 0, 0)


@pytest.mark.parametrize("cls,minutes,hours", [("rate", 15, 6), ("challenge", 60, 24)])
def test_lo_blocked_fail_waits_then_goes_on_with_the_login_only(ext, member, cls, minutes, hours):
    aid, jid, _ = leased(ext, member)
    ext.lo_items(jid, [v1_reel(m) for m in TIMELINE[:12]], account=lo_user(), strategy="lo_reels_gql")
    out = ext.ok(f"/jobs/{jid}/fail", {"code": "lo_blocked", "lo": {"class": cls, "strategy": "lo_reels_gql", "http": 429},
                                       "fetch_log": [log_row("lo_reels_gql", "blocked", cls, 429)]})
    assert hours - 0.1 < hours_left(out["lo"]["cooldown_until"]) <= hours and out["lo"]["cooldown_reason"] == cls
    assert "cooldown_until" not in out                 # the logged-in cooldown is untouched
    st = jobs_ig.device_state(device_row(ext.device_id))
    assert st["cooldown_until"] is None and st["signed_in"] is True
    j = job_row(jid)
    wait = (j.run_after - db.utcnow()).total_seconds() / 60
    assert j.state == "waiting" and j.attempts == 0 and minutes - 1 < wait <= minutes
    assert j.payload["_skip_lo"] is True and j.payload["target"] == 88
    assert j.error_message == "Instagram asks ReelVault to wait. It goes on by itself."
    assert ext.lease()["job"] is None                  # no logged-in call before the wait is over
    assert pull_row(jid).layers["logged_out"]["stop"] == "blocked" and pull_row(jid).finished_at is None
    run_now(jid)
    p = ext.lease(["ig_pull"])["job"]["payload"]
    assert p["plan"] == {"lo": False, "li": True, "lo_budget": 0, "strategies": []} and "_skip_lo" not in p
    out = li_pages(ext, jid, 3)
    assert out["stop"] is True and count_reels(member.id) == 100
    ext.ok(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})
    assert pull_row(jid).layer == "mixed" and pull_row(jid).layers["logged_in"]["reels"] == 88


def test_lo_blocked_in_logged_out_only_mode_closes_the_pull(ext, member, run_jobs):
    aid, jid, _ = leased(ext, member, read="logged_out_only")
    ext.lo_items(jid, [v1_reel(m) for m in TIMELINE[:12]], account=lo_user(), strategy="lo_reels_gql")
    out = ext.ok(f"/jobs/{jid}/fail", {"code": "lo_blocked", "lo": {"class": "rate", "strategy": "lo_reels_gql"}})
    assert out["lo"]["cooldown_reason"] == "rate"
    j, pull = job_row(jid), pull_row(jid)
    assert j.state == "done" and j.result["reels_new"] == 12
    assert (pull.stop_reason, pull.layer, pull.finished_at is not None) == ("rate_limited", "logged_out", True)
    assert account_row(aid).status == "active"
    with db.tx(immediate=False) as conn:
        assert conn.execute(select(func.count()).select_from(db.jobs).where(db.jobs.c.kind == "score_account")).scalar() == 1
    # while it cools, a new logged-out-only pull waits with a plain message
    jid2 = jobs_ig.start_pull(member.id, aid, "next")
    assert ext.lease()["job"] is None
    assert job_row(jid2).error_message == "ReelVault waits until Instagram allows reads again."
    assert 5.9 < (job_row(jid2).run_after - db.utcnow()).total_seconds() / 3600 <= 6


def test_extension_cooldown_report_and_repair_keep_the_later_time(ext, member, member_client, anon):
    until = db.iso(db.utcnow() + timedelta(hours=3))
    lz = ext.lease(lo_state={"cooldown_until": until, "strategy_off": {"lo_embed": until, "nope": until}})
    assert lz["lo"]["cooldown_until"] == until and list(lz["lo"]["strategy_off"]) == ["lo_embed"]
    # an earlier time from the extension never shortens the server's
    lz = ext.lease(lo_state={"cooldown_until": db.iso(db.utcnow() + timedelta(hours=1)), "strategy_off": {}})
    assert lz["lo"]["cooldown_until"] == until
    # a silly far-away time is capped at the longest cooldown of the table (48 h)
    lz = ext.lease(lo_state={"cooldown_until": "2099-01-01T00:00:00Z"})
    assert 47.9 < hours_left(lz["lo"]["cooldown_until"]) <= 48
    # disconnect + connect does not clear it
    code = member_client.post("/api/ext/pair", json={}).json()["code"]
    c = anon.post("/api/ext/v2/pair/claim", json={"code": code, "name": "Chrome", "version": "2.1.0", "kind": "extension"})
    e2 = Ext(anon, {"Authorization": f"Bearer {c.json()['token']}"}, c.json()["device_id"])
    assert 47.9 < hours_left(e2.ok("/hello", {"version": "2.1.0"})["lo"]["cooldown_until"]) <= 48


def test_global_switch_off_after_many_failures_and_admin_switch(anon, member, make_user, make_device, admin_client,
                                                                member_client):
    devices = []
    for i in range(3):
        u = member if i == 0 else make_user()
        did, h = make_device(u)
        set_version(did, "2.1.0")
        confirm(did, f"6100{i}", f"reader.{i}")
        e = Ext(anon, h, did)
        aid = add_account(u)
        jid = jobs_ig.start_pull(u.id, aid, "first")
        assert e.lease(["ig_pull"])["job"]["id"] == jid
        devices.append((e, jid))
    wall = [log_row("lo_reels_gql", "blocked", "login_wall", 401)] * 7
    for i, (e, jid) in enumerate(devices):
        e.lo_items(jid, [], log=wall, strategy=None)
        off = fetch.global_off()
        assert ("lo_reels_gql" in off) == (i == 2)          # 21 attempts on 3 devices, none worked
    assert fetch.global_off()["lo_reels_gql"]["reason"] == "auto"
    e, jid = devices[0]
    hello = e.ok("/hello", {"version": "2.1.0"})
    assert "lo_reels_gql" in hello["fetch"]["off"]
    health = admin_client.get("/api/admin/fetch-health").json()
    row = {s["id"]: s for s in health["strategies"]}["lo_reels_gql"]
    assert (row["enabled"], row["off"]["reason"], row["blocked"], row["devices"], row["rate"]) == (False, "auto", 21, 3, 0.0)
    assert len(health["recent_blocks"]) == 20 and health["recent_blocks"][0]["code"] == "login_wall"
    # the admin switches it back on, then another one off for good
    assert member_client.post("/api/admin/fetch-health/strategy", json={"strategy": "lo_reels_gql", "enabled": True}).status_code == 403
    assert admin_client.post("/api/admin/fetch-health/strategy", json={"strategy": "lo_reels_gql", "enabled": True}).json() == {"ok": True}
    assert admin_client.post("/api/admin/fetch-health/strategy", json={"strategy": "lo_embed", "enabled": False}).json() == {"ok": True}
    assert admin_client.post("/api/admin/fetch-health/strategy", json={"strategy": "li_clips", "enabled": False}).status_code == 400
    assert fetch.global_off() == {"lo_embed": {"until": None, "reason": "admin"}}
    # with every list strategy off for everyone, pulls are planned with the login only
    for s in fetch.LIST_STRATEGIES:
        admin_client.post("/api/admin/fetch-health/strategy", json={"strategy": s, "enabled": False})
    e.ok(f"/jobs/{jid}/fail", {"code": "bad_job"})
    with db.tx() as conn:
        fetch.save_fetch_state(conn, e.device_id, fetch.fetch_state(None))
    aid = add_account(member, "second.account")
    jid2 = jobs_ig.start_pull(member.id, aid, "first")
    assert e.lease(["ig_pull"])["job"]["payload"]["plan"]["lo"] is False


# ============================================================================================== scoring

def test_outlier_scoring_is_not_moved_by_reels_without_views(ext, member):
    """A logged-out reel without a view count must never be scored as 0 views (DESIGN-FETCH.md 2.2)."""
    now = db.utcnow()
    aid, jid, _ = leased(ext, member, read="logged_in_only")
    li_pages(ext, jid, 1, size=60)
    ext.ok(f"/jobs/{jid}/done", {"stop_reason": "page_cap"})
    jobs_score.score_account_now(member.id, aid, now=now)
    before = {pk: (r.score, r.baseline, r.band, r.basis) for pk, r in reel_rows(member.id).items()}
    base_before = account_row(aid).baseline_views
    assert sum(1 for v in before.values() if v[0] is not None) >= 40 and base_before
    # 25 more reels arrive logged out WITHOUT views (partial), in a logged-out-only pull
    set_read_mode(member, "logged_out_only")
    jid2 = jobs_ig.start_pull(member.id, aid, "next")
    ext.lease(["ig_pull"])
    ext.lo_items(jid2, [v1_detail(m, views="none") for m in TIMELINE[60:85]])
    ext.stage_done(jid2, "no_views")
    ext.ok(f"/jobs/{jid2}/done", {"stop_reason": "page_cap"})
    jobs_score.score_account_now(member.id, aid, now=now)
    rows = reel_rows(member.id)
    assert len(rows) == 85 and account_row(aid).baseline_views == base_before
    for pk, old in before.items():
        r = rows[pk]
        assert (r.score, r.baseline, r.band, r.basis) == old            # not one score moved
    for m in TIMELINE[60:85]:
        r = rows[str(m["pk"])]
        assert r.views is None and r.partial and r.basis != "views"      # likes or unscored, never "0 views"
        assert r.adj_views is None or r.basis == "likes"


# ============================================================================================== review fix: no fake blocks

def _video_job(ext, member, read="logged_out_only"):
    set_read_mode(member, read)
    aid = add_account(member)
    with db.tx() as conn:
        rid = db.new_id("reel")
        conn.execute(db.reels.insert().values(id=rid, user_id=member.id, account_id=aid, ig_pk=str(TIMELINE[0]["pk"]),
                                              code=TIMELINE[0]["code"], url="u", media_state="need_extension"))
    vid = jobs_ig.request_videos(member.id, [rid])
    lz = ext.lease(["ig_videos"])
    assert lz["job"]["id"] == vid
    return vid, lz["job"]["payload"]


def _blocks():
    with db.tx(immediate=False) as conn:
        return conn.execute(select(func.count()).select_from(db.audit_log)
                            .where(db.audit_log.c.action == "fetch_block")).scalar()


def test_video_items_carry_the_account_handle(ext, member):
    vid, p = _video_job(ext, member)
    assert p["items"][0]["handle"] == "scaleit.demo" and p["plan"]["lo"] is True and p["plan"]["li"] is False
    assert jobs_ig.payload_problem("ig_videos", dict(p, items=[dict(p["items"][0], handle="Bad Handle!")])) == "items"


def test_lo_blocked_with_only_skipped_calls_switches_nothing_off(ext, member):
    """The extension's old fail body for a video job with no lsd token: nothing went to Instagram."""
    vid, _ = _video_job(ext, member)
    out = ext.ok(f"/jobs/{vid}/fail", {"code": "lo_blocked", "lo": {"class": "login_wall", "strategy": "lo_post_gql", "http": 0},
                                       "fetch_log": [log_row("lo_post_gql", "skipped", "no_lsd", 0)]})
    assert out["lo"]["strategy_off"] == {} and not out["lo"]["cooldown_until"]
    assert _blocks() == 0
    fs = fetch.device_fetch_state(device_row(ext.device_id))
    assert fs["strategy_off"] == {} and not fs.get("repeats")
    j = job_row(vid)
    assert j.state == "waiting" and j.attempts == 0 and j.error_message == fetch.MSG_LO_SKIPPED
    assert 29 < (j.run_after - db.utcnow()).total_seconds() / 60 <= 30


def test_lo_blocked_after_a_real_call_still_switches_the_way_off(ext, member):
    vid, _ = _video_job(ext, member)
    out = ext.ok(f"/jobs/{vid}/fail", {"code": "lo_blocked", "lo": {"class": "login_wall", "strategy": "lo_post_gql", "http": 200},
                                       "fetch_log": [log_row("lo_profile_page", "ok", None, 200),
                                                     log_row("lo_post_gql", "blocked", "login_wall", 200)]})
    assert "lo_post_gql" in out["lo"]["strategy_off"] and _blocks() >= 1


def test_video_job_with_nothing_sent_waits_without_a_cooldown(ext, member):
    """The extension now ends such a job with `budget` (no block): it waits, no attempt spent, no cooldown."""
    vid, _ = _video_job(ext, member)
    out = ext.ok(f"/jobs/{vid}/fail", {"code": "budget", "cooldown_s": 3600, "message": "lo_skipped cooling",
                                       "fetch_log": [log_row("lo_post_gql", "skipped", "cooling", 0)]})
    assert out["cooldown_until"] is None and _blocks() == 0
    j = job_row(vid)
    assert j.state == "waiting" and j.attempts == 0
    assert jobs_ig.device_state(device_row(ext.device_id))["cooldown_until"] is None
