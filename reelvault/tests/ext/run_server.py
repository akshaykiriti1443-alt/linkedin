"""ReelVault server for the extension tests (DESIGN-SAAS.md 12.3). WP2. Test-only: never used in production.

    python tests/ext/run_server.py --port 9141 --data-dir /tmp/x

Starts the real app (create_app("web"), RV_TEST_PACING=1 so the server sends 50-100 ms gaps) with a fresh data
folder and one member, plus control routes under /__test/* for the Node test (pair code, accounts, pulls, state)
and a log of every request to /api/ext/v2/* (headers and body) so the test can prove no Instagram cookie arrives.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Request  # module level: the route annotations are strings (future annotations)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

LOG: list[dict] = []
LOG_LOCK = threading.Lock()


FLAGS = {"fail_503": False}   # POST /__test/flags {"fail_503": true}: answer every /fail with 503 (server trouble)


class Recorder:
    """Pure ASGI: keep method, path, headers and the body of every extension API request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/__test/flags" and scope["method"] == "POST":
            import json as _j
            body = b""
            while True:
                m = await receive()
                body += m.get("body", b"")
                if not m.get("more_body"):
                    break
            FLAGS.update(_j.loads(body or b"{}"))
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": _j.dumps(FLAGS).encode()})
            return
        if scope["type"] != "http" or not scope["path"].startswith("/api/ext/v2/"):
            return await self.app(scope, receive, send)
        if FLAGS["fail_503"] and scope["method"] == "POST" and scope["path"].endswith("/fail"):
            await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error": "down", "code": "server"}'})
            return
        chunks = []

        async def rec():
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
            return msg

        status = {}

        async def snd(msg):
            if msg["type"] == "http.response.start":
                status["s"] = msg["status"]
            await send(msg)

        await self.app(scope, rec, snd)
        body = b"".join(chunks)
        with LOG_LOCK:
            LOG.append({"method": scope["method"], "path": scope["path"], "status": status.get("s"),
                        "headers": {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]},
                        "body": body[:200_000].decode("utf-8", "replace") if not scope["path"].endswith("/video")
                        else f"<{len(body)} bytes>", "body_len": len(body)})


def build_app():
    from sqlalchemy import func, select

    from reelvault.server import api_ext, db, jobs, jobs_ig
    from reelvault.server.app import create_app
    from reelvault.server.security import hash_password

    app = create_app("web")
    r = APIRouter()
    state = {}

    with db.tx() as conn:
        uid = db.new_id("usr")
        now = db.utcnow()
        conn.execute(db.users.insert().values(id=uid, email="member@example.com", name="Mia Member",
                                              password_hash=hash_password("member-pass-123"), status="active",
                                              role="member", created_at=now, membership_checked_at=now,
                                              settings=dict(db.USER_SETTINGS_DEFAULT)))
    state["uid"] = uid

    def dev():
        with db.tx(immediate=False) as conn:
            return conn.execute(select(db.devices).where(db.devices.c.user_id == uid, db.devices.c.revoked_at.is_(None),
                                                         db.devices.c.kind == "extension")
                                .order_by(db.devices.c.created_at.desc())).first()

    @r.post("/__test/pair")
    def pair():
        code, exp = api_ext.new_pairing_code(uid, "extension")
        from reelvault.server.settings import get_settings

        return {"code": code, "pair_link": f"{get_settings().public_origin}/pair#{code}"}

    @r.post("/__test/account")
    async def account(req: Request):
        b = await req.json()
        aid = db.new_id("acc")
        with db.tx() as conn:
            conn.execute(db.accounts.insert().values(id=aid, user_id=uid, handle=b["handle"]))
        return {"account_id": aid}

    @r.post("/__test/pull")
    async def pull(req: Request):
        b = await req.json()
        return {"job_id": jobs_ig.start_pull(uid, b["account_id"], b.get("mode", "first"))}

    @r.post("/__test/profile")
    async def profile(req: Request):
        b = await req.json()
        return {"job_id": jobs_ig.request_profile(uid, b["handle"], "competitor", account_id=b["account_id"])}

    @r.post("/__test/raw_job")
    async def raw_job(req: Request):
        b = await req.json()
        return {"job_id": jobs.enqueue(uid, b["kind"], b["payload"], executor="extension")}

    real_problem = jobs_ig.payload_problem

    @r.post("/__test/trust")
    async def trust(req: Request):
        """{"off": true} = act like a compromised server: hand out jobs without the server's own payload check, so
        the test can prove the extension's validate.js refuses them (5.4.0)."""
        b = await req.json()
        jobs_ig.payload_problem = (lambda kind, p: None) if b.get("off") else real_problem
        return {"ok": True}

    @r.post("/__test/confirm")
    def confirm():
        d = dev()
        with db.tx() as conn:
            conn.execute(db.devices.update().where(db.devices.c.id == d.id).values(ig_confirmed_user_id=d.ig_user_id))
        return {"ok": True, "ig_user_id": d.ig_user_id, "ig_username": d.ig_username}

    @r.post("/__test/clear_cooldown")
    def clear_cooldown():
        with db.tx() as conn:  # every extension device of the member: a new pairing carries an active stop over
            for d in conn.execute(select(db.devices).where(db.devices.c.user_id == uid,
                                                           db.devices.c.kind == "extension")).all():
                st = dict(d.state or {}, cooldown_until=None, cooldown_reason=None, signed_in=True)
                conn.execute(db.devices.update().where(db.devices.c.id == d.id).values(state=st))
            conn.execute(db.jobs.update().where(db.jobs.c.user_id == uid, db.jobs.c.state == "waiting")
                         .values(run_after=None))
        return {"ok": True}

    @r.post("/__test/cancel_all")
    def cancel_all():
        with db.tx() as conn:
            conn.execute(db.jobs.update().where(db.jobs.c.user_id == uid,
                                                db.jobs.c.state.in_(("waiting", "queued", "running", "paused")),
                                                db.jobs.c.executor == "extension")
                         .values(state="canceled", dedupe_key=None, lease_owner=None))
        return {"ok": True}

    @r.post("/__test/videos")
    async def videos(req: Request):
        b = await req.json()
        with db.tx(immediate=False) as conn:
            ids = [x[0] for x in conn.execute(select(db.reels.c.id).where(db.reels.c.user_id == uid)
                                              .order_by(db.reels.c.taken_at.desc()).limit(int(b.get("n", 3))))]
        return {"job_id": jobs_ig.request_videos(uid, ids), "reel_ids": ids}

    @r.post("/__test/page_read")
    async def page_read(req: Request):
        b = await req.json()
        sid = db.new_id("src")
        kind = {"linkedin_self": "linkedin_page"}.get(b["kind"], b["kind"])
        with db.tx() as conn:
            conn.execute(db.profile_sources.insert().values(id=sid, user_id=uid, kind=kind, url=b["url"],
                                                            via="extension"))
        return {"job_id": jobs_ig.request_page_read(uid, sid, b["kind"], b["url"]), "source_id": sid}

    @r.post("/__test/discover")
    async def discover(req: Request):
        b = await req.json()
        run = db.new_id("drun")
        with db.tx() as conn:
            conn.execute(db.discover_runs.insert().values(id=run, user_id=uid, mode=b.get("mode", "search")))
        jid = jobs_ig.request_discover(uid, run, b.get("mode", "search"), queries=b.get("queries"),
                                       seeds=b.get("seeds"))
        return {"job_id": jid, "run_id": run}

    @r.post("/__test/reset")
    def reset():
        with db.tx() as conn:
            conn.execute(db.jobs.update().where(db.jobs.c.user_id == uid,
                                                db.jobs.c.state.in_(("waiting", "queued", "running", "paused")))
                         .values(state="canceled", dedupe_key=None, lease_owner=None))
            for t in (db.reel_snapshots, db.reel_accounts, db.reels, db.pulls, db.discover_candidates,
                      db.discover_runs, db.accounts, db.profile_sources):
                conn.execute(t.delete().where(t.c.user_id == uid))
            conn.execute(db.usage_counters.delete())
        return {"ok": True}

    @r.get("/__test/state")
    def st():
        with db.tx(immediate=False) as conn:
            accs = [dict(x._mapping) for x in conn.execute(select(db.accounts).where(db.accounts.c.user_id == uid))]
            pulls = [dict(x._mapping) for x in conn.execute(select(db.pulls).where(db.pulls.c.user_id == uid)
                                                            .order_by(db.pulls.c.id))]
            reels = [dict(x._mapping) for x in conn.execute(
                select(db.reels.c.id, db.reels.c.ig_pk, db.reels.c.code, db.reels.c.account_id, db.reels.c.is_collab,
                       db.reels.c.media_state, db.reels.c.video_path).where(db.reels.c.user_id == uid))]
            js = [dict(x._mapping) for x in conn.execute(select(db.jobs).where(db.jobs.c.user_id == uid)
                                                         .order_by(db.jobs.c.created_at))]
            srcs = [dict(x._mapping) for x in conn.execute(select(db.profile_sources)
                                                           .where(db.profile_sources.c.user_id == uid))]
            cands = [dict(x._mapping) for x in conn.execute(select(db.discover_candidates)
                                                            .where(db.discover_candidates.c.user_id == uid))]
            debug = [x.key for x in conn.execute(select(db.app_settings.c.key)
                                                 .where(db.app_settings.c.key.like("internal.debug_sample.%")))]
            ra = conn.execute(select(func.count()).select_from(db.reel_accounts)).scalar()
        d = dev()
        out = {"accounts": accs, "pulls": pulls, "reels": reels, "jobs": js, "sources": srcs, "candidates": cands,
               "debug_samples": debug, "reel_accounts": ra,
               "device": dict(d._mapping) if d else None, "uid": uid}
        import json as _j

        return _j.loads(_j.dumps(out, default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v)))

    @r.get("/__test/requests")
    def requests_log():
        with LOG_LOCK:
            return {"requests": list(LOG)}

    for route in reversed(r.routes):  # before the MCP mount at "/", which takes every unmatched path
        app.router.routes.insert(0, route)
    return Recorder(app)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9141)
    ap.add_argument("--data-dir", required=True)
    a = ap.parse_args(argv)
    data = Path(a.data_dir)
    (data / "db").mkdir(parents=True, exist_ok=True)
    os.environ.update({"DATA_DIR": str(data), "PUBLIC_URL": f"http://127.0.0.1:{a.port}", "RV_TEST_PACING": "1",
                       "RV_MASTER_KEYS": "test:" + base64.b64encode(os.urandom(32)).decode(), "LOG_LEVEL": "warning",
                       "RV_MODELS_DIR": str(data / "models"), "RV_ROLE": "web"})
    for k in ("DATABASE_URL", "ADMIN_EMAIL", "ADMIN_PASSWORD", "SMTP_URL"):
        os.environ.pop(k, None)
    import uvicorn

    app = build_app()
    print(f"reelvault test server on http://127.0.0.1:{a.port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning", lifespan="on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
