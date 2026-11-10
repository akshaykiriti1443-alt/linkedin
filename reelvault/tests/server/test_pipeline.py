"""WP5: pipeline.advance (6.0), tag batches at lease time (3.13.2), executors (3.13.1), topic retags (6.5)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from _wp5_support import add_account, add_reel, add_topics, fake_answer, give_signals, set_mode

from reelvault.server import db, jobs, pipeline, work


def reel(rid):
    with db.read() as c:
        return c.execute(select(db.reels).where(db.reels.c.id == rid)).first()


def job(jid):
    with db.read() as c:
        return c.execute(select(db.jobs).where(db.jobs.c.id == jid)).first()


def active(uid, kind=None):
    with db.read() as c:
        q = select(db.jobs).where(db.jobs.c.user_id == uid, db.jobs.c.state.in_(db.ACTIVE_JOB_STATES))
        if kind:
            q = q.where(db.jobs.c.kind == kind)
        return c.execute(q).all()


def test_wanted_reel_gets_fetch_then_process(rv_env, member):
    aid = add_account(member.id)
    pri = add_reel(member.id, aid, tier="priority")
    rest = add_reel(member.id, aid, tier="rest")
    j1 = pipeline.advance(member.id, pri)
    assert job(j1).kind == "fetch_video" and reel(pri).media_state == "queued"
    assert pipeline.advance(member.id, pri) == j1  # dedupe: same active job
    assert pipeline.advance(member.id, rest) is None and reel(rest).media_state == "none"
    with db.tx() as c:
        c.execute(db.reels.update().where(db.reels.c.id == pri).values(media_state="have"))
    j2 = pipeline.advance(member.id, pri)
    assert job(j2).kind == "process_reel" and job(j2).payload["words"] is True
    samp = add_reel(member.id, aid, tier="sample", media_state="have")
    assert job(pipeline.advance(member.id, samp)).payload["words"] is False
    assert reel(samp).signals_state == "queued"


def test_need_extension_waits_and_failed_signals_tag_measured(rv_env, member):
    aid = add_account(member.id)
    r1 = add_reel(member.id, aid, media_state="need_extension")
    assert pipeline.advance(member.id, r1) is None
    r2 = add_reel(member.id, aid, media_state="have", signals_state="failed")
    slot = pipeline.advance(member.id, r2)
    assert reel(r2).tag_state == "queued" and job(slot).kind == "tag_reels"
    assert job(slot).state == "paused" and job(slot).error_code == "no_ai"  # no AI yet: waits, plain message
    assert job(slot).error_message == "Choose how ReelVault uses AI in Settings > AI."


def test_no_analyze_before_signals(rv_env, member):
    aid = add_account(member.id)
    rid = add_reel(member.id, aid, tier="rest")
    set_mode(member.id, "desktop")
    jids = work.queue_deep(member.id, [rid])
    assert [job(j).kind for j in jids] == ["fetch_video"] and reel(rid).deep_state == "queued"
    assert not active(member.id, "analyze_reel")
    give_signals(member.id, rid)
    jid = pipeline.advance(member.id, rid)
    assert job(jid).kind == "analyze_reel" and job(jid).executor == "desktop" and job(jid).state == "waiting"
    r = reel(rid)
    assert r.deep_state == "waiting" and r.tag_state == "queued"
    assert work.queue_deep(member.id, [rid]) == [jid]  # asking again finds the same waiting job


def test_deep_words_later(rv_env, member):
    aid = add_account(member.id)
    rid = add_reel(member.id, aid, tier="sample")
    give_signals(
        member.id,
        rid,
        transcript={
            "text": "hi there",
            "segments": [{"start": 0, "end": 1, "text": "hi"}],
            "words": [],
            "model": "base",
            "speech_spans": [],
        },
    )
    set_mode(member.id, "desktop")
    jid = work.queue_deep(member.id, [rid])[0]
    assert job(jid).kind == "process_reel" and job(jid).payload["words"] is True
    assert reel(rid).signals_state == "queued"


def test_dedupe_rerun_after_done(rv_env, member):
    aid = add_account(member.id)
    rid = add_reel(member.id, aid, tier="priority")
    j1 = pipeline.advance(member.id, rid)
    jobs.complete(j1, {"ok": True})
    with db.tx() as c:
        c.execute(db.reels.update().where(db.reels.c.id == rid).values(media_state="none"))
    j2 = pipeline.advance(member.id, rid)
    assert j2 and j2 != j1 and job(j2).kind == "fetch_video"


def test_executor_rules_and_mode_change_moves_jobs(rv_env, member, member_client):
    uid = member.id
    assert work.ai_executor(uid, "tag_reels") is None
    jid = work.enqueue_ai(uid, "draft_profile", {})
    assert job(jid).state == "paused" and job(jid).error_code == "no_ai"
    set_mode(uid, "claude_connector")
    assert work.ai_executor(uid, "analyze_reel") == "mcp"
    r = member_client.put("/api/ai/mode", json={"mode": "desktop"})
    assert r.status_code == 200 and r.json()["mode"] == "desktop"
    j = job(jid)
    assert j.executor == "desktop" and j.state == "waiting" and j.job_class == "external" and j.error_code is None
    r = member_client.put("/api/ai/mode", json={"mode": "api"})
    assert r.status_code == 400 and "API key" in r.json()["error"]
    cid = db.new_id("aic")
    with db.tx() as c:
        c.execute(db.ai_connections.insert().values(id=cid, user_id=uid, provider="gemini", model="m"))
        c.execute(db.users.update().where(db.users.c.id == uid).values(tagging_conn_id=cid))
    assert work.ai_executor(uid, "tag_reels") == "server" and work.ai_executor(uid, "analyze_reel") == "desktop"


def test_tag_batch_formed_at_lease_time_and_expiry_returns_reels(rv_env, member, make_device, client):
    from reelvault.server import jobs_ai  # noqa: F401  (registers the lease-expiry hook)

    uid = member.id
    set_mode(uid, "desktop")
    aid = add_account(uid)
    rids = [add_reel(uid, aid, tier="priority", score=s) for s in (2.0, 9.0, 5.0)]
    for r in rids:
        give_signals(uid, r)
        pipeline.advance(uid, r)
    slots = active(uid, "tag_reels")
    assert len(slots) == 1 and slots[0].payload == {} and slots[0].executor == "desktop"
    dev, hdr = make_device(member, "desktop")
    got = client.post("/api/worker/lease", json={"kinds": ["tag_reels"]}, headers=hdr).json()["job"]
    ids = got["task"]["prompt"]
    payload = job(got["id"]).payload["reel_ids"]
    assert payload[0] == rids[1] and set(payload) == set(rids)  # best score first, all 3 (batch 6)
    assert all(reel(r).tag_state == "leased" and reel(r).tag_batch == got["id"] for r in rids)
    assert rids[1] in ids
    with db.tx() as c:  # the lease runs out
        c.execute(db.jobs.update().where(db.jobs.c.id == got["id"]).values(lease_until=db.utcnow() - timedelta(1)))
    assert jobs.requeue_expired() == 1
    assert all(reel(r).tag_state == "queued" and reel(r).tag_batch is None for r in rids)
    j = job(got["id"])
    assert j.state == "waiting" and "reel_ids" not in j.payload


def test_save_tags_needs_exact_batch_and_retag_clears_flags(rv_env, member, make_device, client):
    uid = member.id
    set_mode(uid, "desktop")
    aid = add_account(uid)
    rids = [add_reel(uid, aid) for _ in range(2)]
    for r in rids:
        give_signals(uid, r)
        pipeline.advance(uid, r)
    dev, hdr = make_device(member, "desktop")
    got = client.post("/api/worker/lease", json={"kinds": ["tag_reels"]}, headers=hdr).json()["job"]
    ans = fake_answer("QuickTagBatch", got["task"]["prompt"])
    short = {"tags": ans["tags"][:1]}
    r = client.post(f"/api/worker/jobs/{got['id']}/result", json={"output": short}, headers=hdr)
    assert r.status_code == 422 and "Missing" in r.json()["detail"]["validation"]
    r = client.post(f"/api/worker/jobs/{got['id']}/result", json={"output": ans}, headers=hdr)
    assert r.status_code == 200
    assert all(reel(x).tag_state == "done" and reel(x).needs_topic_retag for x in rids)  # no topics yet
    assert job(got["id"]).state == "done"
    add_topics(uid)
    rj = work.queue_retag_topics(uid)
    got = client.post("/api/worker/lease", json={"kinds": ["retag_topics"]}, headers=hdr).json()["job"]
    assert got["id"] == rj and set(job(rj).payload["reel_ids"]) == set(rids)
    ans = fake_answer("TopicRetagBatch", got["task"]["prompt"])
    assert len(ans["tags"]) == 2
    r = client.post(f"/api/worker/jobs/{rj}/result", json={"output": ans}, headers=hdr)
    assert r.status_code == 200, r.text
    with db.read() as c:
        rows = c.execute(select(db.analyses).where(db.analyses.c.user_id == uid)).all()
    assert all(not reel(x).needs_topic_retag for x in rids) and all(a.topics == ["client-results"] for a in rows)


def test_topics_from_reels_after_30_tags(rv_env, member):
    uid = member.id
    set_mode(uid, "desktop")
    aid = add_account(uid)
    rids = [add_reel(uid, aid, tag_state="queued") for _ in range(30)]
    jid = jobs.enqueue(uid, "tag_reels", {}, executor="desktop")
    with db.tx() as c:
        j = c.execute(select(db.jobs).where(db.jobs.c.id == jid)).first()
        work.lease_tag_batch(c, uid, j, "paid_api", n=30)
    j = job(jid)
    with pytest.raises(work.Invalid, match="expired"):   # a batch that is not leased any more is refused
        work.save_result(uid, j, {"tags": []}, {"via": "desktop"})
    with db.tx() as c:
        c.execute(db.jobs.update().where(db.jobs.c.id == jid).values(state="running"))
    j = job(jid)
    work.save_result(
        uid,
        j,
        {"tags": [fake_answer("QuickTagBatch", f"Reel ids: {r}.\n")["tags"][0] for r in rids]},
        {"via": "desktop"},
    )
    st = active(uid, "suggest_topics")
    assert len(st) == 1 and st[0].payload == {"from": "reels"}
    # new tags queue WP4's debounced insights refresh (integration finding: insights only refreshed on page opens)
    assert len(active(uid, "refresh_insights")) == 1
    with db.read() as c:
        u = c.execute(select(db.users).where(db.users.c.id == uid)).first()
    assert u.settings["topics_from_reels_at"] == 30


def test_api_tag_slot_gathers_a_batch_while_media_work_runs(rv_env, member):
    """Integration finding: with reels finishing one by one, every API tag call carried 1 reel. A server slot with
    less than a full batch now waits TAG_GATHER_S while the member still has reels in media work; Claude and desktop
    slots never wait."""
    aid = add_account(member.id)
    set_mode(member.id, "api")
    ready = add_reel(member.id, aid, tier="priority", media_state="have", signals_state="done", tag_state="queued")
    busy = add_reel(member.id, aid, tier="sample", media_state="have", signals_state="queued")
    slot = work.ensure_tag_slot(member.id)
    assert job(slot).executor == "server" and job(slot).run_after is not None
    assert job(slot).run_after > db.utcnow() + timedelta(seconds=work.TAG_GATHER_S - 5)
    with db.tx() as c:   # the last media job finished: no reason to wait any more
        c.execute(db.jobs.update().where(db.jobs.c.id == slot).values(state="canceled", dedupe_key=None))
        c.execute(db.reels.update().where(db.reels.c.id == busy).values(signals_state="done", tag_state="queued"))
    slot2 = work.ensure_tag_slot(member.id)
    assert job(slot2).run_after is None
    with db.tx() as c:
        c.execute(db.jobs.update().where(db.jobs.c.id == slot2).values(state="canceled", dedupe_key=None))
    set_mode(member.id, "claude_connector")
    add_reel(member.id, aid, tier="sample", media_state="have", signals_state="queued")
    slot3 = work.ensure_tag_slot(member.id)
    assert job(slot3).executor == "mcp" and job(slot3).run_after is None
    assert ready


def test_retry_of_a_failed_tag_batch_requeues_its_reels_on_the_current_ai(rv_env, member):
    """Integration finding: Retry on a failed tag batch re-ran an empty batch (its reels stayed 'failed') on the
    executor of the member's old AI mode."""
    uid = member.id
    aid = add_account(uid)
    set_mode(uid, "api")
    rids = [add_reel(uid, aid, media_state="have", signals_state="done", tag_state="failed") for _ in range(2)]
    jid = jobs.enqueue(uid, "tag_reels", {"reel_ids": rids}, executor="server")
    with db.tx() as c:
        c.execute(db.jobs.update().where(db.jobs.c.id == jid).values(state="failed", error_code="invalid_output"))
    set_mode(uid, "claude_connector")
    got = jobs.retry(jid, uid)
    assert got.id != jid and got.kind == "tag_reels" and got.executor == "mcp" and got.state == "waiting"
    assert all(reel(r).tag_state == "queued" for r in rids)
    assert job(jid).state == "canceled"
    # another AI job simply moves to the current executor
    aj = jobs.enqueue(uid, "make_output", {"output_id": "out_x"}, executor="server")
    with db.tx() as c:
        c.execute(db.jobs.update().where(db.jobs.c.id == aj).values(state="failed"))
    again = jobs.retry(aj, uid)
    assert again.id == aj and again.executor == "mcp" and again.state == "waiting"


def test_failed_tags_are_not_left_to_tag_forever(rv_env, member):
    """Integration finding: reels whose tag batch failed were counted as 'left to tag' and the pill never cleared."""
    from reelvault.server import api_reels

    aid = add_account(member.id)
    set_mode(member.id, "api")
    add_reel(member.id, aid, tier="priority", tag_state="failed")
    add_reel(member.id, aid, tier="sample", tag_state="done")
    with db.read() as c:
        u = c.execute(select(db.users).where(db.users.c.id == member.id)).first()
        p = api_reels.pipeline_state(c, u)
    assert p["left_to_tag"] == 0 and p["failed_tags"] == 1
    assert p["line"] == "1 reel could not be tagged. Press Retry on the failed job to try again."
