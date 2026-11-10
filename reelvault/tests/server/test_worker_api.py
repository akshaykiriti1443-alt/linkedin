"""WP5: the desktop worker API (4.7) and members_worker.py, with brain.claude_code faked; the desktop app's
Settings card; and (opt-in, RV_REAL_CLAUDE=1) one real run of the unmodified Claude Code CLI on a synthetic reel."""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from _wp5_support import (
    add_account,
    add_reel,
    add_topics,
    approve_profile,
    fake_answer,
    free_port,
    give_signals,
    set_mode,
)

from reelvault import members_worker as mw
from reelvault import schema_members as S
from reelvault.server import db, jobs, pipeline, work


class Session:
    """requests.Session look-alike over the FastAPI TestClient (what members_worker.Client calls)."""

    def __init__(self, tc):
        self.tc = tc
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, data=None, headers=None, timeout=None):
        path = url.split("://", 1)[-1].split("/", 1)[1]
        self.posts.append(("/" + path, json.loads(data or "{}")))
        return self.tc.post("/" + path, content=data, headers=headers)

    def get(self, url, headers=None, timeout=None):
        path = url.split("://", 1)[-1].split("/", 1)[1]
        return self.tc.get("/" + path, headers=headers)


SCHEMAS = {json.dumps(mw.schema_for(n)[1], sort_keys=True): n for n in S.MODELS}


def schema_name(schema, cwd: Path) -> str:
    if schema is None:
        schema = json.loads((cwd / "schema.json").read_text())
    return SCHEMAS[json.dumps(schema, sort_keys=True)]


class FakeClaude:
    """Stands in for brain.claude_code: answers with the fake AI, checks the images are in the folder."""

    def __init__(self, bad_first: int = 0):
        self.calls = []
        self.bad_first = bad_first

    def __call__(self, prompt, instruction, system, cwd, schema, read_files, model, effort):
        name = schema_name(schema, cwd)
        images = sorted(p.name for p in Path(cwd).glob("*.jpg"))
        for n in images:
            assert n in instruction and (Path(cwd) / n).read_bytes()[:2] == b"\xff\xd8"
        self.calls.append(
            {
                "name": name,
                "prompt": prompt,
                "instruction": instruction,
                "schema": schema,
                "images": images,
                "file": (Path(cwd) / "schema.json").exists(),
                "read": read_files,
            }
        )
        ans = fake_answer(name, prompt + "\n" + system)
        if self.bad_first and len(self.calls) <= self.bad_first and name == "QuickTagBatch":
            ans = {"tags": ans["tags"][:-1]}  # passes pydantic, fails the batch check
        return {
            "type": "result",
            "subtype": "success",
            "structured_output": None if schema is None else ans,
            "result": json.dumps(ans),
        }


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("REELVAULT_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def linked(rv_env, member, member_client, client, home):
    r = member_client.post("/api/ai/desktop/pair", json={})
    assert r.status_code == 200 and r.json()["server"] == "http://localhost"
    sess = Session(client)
    out = mw.pair("http://localhost", r.json()["code"], "Test PC", session=sess)
    assert out["ok"] and mw.load_config()["token"].startswith("rvd_")
    set_mode(member.id, "desktop")
    cfg = mw.load_config()
    return mw.Client(cfg["server"], cfg["token"], sess)


def drain_worker(cli, runner, limit=30):
    recs = []
    for _ in range(limit):
        rec = mw.process_one(cli, runner=runner)
        if rec is None:
            break
        recs.append(rec)
    return recs


def test_every_task_kind_completes_through_the_worker(linked, member, member_client):
    uid = member.id
    approve_profile(uid)
    add_topics(uid)
    aid = add_account(uid)
    rids = [add_reel(uid, aid, score=s) for s in (6.0, 4.0)]
    for r in rids:
        give_signals(uid, r)
        pipeline.advance(uid, r)
    work.queue_deep(uid, [rids[0]])
    with db.tx() as c:
        c.execute(
            db.profile_sources.insert().values(
                id="src_x", user_id=uid, kind="paste", via="paste", status="ok", text="We help dentists."
            )
        )
    work.queue_profile_draft(uid)
    work.queue_topics(uid)
    fid = db.new_id("fmt")
    with db.tx() as c:
        c.execute(
            db.formats.insert().values(
                id=fid, user_id=uid, recipe_key="VF01|hb=1", vf="VF01", recipe={}, example_reels=rids
            )
        )
    work.queue_format_names(uid, [fid])
    oid, _ = work.create_output(uid, "reel_script", {"count": 2, "based_on": {"reel_ids": rids}})
    fake = FakeClaude()
    recs = drain_worker(linked, fake)
    assert all(r["ok"] for r in recs), recs
    kinds = {c["name"] for c in fake.calls}
    assert kinds >= {
        "QuickTagBatch",
        "ReelBreakdown",
        "ProfileDraft",
        "TopicList",
        "FormatGuideBatch",
        "ReelScriptPack",
    }
    deep_call = next(c for c in fake.calls if c["name"] == "ReelBreakdown")
    assert deep_call["file"] and deep_call["schema"] is None and "schema.json" in deep_call["instruction"]
    assert len(deep_call["images"]) == 8  # paid_api deep frames
    tag_call = next(c for c in fake.calls if c["name"] == "QuickTagBatch")
    assert not tag_call["file"] and tag_call["schema"] is not None and len(tag_call["images"]) == 2
    with db.read() as c:
        out = c.execute(select(db.outputs).where(db.outputs.c.id == oid)).first()
        q = c.execute(
            select(db.analyses).where(db.analyses.c.reel_id == rids[0], db.analyses.c.level == "quick")
        ).first()
        dev = c.execute(select(db.devices).where(db.devices.c.user_id == uid)).first()
    assert out.status == "done" and len(out.data["scripts"]) == 2
    assert sum(1 for c in fake.calls if c["name"] == "ReelScriptPack") == 2  # a chunk per lease
    assert q.made_by == {"via": "desktop", "provider": "claude_code"} and q.tag_quality == "vision"
    assert dev.name == "Test PC"
    # retag topics after a topic change
    add_topics(uid, ("canva",))
    work.queue_retag_topics(uid)
    fake2 = FakeClaude()
    assert drain_worker(linked, fake2) and fake2.calls[0]["name"] == "TopicRetagBatch"
    assert not work.pending_counts(uid)["retag_topics"]


def test_422_is_retried_once_with_the_message(linked, member):
    uid = member.id
    aid = add_account(uid)
    rids = [add_reel(uid, aid) for _ in range(2)]
    for r in rids:
        give_signals(uid, r)
        pipeline.advance(uid, r)
    fake = FakeClaude(bad_first=1)
    rec = mw.process_one(linked, runner=fake)
    assert rec["ok"] and len(fake.calls) == 2
    assert (
        "ReelVault could not use your last answer" in fake.calls[1]["prompt"] and "Missing" in fake.calls[1]["prompt"]
    )
    with db.read() as c:
        assert all(r.tag_state == "done" for r in c.execute(select(db.reels).where(db.reels.c.user_id == uid)))


def test_schema_file_path_when_the_command_line_is_too_long(linked, member, monkeypatch):
    monkeypatch.setattr(mw, "ARGV_LIMIT", 500)
    uid = member.id
    aid = add_account(uid)
    rid = add_reel(uid, aid)
    give_signals(uid, rid)
    pipeline.advance(uid, rid)
    fake = FakeClaude()
    rec = mw.process_one(linked, runner=fake)
    assert rec["ok"] and rec["schema_file"] is True
    assert fake.calls[0]["schema"] is None and fake.calls[0]["file"] and fake.calls[0]["read"]


def test_worker_routes_are_scoped_to_the_device(linked, member, other_member, make_device, client):
    uid = member.id
    aid = add_account(uid)
    rid = add_reel(uid, aid)
    give_signals(uid, rid)
    pipeline.advance(uid, rid)
    hello = linked.post("/api/worker/hello", {"version": "1.0", "claude": {"found": True, "signed_in": True}})[1]
    assert hello == {"ok": True, "has_work": True}
    _, got = linked.post("/api/worker/lease", {"kinds": ["tag_reels"]})
    job = got["job"]
    assert job["task"]["schema_name"] == "QuickTagBatch" and job["task"]["image_dest"] == "paid_api"
    name = job["task"]["images"][0]["name"]
    assert linked.get_bytes(f"/api/worker/files/{job['id']}/{name}")[:2] == b"\xff\xd8"
    _, other_hdr = make_device(other_member, "desktop")
    assert client.get(f"/api/worker/files/{job['id']}/{name}", headers=other_hdr).status_code == 404
    _, mine2 = make_device(member, "desktop")  # same member, not the lease owner
    assert client.get(f"/api/worker/files/{job['id']}/{name}", headers=mine2).status_code == 404
    assert client.get(f"/api/worker/files/{job['id']}/x.jpg", headers=linked._headers()).status_code == 404
    _, ext_hdr = make_device(member, "extension")
    assert client.post("/api/worker/lease", json={}, headers=ext_hdr).status_code == 401
    assert (
        client.post(f"/api/worker/jobs/{job['id']}/result", json={"output": {}}, headers=other_hdr).status_code == 404
    )
    r = client.post(f"/api/worker/jobs/{job['id']}/result", json={"output": {"tags": "x"}}, headers=linked._headers())
    assert r.status_code == 422 and r.json()["code"] == "invalid_output"
    linked.post(f"/api/worker/jobs/{job['id']}/fail", {"message": "Claude Code is not signed in.\x00"})
    j = jobs.get(job["id"])
    assert j.state == "waiting" and j.run_after is not None
    with db.read() as c:
        r = c.execute(select(db.reels).where(db.reels.c.id == rid)).first()
    assert r.tag_state == "queued" and r.tag_batch is None


def test_ai_state_and_desktop_devices(linked, member, member_client):
    st = member_client.get("/api/ai").json()
    assert st["mode"] == "desktop" and st["desktop"]["devices"][0]["name"] == "Test PC"
    assert st["connector"]["url"] == "http://localhost/mcp" and st["connector"]["connected"] is False
    dev = st["desktop"]["devices"][0]["id"]
    est = member_client.get("/api/ai/estimate?mode=desktop").json()
    assert est["reels_to_tag"] == 150 and est["lines"][0].startswith("The desktop app works through")
    est = member_client.get("/api/ai/estimate?mode=claude_connector").json()
    assert est["chats"] and "Claude chats" in est["lines"][0]
    est = member_client.get("/api/ai/estimate?mode=api&provider=anthropic&model=claude-sonnet-5-5").json()
    assert est["cost_usd"] and "Anthropic" in est["lines"][0]
    cat = member_client.get("/api/ai/catalog").json()
    assert {p["id"] for p in cat["providers"]} == {"anthropic", "gemini", "openai", "openai_compat"}
    assert next(p for p in cat["providers"] if p["id"] == "anthropic")["default_model"] == "claude-sonnet-5-5"
    assert member_client.delete(f"/api/ai/desktop/{dev}").json() == {"ok": True}
    with pytest.raises(mw.Unpaired):
        linked.post("/api/worker/hello", {})


def test_connection_api_tests_and_hides_the_key(rv_env, member, member_client):
    from _wp5_support import FakeAI

    fake = FakeAI()
    try:
        db.set_setting("allow_local_ai_urls", True)
        body = {
            "provider": "openai_compat",
            "preset": "custom",
            "base_url": fake.base_url,
            "model": "plain-text-model",
            "api_key": "sk-secret-abcd1234",
        }
        fake.script = ["401", "401"]
        r = member_client.post("/api/ai/connections", json=body)
        assert r.status_code == 400 and "key didn't work" in r.json()["error"]
        r = member_client.post("/api/ai/connections?save_anyway=1", json=body)
        assert r.status_code == 201 and r.json()["connection"]["status"] == "bad_key"
        r = member_client.post("/api/ai/connections", json=dict(body, use_for="tagging"))
        assert r.status_code == 201
        res = r.json()
        conn = res["connection"]
        assert conn["key"] == "••••1234" and "sk-secret" not in r.text and conn["use_for"] == "tagging"
        assert res["test"]["ok"] and res["test"]["message"].startswith("Works.") and "warning" in res
        st = member_client.get("/api/ai").json()
        assert st["tagging_conn_id"] == conn["id"] and st["mode"] == "api" and "sk-secret" not in json.dumps(st)
        with db.read() as c:
            row = c.execute(select(db.ai_connections).where(db.ai_connections.c.id == conn["id"])).first()
        assert row.key_ciphertext and b"sk-secret" not in row.key_ciphertext
        r = member_client.patch(f"/api/ai/connections/{conn['id']}", json={"is_default": True, "model": "other-model"})
        assert r.json()["connection"]["is_default"] is True and r.json()["connection"]["model"] == "other-model"
        db.set_setting("allow_local_ai_urls", False)
        r = member_client.post("/api/ai/connections", json=dict(body, preset="ollama_local"))
        assert r.status_code == 400 and "self-hosted" in r.json()["error"]
        r = member_client.post("/api/ai/connections", json=dict(body, base_url="http://10.0.0.5/v1"))
        assert r.status_code == 400
        assert member_client.delete(f"/api/ai/connections/{conn['id']}").json() == {"ok": True}
        assert member_client.get("/api/ai").json()["tagging_conn_id"] is None
    finally:
        fake.stop()


def test_desktop_settings_card(home, monkeypatch):
    """reelvault/app.py: the index page loads the card; the card's endpoints link, switch and unlink."""
    import urllib.request
    from http.server import ThreadingHTTPServer

    from reelvault import app as desktop

    monkeypatch.setattr(desktop, "HOME", home)
    monkeypatch.setattr(desktop, "_MEMBERS", {"worker": None})
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), desktop.Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"

    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.read().decode()

    def post(path, body):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    try:
        assert '<script src="/members-card.js"></script>' in get("/")
        assert "Use my Claude for ReelVault Members" in get("/members-card.js")
        assert json.loads(get("/api/members"))["linked"] is False
        assert "Link this app first" in post("/api/members/working", {"on": True})["error"]
        r = post("/api/members/link", {"server": "http://example.com", "code": "AAAA-BBBB"})
        assert r["error"] == "The server address must start with https://." and r["linked"] is False

        def fake_pair(server, code, name=None, session=None):
            mw.save_config({"server": "https://app.example.com", "token": "rvd_x", "member": "Mia", "working": True})
            return {"ok": True}

        monkeypatch.setattr(mw, "pair", fake_pair)
        monkeypatch.setattr(mw, "POLL_S", 3600)
        r = post("/api/members/link", {"server": "app.example.com", "code": "AAAA-BBBB"})
        assert r["linked"] and r["working"] and r["running"] and r["member"] == "Mia"
        r = post("/api/members/working", {"on": False})
        assert r["working"] is False and mw.load_config()["working"] is False
        r = post("/api/members/unlink", {})
        assert r["linked"] is False and "token" not in mw.load_config()
    finally:
        desktop._MEMBERS["worker"] and desktop._MEMBERS["worker"].stop()
        srv.shutdown()
        srv.server_close()


# ============================================================================================== real Claude Code


@pytest.mark.skipif(
    os.environ.get("RV_REAL_CLAUDE") != "1", reason="set RV_REAL_CLAUDE=1 to run the real Claude Code CLI"
)
def test_real_claude_code_tags_and_breaks_down_one_synthetic_reel(rv_env, member, member_client, home):
    """End to end on this machine: a real uvicorn server, members_worker with the real `claude -p` on the member's
    own Claude login, one synthetic reel: quick tags (schema on the command line) and a deep breakdown (the
    ReelBreakdown schema is over 6,000 argv characters, so it goes through schema.json)."""
    import uvicorn

    from reelvault.brain import find_claude
    from reelvault.server.app import create_app

    assert find_claude(), "Claude Code is not installed"
    uid = member.id
    approve_profile(uid)
    add_topics(uid)
    aid = add_account(uid)
    rid = add_reel(uid, aid, caption="How we got 41 new patients for a dentist. Comment AUDIT.")
    give_signals(uid, rid)
    set_mode(uid, "desktop")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app("web"), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    try:
        code = member_client.post("/api/ai/desktop/pair", json={}).json()["code"]
        mw.pair(f"http://127.0.0.1:{port}", code, "Real test")
        pipeline.advance(uid, rid)
        work.queue_deep(uid, [rid])
        worker = mw.Worker()
        recs = []
        for _ in range(4):
            rec = worker.tick()
            if rec is None:
                break
            recs.append(rec)
        assert [r["kind"] for r in recs] == ["analyze_reel", "tag_reels"] or sorted(r["kind"] for r in recs) == [
            "analyze_reel",
            "tag_reels",
        ], recs
        assert all(r["ok"] for r in recs), recs
        deep = next(r for r in recs if r["kind"] == "analyze_reel")
        assert deep["schema_file"] is True
        with db.read() as c:
            rows = {a.level: a for a in c.execute(select(db.analyses).where(db.analyses.c.reel_id == rid))}
            reel = c.execute(select(db.reels).where(db.reels.c.id == rid)).first()
        assert reel.tag_state == "done" and reel.deep_state == "done"
        assert rows["quick"].visual_format in [f"VF{i:02d}" for i in range(1, 21)] + ["other"]
        assert rows["quick"].made_by["via"] == "desktop" and rows["deep"].data["reel_id"] == rid
        print(
            "REAL CLAUDE:",
            json.dumps(
                {
                    "quick": {
                        k: rows["quick"].data.get(k)
                        for k in ("visual_format", "visual_subject", "hook_type", "one_line")
                    },
                    "deep_title": rows["deep"].data.get("title"),
                    "weaknesses": rows["deep"].data.get("weaknesses"),
                },
                ensure_ascii=False,
            ),
        )
    finally:
        server.should_exit = True
        th.join(timeout=10)
