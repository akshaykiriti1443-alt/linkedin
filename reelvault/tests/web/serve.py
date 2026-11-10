"""Seeded ReelVault Members server for the web UI tests (WP7). Not a test module.

    python tests/web/serve.py --port 9241 --data-dir /tmp/somewhere [--fake-ai-port 9251]

Seeds, through the real APIs where it can (extension API for pulls, jobs for scoring/insights, WP5's fake
OpenAI-compatible server for outputs):
- admin (printed), a community code, and member@example.com with: a paired + confirmed extension, two pulled
  competitors (WP2 fixtures), quick tags (outliers = green screen + headline box), deep reads + signals + video for
  the top 3 outliers, covers, topics, an approved profile, insights + named formats, a discovery run with
  candidates, and one finished output of each kind.
Then serves web + worker (role "all") on --port, prints one line `READY {json}` and runs until SIGTERM.
All made-up data; nothing leaves this machine (the fake AI listens on 127.0.0.1).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "server"))

FIX = ROOT / "tests" / "fixtures" / "ig"
MEMBER_PASSWORD = "member-pass-123"


def _mk():
    spec = importlib.util.spec_from_file_location("rv_mkfx_wp7_web", FIX / "make_fixtures.py")
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    return mk


def _cover(path: Path, i: int) -> None:
    import cv2
    import numpy as np

    h, w = 400, 300
    img = np.zeros((h, w, 3), np.uint8)
    hues = [(70, 40, 200), (40, 140, 220), (180, 90, 40), (60, 160, 90), (150, 60, 160), (40, 60, 90)]
    c = hues[i % len(hues)]
    for y in range(h):
        img[y, :] = [int(v * (0.55 + 0.45 * y / h)) for v in c]
    cv2.rectangle(img, (20, 30), (280, 90), (250, 250, 250), -1)
    cv2.putText(img, f"REEL {i + 1}", (40, 75), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (20, 20, 20), 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 70])


def seed(fake_port: int) -> dict:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    import _wp5_support as w5
    from reelvault import igparse
    from reelvault.importers import merge
    from reelvault.server import db, dev, jobs, storage, work
    from reelvault.server import jobs_score as js
    from reelvault.server.app import create_app

    mk = _mk()
    info = dev.seed_member(MEMBER_PASSWORD)
    uid = info["member_id"]
    with db.read() as conn:
        if conn.execute(select(db.accounts.c.id).where(db.accounts.c.user_id == uid)).first():
            return {"seeded": "already"}
    js.INSIGHTS_DEBOUNCE_S = 0
    db.set_setting("allow_local_ai_urls", True)
    db.set_setting("signup_mode", "pending")
    app = create_app("web", prepare=False)
    out: dict = {}
    with TestClient(app, base_url="http://localhost") as c:
        r = c.post("/api/auth/login", json={"email": info["member_email"], "password": MEMBER_PASSWORD})
        assert r.status_code == 200, r.text
        c.headers["X-CSRF-Token"] = r.json()["user"]["csrf_token"]

        # ---- profile (approved) -------------------------------------------------------------------------
        answers = {
            "q1": {"value": "I help dental clinics get 30 new patients a month with Meta ads"},
            "q3": {"value": ["My ads stop working after two weeks", "Leads never pick up the phone",
                             "Agencies take my money and vanish"]},
            "q5": {"value": "A full calendar and 2 new chairs within a year"},
            "q7": {"value": "Done-for-you Meta ads for clinics, £2,500 a month, 3 month minimum"},
            "q8": {"value": "Smile Co: 12 to 41 new patients a month in 90 days"},
            "q10": {"value": ["direct", "friendly"], "text": "Always: patients. Never: leads."},
            "q11": {"value": "Instagram, 4,000 followers, 3 posts a week. Comment AUDIT for a free audit."},
            "q12": {"value": ["on_camera", "screen_recording"], "hours": 5, "editor": False, "languages": ["en"]},
        }
        assert c.put("/api/profile/answers", json={"answers": answers}).status_code == 200
        c.post("/api/profile/sources", json={"kind": "paste", "label": "About me",
                                             "text": "Clinic Ads Co runs Meta ads for UK dental clinics."})
        assert c.post("/api/profile/approve", json={}).status_code == 200

        # ---- extension paired + confirmed ---------------------------------------------------------------
        from reelvault.server import security

        token, th = security.new_token("rvd")
        did = db.new_id("dev")
        with db.tx() as conn:
            conn.execute(db.devices.insert().values(
                id=did, user_id=uid, kind="extension", name="Chrome on Windows", token_hash=th, version="2.0.0",
                ig_user_id="61000777", ig_username="member.reader", ig_confirmed_user_id="61000777",
                state=dict(db.DEVICE_STATE_DEFAULT, signed_in=True), created_at=db.utcnow(),
                last_seen_at=db.utcnow()))
        h = {"Authorization": f"Bearer {token}"}

        def ext(path, body):
            rr = c.post("/api/ext/v2" + path, json=body, headers=h)
            assert rr.status_code == 200, (path, rr.text)
            return rr.json()

        def trimmed(raw_list):
            return [igparse.trim_media(m) for m in igparse.parse_ig_json(mk.dumps({"items": raw_list}))["items"]]

        def ig_user(u, followers):
            raw = mk.web_profile_info(u, followers=followers)["data"]["user"]
            return igparse.trim_user(igparse.parse_ig_json(mk.dumps(raw)))

        # ---- two competitors pulled through the extension API -------------------------------------------
        for owner, followers, seed_n in ((mk.TARGET, 182_000, 7), (mk.OTHER, 64_000, 11)):
            timeline = mk.account_timeline(120, owner=owner, seed=seed_n)
            r = c.post("/api/accounts", json={"handles": [f"@{owner['username']}"], "pull": True})
            assert r.status_code == 201, r.text
            lease = ext("/jobs/lease", {"kinds": ["ig_pull"], "budget": {"hour_used": 0, "day_used": 0,
                                                                         "cdn_day_used": 0}})
            job = lease["job"]
            for i, off in enumerate((0, 50)):
                body = {"page": i + 1, "paging": {"max_id": mk.cursor_for(off + 50), "more_available": True,
                                                  "source": "clips"}, "items": trimmed(timeline[off:off + 50])}
                if i == 0:
                    body["account"] = ig_user(owner, followers)
                ext(f"/jobs/{job['id']}/items", body)
            ext(f"/jobs/{job['id']}/done", {"stop_reason": "target_reached"})
            jobs.drain(("light",))

        # ---- quick tags (what the member's AI would write) ----------------------------------------------
        a, rl = db.analyses, db.reels
        recipe_hit = dict(visual_format="VF04", headline_box=True, caption_style="word_by_word_highlight",
                          face_bubble=False, broll_share="none", setting="studio", framing="medium")
        recipe_b = dict(visual_format="VF05", headline_box=False, caption_style="sentence", face_bubble=True,
                        broll_share="none", setting="screen", framing="close")
        with db.tx() as conn:
            rows = conn.execute(select(rl).where(rl.c.user_id == uid, rl.c.tier.in_(("priority", "sample")))
                                .order_by(rl.c.score.desc().nullslast())).all()
            for i, x in enumerate(rows):
                hit = (x.score or 0) >= 3
                t = dict(recipe_hit) if (hit and i % 3 != 2) else dict(recipe_b) if hit else dict(
                    visual_format="VF01", headline_box=False, caption_style="none", face_bubble=False,
                    broll_share="light", setting="desk", framing="close")
                data = {"reel_id": x.id, "hook_line": "Stop boosting posts." if hit else "Here is my week.",
                        "visual_subject": "creator over a tweet" if hit else "creator at a desk",
                        "one_line": "Shows an ad that brought patients in" if hit else "A day in the office",
                        "hook_bank": {"spoken": "Stop boosting posts. Do this instead." if hit else "Here is my week.",
                                      "on_screen_text": "STOP BOOSTING" if hit else "", "visual": "face + tweet",
                                      "hook_type": "contrarian" if hit else "story_open", "level": "quick"}}
                conn.execute(a.insert().values(
                    id=db.new_id("ana"), reel_id=x.id, user_id=uid, level="quick", data=data,
                    sound_primary="SD06" if hit else "SD01", topics=["ad-creative"] if i % 2 else [],
                    hook_type="contrarian" if hit else "story_open", cta_type="comment_keyword" if hit else "follow",
                    has_text_hook=hit, cut_pace="fast" if hit else "slow", length_bucket="30-60",
                    tag_quality="vision", **t))
                conn.execute(rl.update().where(rl.c.id == x.id).values(tag_state="done", tag_quality="vision",
                                                                       cover_path=f"reels/{x.id}/cover.jpg"))
            for i, (slug, label) in enumerate((("ad-creative", "Ad creative"), ("patient-results", "Patient results"))):
                conn.execute(db.topics.insert().values(id=db.new_id("top"), user_id=uid, brand_id="me", slug=slug,
                                                       label=label, description=f"Reels that show {label.lower()}",
                                                       status="active", position=i, source="member",
                                                       created_at=db.utcnow()))
            conn.execute(db.topics.insert().values(id=db.new_id("top"), user_id=uid, brand_id="me",
                                                   slug="canva-templates", label="Canva templates",
                                                   description="Shows a Canva template", status="suggested",
                                                   position=9, source="ai_from_reels", created_at=db.utcnow()))
        for i, x in enumerate(rows):
            _cover(storage.member_path(uid, f"reels/{x.id}/cover.jpg"), i)

        # ---- deep reads + signals + video for the top outliers ------------------------------------------
        top = [x.id for x in rows if (x.score or 0) >= 3][:3]
        clips = None
        try:
            for rid in top:
                w5.give_signals(uid, rid)
                if clips is None:
                    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
                    import make_media

                    clips = make_media.ensure(ROOT / "tests" / "fixtures" / "media")
                shutil.copy(clips["talking_head"], storage.member_path(uid, f"reels/{rid}/video.mp4"))
                jid = jobs.enqueue(uid, "analyze_reel", {"reel_id": rid}, executor="desktop")
                with db.read() as conn:
                    jrow = conn.execute(select(db.jobs).where(db.jobs.c.id == jid)).first()
                work.save_result(uid, jrow, w5.breakdown(rid), {"via": "desktop", "model": "fake"})
                with db.tx() as conn:
                    conn.execute(rl.update().where(rl.c.id == rid).values(cover_path=f"reels/{rid}/cover.jpg"))
                _cover(storage.member_path(uid, f"reels/{rid}/cover.jpg"), top.index(rid))
        except Exception as e:  # noqa: BLE001 - media tools missing: the UI still works without deep reads
            print(f"seed: deep reads skipped ({e})", file=sys.stderr)
        js.queue_insights(uid)
        jobs.drain(("light",))
        out["top_reels"] = top

        # ---- a discovery run with candidates (what ig_discover + ig_check would leave) -----------------
        run_id = db.new_id("drun")
        with db.tx() as conn:
            conn.execute(db.discover_runs.insert().values(id=run_id, user_id=uid, mode="similar", queries=[],
                                                          seeds=[{"handle": mk.TARGET["username"]}], state="done",
                                                          created_at=db.utcnow()))
            for i, (hd, fn, fol, med, ppw) in enumerate((
                    ("dental.growth.co", "Dental Growth Co", 120_000, 84_000, 4.0),
                    ("smile.marketing", "Smile Marketing", 48_000, 51_000, 3.0),
                    ("clinic.ads.lab", "Clinic Ads Lab", 22_000, 30_500, 5.0))):
                conn.execute(db.discover_candidates.insert().values(
                    id=db.new_id("cand"), user_id=uid, run_id=run_id, handle=hd, pk=str(70000000 + i),
                    full_name=fn, followers=fol, is_private=False, is_verified=False, source="similar",
                    seed_handle=mk.TARGET["username"], median_views_12=med, posts_per_week=ppw,
                    last_post_at=db.utcnow(), rank_score=5.0 - i, checked_at=db.utcnow(), status="checked"))
        out["discover_run"] = run_id

        # ---- the member's AI: WP5's fake OpenAI-compatible server --------------------------------------
        w5.PORTS = range(fake_port, fake_port + 1)
        fake = w5.FakeAI()
        conn_out = w5.add_fake_connection(c, fake)
        out["fake_ai_port"] = fake.port
        out["fake_ai"] = fake
        made = {}
        for kind, opts in (("meta_ads", {"count": 1, "offer": "free audit"}),
                           ("content_plan", {"posts_per_week": 3}),
                           ("competitor_report", {})):
            rr = c.post("/api/outputs", json={"kind": kind, "options": opts})
            if rr.status_code == 201:
                made[kind] = rr.json()["output"]["id"]
            else:
                print(f"seed: {kind} not created: {rr.text}", file=sys.stderr)
        if top:
            rr = c.post("/api/outputs/quick", json={"action": "remake", "reel_id": top[0]})
            if rr.status_code == 201:
                made["reel_script"] = rr.json()["output"]["id"]
        for _ in range(12):
            if not jobs.drain(("ai", "light")):
                break
        out["outputs"] = made
        out["connection"] = conn_out.get("connection", {}).get("id")
        with db.tx() as conn:   # media jobs would wait for a CDN that does not exist here
            conn.execute(db.jobs.update().where(db.jobs.c.user_id == uid, db.jobs.c.job_class == "media",
                                                db.jobs.c.state.in_(("queued", "running"))).values(
                state="canceled", finished_at=db.utcnow(), dedupe_key=None))
        merge  # noqa: B018 - imported to fail fast when WP7 importers are broken
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--fake-ai-port", type=int, default=0)
    a = ap.parse_args(argv)
    if not 9240 <= a.port <= 9259:
        raise SystemExit("WP7 web tests use ports 9240-9259.")
    from reelvault.server import api_admin, db, dev

    info = dev.configure(a.port, a.data_dir)
    import uvicorn

    from reelvault.server.app import create_app, prepare_runtime

    prepare_runtime()
    seeded = seed(a.fake_ai_port or (a.port + 10))
    with db.tx() as conn:
        info["community_code"] = api_admin.create_community_code(conn)
    info.update({"member_email": dev.MEMBER_EMAIL, "member_password": MEMBER_PASSWORD, "port": a.port})
    info.update({k: v for k, v in seeded.items() if k != "fake_ai"})
    print("READY " + json.dumps(info), flush=True)

    def _exit(*_a):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _exit)
    try:
        uvicorn.run(create_app("all", prepare=False), host="127.0.0.1", port=a.port, log_level="warning")
    finally:
        fake = seeded.get("fake_ai")
        if fake is not None:
            fake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
