"""WP7 profile and topics API (DESIGN-SAAS.md 4.4, 9, 12.2): sources (paste, website, Skool, LinkedIn, Instagram bio,
PDF) incl. the extension page_read round trip, answers, draft merge never overwrites confirmed answers, approve needs
q1/q3/q7/q12, the digest, topics, and the UI copy endpoint."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from reelvault.importers import website
from reelvault.server import db, jobs, work
from reelvault.server.testing import BASE_URL

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def _paired(make_device, user, *, confirmed=True):
    did, h = make_device(user)
    with db.tx() as conn:
        conn.execute(db.devices.update().where(db.devices.c.id == did).values(
            ig_user_id="61000777", ig_username="member.reader",
            ig_confirmed_user_id="61000777" if confirmed else None))
    return did, h


def _src(uid, sid):
    with db.read() as conn:
        return conn.execute(select(db.profile_sources).where(db.profile_sources.c.id == sid,
                                                             db.profile_sources.c.user_id == uid)).first()


FULL = {"q1": {"value": "I help dental clinics get 30 new patients a month"},
        "q3": {"value": ["Ads stop working after two weeks", "Leads never pick up", "Agencies vanish"]},
        "q7": {"value": "Done-for-you Meta ads, £2,500 a month, 3 month minimum"},
        "q12": {"value": ["on_camera", "screen_recording"], "hours": 5, "editor": True, "languages": ["en"]}}


def test_empty_profile_has_the_questions(member_client):
    d = member_client.get("/api/profile").json()
    assert d["status"] == "empty" and d["version"] == 0 and d["sources"] == []
    assert [q["id"] for q in d["questions"]] == [f"q{i}" for i in range(1, 15)]
    q12 = d["questions"][11]
    assert q12["kind"] == "choice" and q12["required"] and q12["choices"][0] == {"id": "on_camera",
                                                                                 "label": "Me on camera"}
    assert set(d["answers"]) == {f"q{i}" for i in range(1, 15)}
    assert d["missing_required"] == ["q1", "q3", "q7", "q12"]


def test_answers_approve_digest_and_edit_back_to_draft(member_client, member):
    r = member_client.post("/api/profile/approve", json={})
    assert r.status_code == 400 and r.json()["error"] == "Answer questions 1, 3, 7 and 12 first."
    r = member_client.put("/api/profile/answers", json={"answers": {"q1": FULL["q1"], "q3": FULL["q3"]}})
    assert r.status_code == 200 and r.json()["status"] == "draft"
    assert r.json()["answers"]["q1"]["source"] == "member" and r.json()["answers"]["q1"]["confirmed"] is True
    assert member_client.post("/api/profile/approve", json={}).json()["detail"]["missing"] == ["q7", "q12"]
    assert member_client.put("/api/profile/answers", json={"answers": {"q99": {"value": "x"}}}).status_code == 400
    bad = member_client.put("/api/profile/answers", json={"answers": {"q12": {"value": ["fly"]}}})
    assert bad.status_code == 400 and "unknown choice" in bad.json()["error"]
    member_client.put("/api/profile/answers", json={"answers": {"q7": FULL["q7"], "q12": FULL["q12"]}})
    a = member_client.post("/api/profile/approve", json={})
    assert a.status_code == 200, a.text
    d = a.json()
    assert d["status"] == "approved" and d["version"] == 1
    dg = d["digest"]
    assert dg["top_pains"] == FULL["q3"]["value"] and dg["capabilities"]["on_camera"] is True
    assert dg["capabilities"]["editor"] is True and dg["capabilities"]["hours_per_week"] == 5
    me = member_client.get("/api/me").json()
    assert me["setup"]["profile"] == "approved"
    # an edit of an approved profile goes back to draft until approved again; the version only grows
    r = member_client.put("/api/profile/answers", json={"answers": {"q5": {"value": "A full calendar"}}})
    assert r.json()["status"] == "draft" and r.json()["digest"]["top_pains"] == FULL["q3"]["value"]
    assert member_client.post("/api/profile/approve", json={}).json()["version"] == 2
    with db.read() as conn:
        aud = conn.execute(select(db.audit_log).where(db.audit_log.c.action == "profile_approved")).all()
    assert len(aud) == 2


def test_paste_source_and_delete(member_client, other_client):
    r = member_client.post("/api/profile/sources", json={"kind": "paste", "text": "  We help dentists.\r\n\r\n\r\nMore. ",
                                                         "label": "My notes"})
    assert r.status_code == 201, r.text
    s = r.json()["source"]
    assert s["status"] == "ok" and s["via"] == "paste" and s["preview"] == "We help dentists.\n\nMore."
    assert s["label"] == "My notes" and "job" not in r.json()
    assert member_client.post("/api/profile/sources", json={"kind": "paste", "text": " "}).status_code == 400
    assert member_client.post("/api/profile/sources", json={"kind": "paste", "text": "x" * 20001}).status_code == 400
    assert member_client.post("/api/profile/sources", json={"kind": "nope"}).status_code == 400
    assert other_client.delete(f"/api/profile/sources/{s['id']}").status_code == 404
    assert other_client.post(f"/api/profile/sources/{s['id']}/paste", json={"text": "x"}).status_code == 404
    assert member_client.delete(f"/api/profile/sources/{s['id']}").json() == {"ok": True}
    assert member_client.get("/api/profile").json()["sources"] == []


def test_website_source_runs_import_source(member_client, member, run_jobs, monkeypatch):
    calls = []

    def fake_fetch(url, **kw):
        calls.append(url)
        return website.SiteResult("ok", text="# Clinic Ads Co\n\n" + "We run ads for clinics. " * 30,
                                  meta={"title": "Clinic Ads Co", "pages": [{"url": url, "chars": 700}]})

    monkeypatch.setattr(website, "fetch_site", fake_fetch)
    r = member_client.post("/api/profile/sources", json={"kind": "website", "url": "clinic-ads.example.com"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"]["status"] == "queued" and body["source"]["url"] == "https://clinic-ads.example.com/"
    assert body["job"]["kind"] == "import_source" and body["job"]["executor"] == "server"
    done = run_jobs(("light",))
    assert [j.kind for j in done] == ["import_source"] and done[0].state == "done"
    s = member_client.get("/api/profile").json()["sources"][0]
    assert s["status"] == "ok" and s["chars"] > 300 and s["title"] == "Clinic Ads Co" and calls
    assert member_client.post("/api/profile/sources", json={"kind": "website", "url": "nope"}).status_code == 400


def test_website_that_needs_paste_then_paste(member_client, member, run_jobs, monkeypatch):
    monkeypatch.setattr(website, "fetch_site", lambda url, **kw: website.SiteResult(
        "needs_paste", text="", meta={}, error=website.NEEDS_PASTE))
    sid = member_client.post("/api/profile/sources", json={"kind": "website", "url": "https://js.example.com"}
                             ).json()["source"]["id"]
    run_jobs(("light",))
    s = member_client.get("/api/profile").json()["sources"][0]
    assert s["status"] == "needs_paste" and s["error"] == "We couldn't read your site. Paste your homepage text."
    r = member_client.post(f"/api/profile/sources/{sid}/paste", json={"text": "We run Meta ads for dentists."})
    assert r.json()["source"]["status"] == "ok" and r.json()["source"]["via"] == "paste"


def test_skool_without_extension_needs_paste_or_server_fetch(member_client, member, run_jobs, monkeypatch):
    r = member_client.post("/api/profile/sources", json={"kind": "skool", "url": "skool.com/clinic-growth-lab"})
    assert r.status_code == 201
    s = r.json()["source"]
    assert s["kind"] == "skool_group" and s["status"] == "needs_paste" and "job" not in r.json()
    assert s["url"] == "https://www.skool.com/clinic-growth-lab/about"
    assert member_client.post("/api/profile/sources", json={"kind": "skool_profile", "url": "x.com/@a"}
                              ).status_code == 400
    assert member_client.post("/api/profile/sources", json={"kind": "skool_group", "via": "server",
                                                            "url": "skool.com/x"}).status_code == 400
    # fallback A: the owner turned server fetch on
    db.set_setting("skool_fetch", True)
    from reelvault.importers import skool

    html = (FIX / "skool" / "group_public.html").read_text()
    monkeypatch.setattr(skool, "fetch_page", lambda url, **kw: skool.parse_html(html))
    r = member_client.post("/api/profile/sources", json={"kind": "skool", "url": "skool.com/clinic-growth-lab"})
    assert r.json()["job"]["kind"] == "import_source" and r.json()["source"]["via"] == "server"
    run_jobs(("light",))
    s = [x for x in member_client.get("/api/profile").json()["sources"] if x["via"] == "server"][0]
    assert s["status"] == "ok" and s["title"] == "Clinic Growth Lab"
    assert {f["kind"] for f in s["found"]} == {"website", "instagram_bio", "linkedin_page"}


def test_skool_page_read_round_trip_through_the_extension(app, member_client, member, make_device):
    did, h = _paired(make_device, member)
    r = member_client.post("/api/profile/sources", json={"kind": "skool_group",
                                                         "url": "https://www.skool.com/clinic-growth-lab/about"})
    assert r.status_code == 201, r.text
    job, src = r.json()["job"], r.json()["source"]
    assert job["kind"] == "page_read" and job["executor"] == "extension" and src["via"] == "extension"
    assert member_client.get("/api/profile").json()["sources"][0]["status"] == "waiting"
    nd = json.loads((FIX / "skool" / "page_read_group.json").read_text())
    with TestClient(app, base_url=BASE_URL) as anon:
        lease = anon.post("/api/ext/v2/jobs/lease", headers=h, json={
            "kinds": ["page_read"], "budget": {"hour_used": 0, "day_used": 0, "cdn_day_used": 0}}).json()
        assert lease["job"]["id"] == job["id"] and lease["job"]["payload"]["kind"] == "skool_group"
        up = anon.post(f"/api/ext/v2/jobs/{job['id']}/page", headers=h, json={
            "final_url": "https://www.skool.com/clinic-growth-lab/about", "title": "Clinic Growth Lab",
            "next_data": nd})
        assert up.status_code == 200, up.text
        assert anon.post(f"/api/ext/v2/jobs/{job['id']}/done", headers=h, json={"stop_reason": "done"}
                         ).status_code == 200
    s = member_client.get("/api/profile").json()["sources"][0]
    assert s["status"] == "ok" and s["preview"].startswith("Skool group: Clinic Growth Lab")
    row = _src(member.id, s["id"])
    assert "Membership questions" in row.text and row.meta["display_name"] == "Clinic Growth Lab"


def test_linkedin_page_and_instagram_bio_use_the_extension(member_client, member, make_device):
    r = member_client.post("/api/profile/sources", json={"kind": "linkedin_page"})
    assert r.json()["source"]["status"] == "needs_paste" and "Chrome extension" in r.json()["source"]["error"]
    r = member_client.post("/api/profile/sources", json={"kind": "instagram_bio", "url": "@Jane.Doe"})
    assert r.json()["source"]["status"] == "needs_paste" and r.json()["source"]["url"] == \
        "https://www.instagram.com/jane.doe/"
    _paired(make_device, member)
    r = member_client.post("/api/profile/sources", json={"kind": "linkedin_page"})
    assert r.json()["job"]["kind"] == "page_read"
    assert jobs.get(r.json()["job"]["id"]).payload == {"source_id": r.json()["source"]["id"],
                                                       "kind": "linkedin_self",
                                                       "url": "https://www.linkedin.com/in/me/"}
    r = member_client.post("/api/profile/sources", json={"kind": "instagram_bio", "url": "instagram.com/jane.doe"})
    j = jobs.get(r.json()["job"]["id"])
    assert j.kind == "ig_profile" and j.payload["purpose"] == "member_bio" and j.payload["handle"] == "jane.doe"
    assert _src(member.id, r.json()["source"]["id"]).status == "waiting"
    assert member_client.post("/api/profile/sources", json={"kind": "instagram_bio", "url": "bad handle!"}
                              ).status_code == 400


def test_linkedin_pdf_upload(member_client, member, run_jobs):
    pdf = (FIX / "web" / "linkedin_profile.pdf").read_bytes()
    r = member_client.post("/api/profile/sources", data={"kind": "linkedin_pdf"},
                           files={"file": ("Profile.pdf", pdf, "application/pdf")})
    assert r.status_code == 201, r.text
    sid = r.json()["source"]["id"]
    assert r.json()["source"]["via"] == "upload" and r.json()["job"]["kind"] == "import_source"
    from reelvault.server import storage

    assert storage.member_path(member.id, f"sources/{sid}.pdf").read_bytes() == pdf
    run_jobs(("light",))
    s = _src(member.id, sid)
    assert s.status == "ok" and "Took Smile Co from 12 to 41" in s.text
    bad = member_client.post("/api/profile/sources", data={"kind": "linkedin_pdf"},
                             files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert bad.status_code == 400 and "not a PDF" in bad.json()["error"]
    big = member_client.post("/api/profile/sources", data={"kind": "linkedin_pdf"},
                             files={"file": ("x.pdf", b"%PDF" + b"0" * (5 * 1024 * 1024), "application/pdf")})
    assert big.status_code == 413
    assert member_client.delete(f"/api/profile/sources/{sid}").status_code == 200
    assert not storage.member_path(member.id, f"sources/{sid}.pdf").exists()


def test_draft_needs_input_and_never_overwrites_confirmed_answers(member_client, member):
    assert member_client.post("/api/profile/draft", json={}).status_code == 400
    member_client.post("/api/profile/sources", json={"kind": "paste", "text": "We run ads for dentists in Leeds."})
    member_client.put("/api/profile/answers", json={"answers": {"q1": {"value": "Dentists in Leeds"}}})
    r = member_client.post("/api/profile/draft", json={})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["kind"] == "draft_profile" and job["state"] == "paused"           # no AI chosen yet
    assert "Settings > AI" in job["error_message"]
    d = member_client.get("/api/profile").json()
    assert d["status"] == "drafting" and d["drafting_job"]["id"] == job["id"]
    # what the member's AI sends back (WP5 save path): q1 is confirmed by the member and must stay
    from reelvault import schema_members as S

    blank = {"value": "", "items": [], "source_ids": [], "confidence": 0.0, "note": ""}
    ans = {f"q{i}": dict(blank) for i in range(1, 15)}
    ans["q1"] = {"value": "Everyone", "items": [], "source_ids": [], "confidence": 0.95, "note": ""}
    ans["q7"] = {"value": "Meta ads for clinics", "items": [], "source_ids": [], "confidence": 0.8, "note": "site"}
    ans["q3"] = {"value": "", "items": ["No patients"], "source_ids": [], "confidence": 0.3, "note": "guess"}
    draft = S.ProfileDraft.model_validate({"answers": ans, "competitor_handles": ["@x"], "niche_label": "Dentist ads",
                                           "search_terms": ["dentist ads"]})
    work.save_profile_draft(member.id, draft, {"via": "api"})
    d = member_client.get("/api/profile").json()
    assert d["answers"]["q1"]["value"] == "Dentists in Leeds" and d["answers"]["q1"]["source"] == "member"
    assert d["answers"]["q7"]["value"] == "Meta ads for clinics" and d["answers"]["q7"]["confirmed"] is False
    assert d["answers"]["q3"]["value"] == "" and d["answers"]["q3"]["suggestion"] == ["No patients"]
    assert d["draft"]["niche_label"] == "Dentist ads" and d["status"] == "draft"
    # confirm-only edit keeps the AI's value
    r = member_client.put("/api/profile/answers", json={"answers": {"q7": {"confirmed": True}}})
    assert r.json()["answers"]["q7"] == dict(d["answers"]["q7"], confirmed=True)
    member_client.put("/api/profile/answers", json={"answers": {"q3": FULL["q3"], "q12": FULL["q12"]}})
    dg = member_client.post("/api/profile/approve", json={}).json()["digest"]
    assert dg["niche_label"] == "Dentist ads" and dg["search_terms"] == ["dentist ads"]


def test_topics_full_list_rename_keeps_slug_and_cap(member_client, member):
    d = member_client.get("/api/topics").json()
    assert d == {"topics": [], "suggested": [], "unsorted_reels": 0, "max": 20}
    r = member_client.put("/api/topics", json={"topics": [
        {"label": "Ad creative", "description": "Reels that show ads that work", "examples": ["3 hooks"]},
        {"label": "Client results", "description": "Shows a result"}]})
    assert r.status_code == 200, r.text
    tops = r.json()["topics"]
    assert [t["slug"] for t in tops] == ["ad-creative", "client-results"]
    assert "retag_job" not in r.json()        # nothing is tagged yet, so no retag job
    # rename + reorder + drop one
    r = member_client.put("/api/topics", json={"topics": [
        {"id": tops[1]["id"], "label": "Patient wins", "description": "Shows a result"}]})
    assert [(t["slug"], t["label"]) for t in r.json()["topics"]] == [("client-results", "Patient wins")]
    # the dropped one is archived; adding it again brings it back
    r = member_client.put("/api/topics", json={"topics": r.json()["topics"] + [{"label": "Ad creative"}]})
    assert [t["slug"] for t in r.json()["topics"]] == ["client-results", "ad-creative"]
    with db.read() as conn:
        assert conn.execute(select(db.topics).where(db.topics.c.user_id == member.id)).all().__len__() == 2
    assert member_client.put("/api/topics", json={"topics": [{"label": f"T {i}"} for i in range(21)]}
                             ).status_code == 400
    assert member_client.put("/api/topics", json={"topics": [{"label": ""}]}).status_code == 400
    assert member_client.put("/api/topics", json={"topics": [{"id": "top_nope", "label": "x y"}]}).status_code == 404
    assert member_client.put("/api/topics", json={"topics": []}).json()["topics"] == []   # 0 topics is fine


def test_topic_suggestions_accept_dismiss_and_retag(member_client, member):
    assert member_client.post("/api/topics/suggest", json={}).status_code == 400
    assert "30 tagged reels" in member_client.post("/api/topics/suggest", json={"from": "reels"}).json()["error"]
    member_client.put("/api/profile/answers", json={"answers": {"q1": FULL["q1"]}})
    r = member_client.post("/api/topics/suggest", json={"from": "profile"})
    assert r.status_code == 200 and r.json()["job"]["kind"] == "suggest_topics"
    from reelvault import schema_members as S

    work.save_topic_list(member.id, S.TopicList.model_validate({"topics": [
        {"slug": "ad-creative", "label": "Ad creative", "description": "shows ads", "examples": []},
        {"slug": "canva", "label": "Canva", "description": "shows canva", "examples": []}]}), "profile")
    d = member_client.get("/api/topics").json()
    assert [t["slug"] for t in d["suggested"]] == ["ad-creative", "canva"] and d["suggested"][0]["seen_in"] == 0
    # a tagged reel so the retag has work
    with db.tx() as conn:
        conn.execute(db.accounts.insert().values(id="acc_t1", user_id=member.id, handle="someone"))
        conn.execute(db.reels.insert().values(id="reel_t1", user_id=member.id, account_id="acc_t1", ig_pk="1",
                                              code="C1", url="https://www.instagram.com/reel/C1/", tag_state="done"))
        conn.execute(db.analyses.insert().values(id="ana_t1", reel_id="reel_t1", user_id=member.id, level="quick",
                                                 data={"new_topic": "new:Canva"}, topics=[]))
    d = member_client.get("/api/topics").json()
    assert d["unsorted_reels"] == 1 and {t["slug"]: t["seen_in"] for t in d["suggested"]}["canva"] == 1
    r = member_client.post("/api/topics/accept", json={"ids": [d["suggested"][0]["id"]],
                                                       "dismiss": [d["suggested"][1]["id"]]})
    assert [t["slug"] for t in r.json()["topics"]] == ["ad-creative"] and r.json()["suggested"] == []
    assert r.json()["retag_job"]["kind"] == "retag_topics"


def test_copy_endpoint_fills_the_mcp_url_and_admin_overrides(member_client, admin_client):
    c = member_client.get("/api/copy").json()
    assert "defaults" not in c
    steps = c["copy"]["connector_steps"]
    assert len(steps) == 6 and "http://localhost/mcp" in steps[2] and steps[1].startswith("Click **+ Add**")
    assert "Always allow" in steps[5] and "Sign in now" in steps[3]   # fix round 2: Claude's current flow
    assert c["copy"]["claude_code_command"] == "claude mcp add --transport http --scope user reelvault http://localhost/mcp"
    assert admin_client.put("/api/admin/copy", json={"connector_steps": ["Open Claude.", "Add {MCP_URL}."]}
                            ).status_code == 200
    c = member_client.get("/api/copy").json()["copy"]
    assert c["connector_steps"] == ["Open Claude.", "Add http://localhost/mcp."]
    a = admin_client.get("/api/copy").json()
    assert "{MCP_URL}" in a["defaults"]["connector_steps"][2]


def test_source_cap(member_client, monkeypatch):
    from reelvault.server import api_profile

    monkeypatch.setattr(api_profile, "MAX_SOURCES", 2)
    for i in range(2):
        assert member_client.post("/api/profile/sources", json={"kind": "paste", "text": f"t{i}"}).status_code == 201
    r = member_client.post("/api/profile/sources", json={"kind": "paste", "text": "t3"})
    assert r.status_code == 400 and "20 sources" in r.json()["error"]


def test_read_again_after_pairing_and_for_a_failed_website(member_client, other_client, member, make_device,
                                                            run_jobs, monkeypatch):
    """WP7 addition: links added before Chrome is connected wait as needs_paste; "Read my links now" re-reads them."""
    sk = member_client.post("/api/profile/sources", json={"kind": "skool", "url": "skool.com/@jane-doe"}).json()
    li = member_client.post("/api/profile/sources", json={"kind": "linkedin_page"}).json()
    ig = member_client.post("/api/profile/sources", json={"kind": "instagram_bio", "url": "@jane.doe"}).json()
    pa = member_client.post("/api/profile/sources", json={"kind": "paste", "text": "I run Meta ads for clinics."}).json()
    assert sk["source"]["status"] == "needs_paste"
    # not paired yet: plain refusal; another member's id: 404 before anything else
    r = member_client.post(f"/api/profile/sources/{sk['source']['id']}/read", json={})
    assert r.status_code == 400 and "Chrome extension" in r.json()["error"]
    assert other_client.post(f"/api/profile/sources/{sk['source']['id']}/read", json={}).status_code == 404
    assert member_client.post(f"/api/profile/sources/{pa['source']['id']}/read", json={}).status_code == 400
    _paired(make_device, member)
    kinds = {}
    for s in (sk, li, ig):
        r = member_client.post(f"/api/profile/sources/{s['source']['id']}/read", json={})
        assert r.status_code == 200, r.text
        kinds[s["source"]["kind"]] = jobs.get(r.json()["job"]["id"])
        assert r.json()["source"]["status"] == "waiting"
    assert kinds["skool_profile"].kind == "page_read" and kinds["skool_profile"].payload["kind"] == "skool_profile"
    assert kinds["linkedin_page"].payload["kind"] == "linkedin_self"
    assert kinds["instagram_bio"].kind == "ig_profile" and kinds["instagram_bio"].payload["handle"] == "jane.doe"
    # already waiting: busy
    assert member_client.post(f"/api/profile/sources/{sk['source']['id']}/read", json={}).status_code == 409

    # a website that failed can be read again on the server
    calls = []

    def fake(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            return website.SiteResult("needs_paste", error=website.NEEDS_PASTE)
        return website.SiteResult("ok", text="We run Meta ads for dental clinics. " * 20, meta={"title": "Clinic Ads"})

    monkeypatch.setattr(website, "fetch_site", fake)
    w = member_client.post("/api/profile/sources", json={"kind": "website", "url": "clinicads.example"}).json()
    run_jobs()
    assert _src(member.id, w["source"]["id"]).status == "needs_paste"
    r = member_client.post(f"/api/profile/sources/{w['source']['id']}/read", json={})
    assert r.status_code == 200 and r.json()["source"]["status"] == "queued"
    run_jobs()
    assert _src(member.id, w["source"]["id"]).status == "ok" and len(calls) == 2


def test_voice_words_drop_trailing_punctuation():
    from reelvault.importers import merge

    d = merge.build_digest({"q10": {"value": ["direct"], "text": "Always: patients. Never: leads."}})
    assert d["voice"]["always"] == ["patients"] and d["voice"]["never"] == ["leads"]
