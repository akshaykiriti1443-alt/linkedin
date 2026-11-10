"""WP4 API: competitors, pulls with the daily cap, first look, reels, insights, formats, banks, discovery
(DESIGN-SAAS.md 4.8, 4.9, 4.11, 6.0, 6.6.1). Uses WP2's Instagram fixtures and its extension API for pulls."""
from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from reelvault import igparse
from reelvault.server import db, jobs, storage
from reelvault.server import jobs_score as js
from reelvault.server.testing import BASE_URL

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ig"
_spec = importlib.util.spec_from_file_location("rv_mkfx_wp4_api", FIX / "make_fixtures.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)
TIMELINE = mk.account_timeline(250)
HANDLE = mk.TARGET["username"]


def trimmed(raw_list):
    return [igparse.trim_media(m) for m in igparse.parse_ig_json(mk.dumps({"items": raw_list}))["items"]]


def ig_user(u=None, followers=182_000):
    raw = mk.web_profile_info(u or mk.TARGET, followers=followers)["data"]["user"]
    return igparse.trim_user(igparse.parse_ig_json(mk.dumps(raw)))


@pytest.fixture(autouse=True)
def _fast_insights(monkeypatch):
    monkeypatch.setattr(js, "INSIGHTS_DEBOUNCE_S", 0)


@pytest.fixture
def anon(app):
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


class Ext:
    def __init__(self, client, headers, device_id):
        self.c, self.h, self.device_id = client, headers, device_id

    def post(self, path, body=None):
        return self.c.post("/api/ext/v2" + path, json=body if body is not None else {}, headers=self.h)

    def lease(self, kinds):
        r = self.post("/jobs/lease", {"kinds": list(kinds), "budget": {"hour_used": 0, "day_used": 0,
                                                                        "cdn_day_used": 0}})
        assert r.status_code == 200, r.text
        return r.json()["job"]


@pytest.fixture
def ext(anon, member, make_device):
    did, h = make_device(member)
    with db.tx() as conn:
        conn.execute(db.devices.update().where(db.devices.c.id == did).values(
            ig_user_id="61000777", ig_username="member.reader", ig_confirmed_user_id="61000777"))
    return Ext(anon, h, did)


def light(run_jobs):
    return run_jobs(("light",))


def pull_100(ext, member_client, run_jobs, *, handle=HANDLE):
    r = member_client.post("/api/accounts", json={"handles": [f"@{handle}"], "pull": True})
    assert r.status_code == 201, r.text
    aid = r.json()["accounts"][0]["id"]
    job = ext.lease(["ig_pull"])
    assert job and job["kind"] == "ig_pull" and job["payload"]["account_id"] == aid
    for i, off in enumerate((0, 50)):
        body = {"page": i + 1, "paging": {"max_id": mk.cursor_for(off + 50), "more_available": True,
                                          "source": "clips"}, "items": trimmed(TIMELINE[off:off + 50])}
        if i == 0:
            body["account"] = ig_user()
        rr = ext.post(f"/jobs/{job['id']}/items", body)
        assert rr.status_code == 200, rr.text
    assert ext.post(f"/jobs/{job['id']}/done", {"stop_reason": "target_reached"}).status_code == 200
    done = light(run_jobs)
    kinds = [j.kind for j in done]
    assert "score_account" in kinds and "pick_sample" in kinds and "refresh_insights" in kinds, kinds
    return aid


def reels_of(uid):
    with db.read() as conn:
        return conn.execute(select(db.reels).where(db.reels.c.user_id == uid)).all()


# ---------------------------------------------------------------------------------------------- competitors

def test_add_parses_handles_and_schedules_past_the_daily_cap(member_client, member):
    r = member_client.post("/api/accounts", json={"handles": [
        "@alexhormozi", "https://www.instagram.com/garyvee/?igsh=abc", "instagram.com/leila.hormozi",
        "Codie.Sanchez", "@one", "@two", "@three", "@alexhormozi"], "pull": True})
    assert r.status_code == 201, r.text
    d = r.json()
    assert [a["handle"] for a in d["accounts"]] == ["alexhormozi", "garyvee", "leila.hormozi", "codie.sanchez",
                                                     "one", "two", "three"]
    assert len(d["jobs"]) == 7
    today = [j for j in d["jobs"] if not j["run_after"]]
    later = [j for j in d["jobs"] if j["run_after"]]
    assert len(today) == 5 and len(later) == 2
    assert d["schedule_note"] == "We'll read 5 accounts today and 2 tomorrow to keep your Instagram safe."
    g = member_client.get("/api/accounts").json()
    assert g["limits"] == {"max": 10, "pulls_today": 5, "pulls_max": 5}
    st = {a["handle"]: a["status"] for a in g["accounts"]}
    assert st["two"] == "scheduled" and st["three"] == "scheduled" and st["alexhormozi"] == "new"
    assert all(a["pulling"] for a in g["accounts"])
    assert "saves_line" in g


def test_bad_handles_cap_and_your_own_account(member_client, monkeypatch):
    r = member_client.post("/api/accounts", json={"handles": ["not a handle!", "@ok.one"]})
    assert r.status_code == 400 and r.json()["code"] == "bad_input" and "not a handle!" in r.json()["error"]
    assert member_client.post("/api/accounts", json={"handles": []}).status_code == 400
    assert member_client.post("/api/accounts", json={"handles": ["https://instagram.com/reel/abc"]}).status_code == 400
    db.set_setting("max_accounts_per_member", 3)
    assert member_client.post("/api/accounts", json={"handles": ["a1", "a2"], "pull": False}).status_code == 201
    r = member_client.post("/api/accounts", json={"handles": ["a3", "a4"], "pull": False})
    assert r.status_code == 429 and r.json()["code"] == "limit_reached" and "room for 1 more" in r.json()["error"]
    r = member_client.post("/api/accounts", json={"handles": ["@me.myself"], "role": "self", "pull": False})
    assert r.status_code == 201 and r.json()["accounts"][0]["role"] == "self"
    r = member_client.post("/api/accounts", json={"handles": ["@someone.else"], "role": "self"})
    assert r.status_code == 409
    g = member_client.get("/api/accounts").json()
    assert g["self"]["handle"] == "me.myself" and len(g["accounts"]) == 2   # self is never counted as a competitor
    # pull=false asks the extension for the profile only
    with db.read() as conn:
        kinds = [x[0] for x in conn.execute(select(db.jobs.c.kind)).all()]
    assert kinds.count("ig_profile") == 3 and "ig_pull" not in kinds


def test_first_look_from_a_fixture_pull_with_zero_media(ext, member_client, member, run_jobs):
    aid = pull_100(ext, member_client, run_jobs)
    rows = reels_of(member.id)
    assert len(rows) == 100
    assert all(x.media_state in ("none", "queued") and not x.video_path for x in rows)  # nothing downloaded
    assert all(x.scored_at is not None for x in rows)
    planted = {str(TIMELINE[i]["pk"]) for i in (5, 17, 31, 44, 66, 90)}
    outs = {x.ig_pk for x in rows if x.band in ("outlier", "big", "breakout")}
    assert len(planted & outs) >= 5
    home = member_client.get("/api/home").json()
    fl = home["first_look"][0]
    assert fl["account"]["handle"] == HANDLE and fl["outliers"] == len(outs)
    assert fl["line"] == f"Your first look: {len(outs)} outliers from @{HANDLE}"
    assert fl["tags_coming"] is True and fl["reels"][0]["band"] in ("outlier", "big", "breakout")
    assert home["new_outliers"] and home["saves_line"].startswith("Instagram hides saves")
    r = member_client.get("/api/reels", params={"band": "outlier,big,breakout"}).json()
    assert r["counts"]["total"] == len(outs)
    first = r["reels"][0]
    assert first["why"].startswith(f"{first['score']:.1f}"[:3]) or "x this creator's normal views" in first["why"]
    assert "x this creator's normal views" in first["why"] and first["account"]["handle"] == HANDLE
    assert first["states"]["signals"] == "none" and first["states"]["media"] in ("none", "queued")
    assert first["tier"] == "priority" and "remake" in first["actions"]
    assert r["pipeline"]["left_to_tag"] > 0
    assert r["pipeline"]["line"].startswith("Connect your AI to tag")
    acc = member_client.get(f"/api/accounts/{aid}").json()["account"]
    assert acc["normal_line"].startswith("A normal reel here gets about ")
    assert acc["data_note"]["reels_pulled"] == 100 and set(acc["data_note"]["excluded"]) == {
        "too_new", "pinned", "hidden_counts"}
    assert acc["pulls"][0]["reels_new"] == 100 and acc["pulls"][0]["stop_reason"] == "target_reached"
    assert acc["outliers"] == len(outs) and acc["baseline_views"] > 0 and acc["followers"] == 182000
    tiers = {x.tier for x in rows}
    assert tiers <= {"priority", "sample", "rest", "none"} and sum(1 for x in rows if x.tier == "sample") == 25


def test_pull_next_refresh_busy_cap_and_paused(ext, member_client, member, run_jobs):
    aid = pull_100(ext, member_client, run_jobs)
    r = member_client.post(f"/api/accounts/{aid}/pull", json={"mode": "next"})
    assert r.status_code == 200 and r.json()["job"]["kind"] == "ig_pull"
    assert member_client.post(f"/api/accounts/{aid}/pull", json={"mode": "refresh"}).status_code == 409
    assert member_client.post(f"/api/accounts/{aid}/pull", json={"mode": "first"}).status_code == 400
    jobs.cancel(r.json()["job"]["id"], member.id)
    with db.tx() as conn:
        conn.execute(db.accounts.update().where(db.accounts.c.id == aid).values(cursor_done=True))
    r = member_client.post(f"/api/accounts/{aid}/pull", json={"mode": "next"})
    assert r.status_code == 400 and r.json()["error"] == "You already have all reels from this account."
    # past the daily cap the pull is scheduled for tomorrow, never refused
    db.set_setting("max_pulls_per_day", 1)
    r = member_client.post(f"/api/accounts/{aid}/pull", json={"mode": "refresh"})
    assert r.status_code == 200 and r.json()["job"]["run_after"]
    assert r.json()["note"] == "You've done 1 pulls today. This one starts tomorrow."
    jobs.cancel(r.json()["job"]["id"], member.id)
    p = member_client.patch(f"/api/accounts/{aid}", json={"status": "paused", "note": "Good hooks"})
    assert p.status_code == 200 and p.json()["account"]["status"] == "paused"
    assert p.json()["account"]["note"] == "Good hooks"
    assert member_client.post(f"/api/accounts/{aid}/pull", json={"mode": "refresh"}).status_code == 400
    assert member_client.patch(f"/api/accounts/{aid}", json={"status": "active"}).json()["account"]["status"] == \
        "active"
    assert member_client.patch(f"/api/accounts/{aid}", json={"status": "gone"}).status_code == 400


def test_delete_account_with_and_without_reels(ext, member_client, member, run_jobs):
    aid = pull_100(ext, member_client, run_jobs)
    rid = reels_of(member.id)[0].id
    p = storage.member_path(member.id, f"reels/{rid}/cover.jpg")
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xd8x")
    r = member_client.request("DELETE", f"/api/accounts/{aid}", json={"delete_reels": True})
    assert r.status_code == 200 and r.json()["reels_deleted"] == 100
    assert reels_of(member.id) == [] and not p.parent.exists()
    assert member_client.get(f"/api/accounts/{aid}").status_code == 404


def test_delete_account_can_keep_reels(member_client, member):
    aid = member_client.post("/api/accounts", json={"handles": ["keep.me"], "pull": False}).json()["accounts"][0]["id"]
    with db.tx() as conn:
        conn.execute(db.reels.insert().values(id=db.new_id("reel"), user_id=member.id, account_id=aid, ig_pk="77",
                                              code="Ck", url="https://www.instagram.com/reel/Ck/"))
    r = member_client.request("DELETE", f"/api/accounts/{aid}", json={"delete_reels": False})
    assert r.status_code == 200 and r.json()["reels_deleted"] == 0
    lst = member_client.get("/api/reels").json()["reels"]
    assert len(lst) == 1 and lst[0]["account"]["handle"] is None


def test_other_members_ids_are_404_before_the_body_is_read(member_client, other_client):
    aid = member_client.post("/api/accounts", json={"handles": ["mine.only"], "pull": False}).json()["accounts"][0]["id"]
    assert other_client.post(f"/api/accounts/{aid}/pull", json={"mode": "bogus"}).status_code == 404
    assert other_client.patch(f"/api/accounts/{aid}", json={"status": "bogus"}).status_code == 404
    assert other_client.request("DELETE", f"/api/accounts/{aid}", json={"delete_reels": "x"}).status_code == 404
    assert other_client.post("/api/reels/analyze-best", json={"account_id": aid}).status_code == 404


def test_nightly_rescore_when_the_member_looks(ext, member_client, member, run_jobs):
    pull_100(ext, member_client, run_jobs)
    with db.tx() as conn:
        conn.execute(db.reels.update().where(db.reels.c.user_id == member.id)
                     .values(scored_at=db.utcnow() - timedelta(days=2)))
    member_client.get("/api/accounts")
    with db.read() as conn:
        q = conn.execute(select(db.jobs).where(db.jobs.c.kind == "score_account",
                                               db.jobs.c.state == "queued")).all()
    assert len(q) == 1
    member_client.get("/api/home")   # no second job while one waits
    with db.read() as conn:
        n = conn.execute(select(func.count()).select_from(db.jobs).where(
            db.jobs.c.kind == "score_account", db.jobs.c.state == "queued")).scalar()
    assert n == 1


# ---------------------------------------------------------------------------------------------- tags -> insights

RECIPE_HIT = dict(visual_format="VF04", headline_box=True, caption_style="word_by_word_highlight",
                  face_bubble=False, broll_share="none", setting="studio", framing="medium")


def seed_tags(uid):
    """What WP5's save_quick_tags would write, for every priority + sample reel: outliers are green screen."""
    a, r = db.analyses, db.reels
    with db.tx() as conn:
        rows = conn.execute(select(r).where(r.c.user_id == uid, r.c.tier.in_(("priority", "sample")))).all()
        for i, x in enumerate(rows):
            hit = (x.score or 0) >= 3
            t = dict(RECIPE_HIT) if hit else dict(visual_format="VF01", headline_box=False, caption_style="none",
                                                  face_bubble=False, broll_share="light", setting="desk",
                                                  framing="close")
            data = {"reel_id": x.id, "hook_line": "Stop boosting posts." if hit else "Here is my week.",
                    "visual_subject": "creator over a tweet" if hit else "creator at a desk", "one_line": "x"}
            conn.execute(a.insert().values(
                id=db.new_id("ana"), reel_id=x.id, user_id=uid, level="quick", data=data,
                sound_primary="SD06" if hit else "SD01", topics=["ad-creative"] if i % 2 else [],
                hook_type="contrarian" if hit else "story_open", cta_type="comment_keyword" if hit else "follow",
                has_text_hook=hit, cut_pace="fast" if hit else "slow", length_bucket="30-60",
                tag_quality="vision", **t))
            conn.execute(r.update().where(r.c.id == x.id).values(tag_state="done", tag_quality="vision"))
        conn.execute(db.topics.insert().values(id=db.new_id("top"), user_id=uid, slug="ad-creative",
                                               label="Ad creative", status="active"))
    return rows


def test_insights_formats_and_banks_from_tagged_reels(ext, member_client, member, run_jobs):
    pull_100(ext, member_client, run_jobs)
    seed_tags(member.id)
    js.queue_insights(member.id)
    light(run_jobs)
    d = member_client.get("/api/insights", params={"dimension": "visual_format"}).json()
    assert d["snapshot_id"] and d["rows"][0]["value"] == "VF04"
    vf04 = d["rows"][0]
    assert vf04["label"] == "Green screen over image" and vf04["lift"] > 1.5 and vf04["evidence"] == "thin"
    assert d["data_note"]["saves_line"].startswith("Instagram hides saves")
    assert d["data_note"]["reels_pulled"] == 100 and d["data_note"]["sample"] == 25
    assert member_client.get("/api/insights", params={"dimension": "nope"}).status_code == 400
    rec = member_client.get("/api/insights", params={"dimension": "hook_type", "window": "all"}).json()
    assert rec["window"] == "all" and rec["rows"]
    s = member_client.get("/api/insights/summary").json()
    assert s["formats"] and s["formats"][0]["vf"] == "VF04"
    fc = s["formats"][0]
    assert fc["name"] == "Studio green screen over image + top headline box"   # <= 70 chars, whole parts
    assert fc["href"] == f"#format/{fc['id']}" and fc["examples"]
    assert s["per_competitor"][0]["handle"] == HANDLE
    assert s["per_competitor"][0]["line"].startswith(f"@{HANDLE} mostly posts")
    # formats rows + analyses.format_id written; a name job would be queued once WP5 exists
    with db.read() as conn:
        f = conn.execute(select(db.formats).where(db.formats.c.user_id == member.id)).all()
        n_fmt = conn.execute(select(func.count()).select_from(db.analyses).where(
            db.analyses.c.format_id == fc["id"])).scalar()
    assert len(f) == 1 and f[0].stats["active"] and f[0].recipe_key.startswith("VF04|hb=1|cap=word")
    assert n_fmt == f[0].stats["n"] >= 3
    one = member_client.get(f"/api/formats/{fc['id']}").json()["format"]
    assert one["examples"] and one["examples"][0]["band"] in ("outlier", "big", "breakout")
    assert one["can_make"] == {"ok": True, "reason": None} and one["needs"] == ["on_camera"]
    assert "frames" in one["examples"][0] and isinstance(one["music"], list)
    assert member_client.get("/api/formats").json()["formats"][0]["id"] == fc["id"]
    # filters on effective tags
    assert member_client.get("/api/reels", params={"format": "VF04"}).json()["counts"]["total"] == fc["n"]
    assert member_client.get("/api/reels", params={"sub_format": fc["id"]}).json()["counts"]["total"] == fc["n"]
    un = member_client.get("/api/reels", params={"topic": "unsorted"}).json()["reels"]
    assert un and all(x["tags"]["topics"] == [] for x in un)
    tagged = member_client.get("/api/reels", params={"topic": "ad-creative"}).json()["reels"]
    assert tagged and tagged[0]["tags"]["visual_format_label"]
    assert member_client.get("/api/reels", params={"purpose": "remake"}).json()["counts"]["total"] > 0
    assert member_client.get("/api/reels", params={"purpose": "bogus"}).status_code == 400
    # banks
    hooks = member_client.get("/api/banks/hooks").json()["hooks"]
    assert hooks and hooks[0]["level"] == "quick" and hooks[0]["spoken"] == "Stop boosting posts."
    assert all(h["score"] >= 2 for h in hooks)
    assert member_client.get("/api/banks/hooks", params={"level": "deep"}).json()["hooks"] == []
    ctas = member_client.get("/api/banks/ctas").json()["ctas"]
    assert ctas and {c["cta_type"] for c in ctas} <= {"comment_keyword", "follow"}
    snd = member_client.get("/api/banks/sounds").json()["patterns"]
    assert snd[0]["id"] == "SD06" and snd[0]["label"] == "SFX on every cut"
    bf = member_client.get("/api/banks/formats").json()["formats"]
    assert bf[0]["id"] == "VF04" and bf[0]["sub_formats"] == [fc["id"]]
    music = member_client.get("/api/banks/music").json()["tracks"]
    assert any(t["type"] == "licensed" for t in music)
    home = member_client.get("/api/home").json()
    assert home["formats"] and home["formats"][0]["id"] == fc["id"]
    # paging
    p1 = member_client.get("/api/reels", params={"limit": 30}).json()
    p2 = member_client.get("/api/reels", params={"limit": 30, "cursor": p1["next_cursor"]}).json()
    assert len(p1["reels"]) == 30 and p1["next_cursor"] and not {x["id"] for x in p1["reels"]} & \
        {x["id"] for x in p2["reels"]}
    assert member_client.get("/api/reels", params={"cursor": "%%%"}).status_code == 400


def test_can_make_follows_the_profile(ext, member_client, member, run_jobs):
    pull_100(ext, member_client, run_jobs)
    seed_tags(member.id)
    js.queue_insights(member.id)
    light(run_jobs)
    with db.tx() as conn:
        conn.execute(db.profiles.update().where(db.profiles.c.user_id == member.id).values(
            digest={"capabilities": {"on_camera": False, "voice": True, "screen": True}}))
    s = member_client.get("/api/insights/summary").json()
    assert s["formats"][0]["can_make"] == {"ok": False, "reason": "Needs you on camera. You said voice only."}
    assert member_client.get("/api/formats", params={"can_make": 1}).json()["formats"] == []


def test_fix_tags_star_and_analyze(ext, member_client, member, run_jobs):
    pull_100(ext, member_client, run_jobs)
    rows = seed_tags(member.id)
    rid = next(x.id for x in rows if (x.score or 0) < 3)
    bad = member_client.patch(f"/api/reels/{rid}", json={"overrides": {"visual_format": "VF99"}})
    assert bad.status_code == 400
    assert member_client.patch(f"/api/reels/{rid}", json={"overrides": {"topics": ["nope"]}}).status_code == 400
    assert member_client.patch(f"/api/reels/{rid}", json={"overrides": {"price": 1}}).status_code == 400
    r = member_client.patch(f"/api/reels/{rid}", json={"overrides": {"visual_format": "VF05", "topics": ["ad-creative"],
                                                                     "headline_box": True}, "starred": True})
    assert r.status_code == 200, r.text
    reel = r.json()["reel"]
    assert reel["tags"]["visual_format"] == "VF05" and reel["tags"]["quality"] == "member" and reel["starred"]
    full = member_client.get(f"/api/reels/{rid}").json()["reel"]
    assert full["overrides"] == {"visual_format": "VF05", "topics": ["ad-creative"], "headline_box": True}
    assert full["effective"]["visual_format"] == "VF05" and full["quick"]["hook_line"] == "Here is my week."
    assert full["accounts"] == [{"id": rows[0].account_id, "handle": HANDLE, "role": "owner"}]
    assert full["snapshots"] and full["saves_line"]
    # removing an override goes back to the AI value
    r = member_client.patch(f"/api/reels/{rid}", json={"overrides": {"visual_format": None}})
    assert r.json()["reel"]["tags"]["visual_format"] == "VF01"
    with db.read() as conn:
        q = conn.execute(select(db.jobs).where(db.jobs.c.kind == "refresh_insights",
                                               db.jobs.c.state == "queued")).all()
    assert len(q) == 1
    # overrides on a reel with no tags yet create the quick row
    plain = next(x for x in reels_of(member.id) if x.tier == "rest")
    r = member_client.patch(f"/api/reels/{plain.id}", json={"overrides": {"hook_type": "question"}})
    assert r.json()["reel"]["tags"]["hook_type"] == "question"
    a = member_client.post(f"/api/reels/{rid}/analyze", json={})
    assert a.status_code == 200 and a.json()["deep_state"] != "none"
    best = member_client.post("/api/reels/analyze-best", json={"limit": 3}).json()
    assert best["count"] <= 3


def test_your_account_is_never_in_competitor_numbers(ext, member_client, member, run_jobs):
    pull_100(ext, member_client, run_jobs)
    seed_tags(member.id)
    with db.tx() as conn:
        conn.execute(db.accounts.update().where(db.accounts.c.user_id == member.id).values(role="self"))
    data = js.refresh(member.id, store=False)
    assert data["N"] == 0 and data["accounts"] == 0
    g = member_client.get("/api/accounts").json()
    assert g["accounts"] == [] and g["self"]["handle"] == HANDLE


def test_report_numbers_for_a_subset(ext, member_client, member, run_jobs):
    aid = pull_100(ext, member_client, run_jobs)
    seed_tags(member.id)
    assert js.report_numbers(member.id, [aid])["N"] > 0
    assert js.report_numbers(member.id, ["acc_none"])["N"] == 0
    with db.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.insight_snapshots)).scalar() == 1  # nothing stored


# ---------------------------------------------------------------------------------------------- discovery

def test_discover_niche_check_rank_and_track(ext, member_client, member, run_jobs):
    r = member_client.post("/api/discover", json={"mode": "niche"})
    assert r.status_code == 400 and "profile" in r.json()["error"]
    with db.tx() as conn:
        conn.execute(db.profiles.update().where(db.profiles.c.user_id == member.id).values(
            digest={"search_terms": ["dental marketing", "meta ads for dentists!"], "niche_label": "Dentist ads"}))
    r = member_client.post("/api/discover", json={"mode": "niche"})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert r.json()["jobs"][0]["kind"] == "ig_discover"
    job = ext.lease(["ig_discover"])
    assert job["payload"]["queries"] == ["dental marketing", "meta ads for dentists", "Dentist ads"]
    users = [dict(ig_user(u=u, followers=f), source="search") for u, f in
             ((mk.PARTNER, 120_000), (mk.OTHER, 9_000), (mk.TARGET, 50_000))]
    assert ext.post(f"/jobs/{job['id']}/done", {"stop_reason": "no_more", "result": {"users": users}}).status_code == 200
    chk = ext.lease(["ig_check"])
    assert [c["handle"] for c in chk["payload"]["candidates"]] == ["partner.studio", HANDLE, "other.creator"]
    views = {"partner.studio": 84_000, HANDLE: 20_000, "other.creator": 300_000}
    for c in chk["payload"]["candidates"]:
        u = {"partner.studio": mk.PARTNER, HANDLE: mk.TARGET, "other.creator": mk.OTHER}[c["handle"]]
        age0 = 70 if c["handle"] == "other.creator" else 5          # other.creator stopped posting
        raw = [mk.media(u, mk.FIXTURE_NOW - (age0 + 2 * k) * mk.DAY, 3000 + k, views=views[c["handle"]],
                        likes=1000, comments=20) for k in range(12)]
        fol = {"partner.studio": 120_000, HANDLE: 50_000, "other.creator": 9_000}[c["handle"]]
        rr = ext.post(f"/jobs/{chk['id']}/items", {"candidate": {"cand_id": c["cand_id"]},
                                                   "account": ig_user(u=u, followers=fol),
                                                   "items": trimmed(raw), "paging": {"source": "clips",
                                                                                     "max_id": None,
                                                                                     "more_available": False}})
        assert rr.status_code == 200, rr.text
    ext.post(f"/jobs/{chk['id']}/done", {"stop_reason": "no_more"})
    g = member_client.get(f"/api/discover/{run_id}").json()
    assert g["state"] == "done"
    hs = [c["handle"] for c in g["candidates"]]
    assert hs[0] == "partner.studio" and hs[-1] == "other.creator"
    top = g["candidates"][0]
    assert top["median_views_12"] and 84_000 <= top["median_views_12"] <= 100_000
    assert top["why"].startswith("Median ") and "posts 4x a week" in top["why"] and "120K followers" in top["why"]
    assert g["candidates"][-1]["not_posting"] and g["candidates"][-1]["why"].startswith("Not posting lately")
    t = member_client.post(f"/api/discover/{run_id}/track", json={"candidate_ids": [top["id"]]})
    assert t.status_code == 201, t.text
    acc = t.json()["accounts"][0]
    assert acc["handle"] == "partner.studio" and t.json()["jobs"][0]["kind"] == "ig_pull"
    with db.read() as conn:
        row = conn.execute(select(db.accounts).where(db.accounts.c.handle == "partner.studio")).first()
    assert row.ig_pk == str(mk.PARTNER["pk"])
    again = member_client.get(f"/api/discover/{run_id}").json()["candidates"]
    assert "partner.studio" not in [c["handle"] for c in again]   # tracked ones are hidden
    assert member_client.post(f"/api/discover/{run_id}/track", json={"candidate_ids": ["cand_x"]}).status_code == 404


def test_discover_similar_and_search(ext, member_client, member):
    r = member_client.post("/api/discover", json={"mode": "similar"})
    assert r.status_code == 400
    r = member_client.post("/api/discover", json={"mode": "similar", "seeds": ["@alexhormozi", "garyvee"]})
    assert r.status_code == 200
    job = ext.lease(["ig_discover"])
    assert job["payload"]["seeds"] == [{"handle": "alexhormozi"}, {"handle": "garyvee"}]
    jobs.cancel(job["id"], member.id)
    r = member_client.post("/api/discover", json={"mode": "search", "query": "facebook ads agency"})
    assert r.status_code == 200
    assert member_client.post("/api/discover", json={"mode": "search", "query": "!"}).status_code == 400
    assert member_client.post("/api/discover", json={"mode": "everything"}).status_code == 400


# ---------------------------------------------------------------------------------------------- admin hooks

def test_support_mirror_and_community_formats(ext, member_client, member, admin_client, run_jobs):
    pull_100(ext, member_client, run_jobs)
    assert admin_client.get(f"/api/admin/support/{member.id}/reels").status_code == 404
    assert member_client.post("/api/me/support-access", json={"days": 1}).status_code == 200
    r = admin_client.get(f"/api/admin/support/{member.id}/reels", params={"limit": 5})
    assert r.status_code == 200 and len(r.json()["reels"]) == 5
    assert admin_client.get(f"/api/admin/support/{member.id}/insights/summary").status_code == 200
    from reelvault.server import api_reels

    out = api_reels.community_formats()
    assert set(out) == {"month", "formats"}


def test_new_tags_refresh_insights_even_without_a_call(ext, member_client, member, run_jobs):
    """WP5 should call jobs_score.queue_insights after saving tags; opening Home also catches it."""
    pull_100(ext, member_client, run_jobs)
    with db.read() as conn:
        n0 = conn.execute(select(func.count()).select_from(db.insight_snapshots)).scalar()
    assert member_client.get("/api/insights/summary").json()["formats"] == []
    seed_tags(member.id)                       # newer than the snapshot, nobody queued a refresh
    member_client.get("/api/home")
    light(run_jobs)
    with db.read() as conn:
        n1 = conn.execute(select(func.count()).select_from(db.insight_snapshots)).scalar()
    assert n1 == n0 + 1
    assert member_client.get("/api/insights/summary").json()["formats"]
    member_client.get("/api/home")             # nothing new -> nothing queued
    with db.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.jobs).where(
            db.jobs.c.kind == "refresh_insights", db.jobs.c.state == "queued")).scalar() == 0
