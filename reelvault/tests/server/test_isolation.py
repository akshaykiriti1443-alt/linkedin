"""Per-member data isolation (DESIGN-SAAS.md 11.5): member B calling member A's ids always gets 404.

The sweep walks EVERY registered route with path parameters, so routes other packages add later are covered
automatically. A route whose parameter cannot be mapped to one of A's ids fails the test: add the mapping below.
"""
import re

import pytest
from fastapi.routing import APIRoute

from reelvault.server import db, jobs, storage
from reelvault.server.app import iter_routes

# Routes that are not member-scoped by id (admin, auth codes, hooks, device-token paths, files handled below).
SKIP_PREFIXES = ("/api/admin/", "/api/invite/", "/api/hooks/", "/api/ext/v2/", "/api/worker/", "/oauth/",
                 "/.well-known/", "/files/", "/download/", "/static")

# (path regex, param) -> key in the seeded ids dict
PARAM_MAP = [
    (r"^/api/jobs/", "job_id", "job"),
    (r"^/api/accounts/", None, "account"),
    (r"^/api/discover/", None, "run"),
    (r"^/api/reels/", None, "reel"),
    (r"^/api/formats/", None, "format"),
    (r"^/api/outputs/", None, "output"),
    (r"^/api/profile/sources/", None, "source"),
    (r"^/api/topics/", None, "topic"),
    (r"^/api/ai/connections/", None, "conn"),
    (r"^/api/ai/desktop/", None, "desktop"),
    (r"^/api/insights/", None, "snapshot"),
]


def seed_member_a(uid: str) -> dict:
    now = db.utcnow()
    ids = {k: db.new_id(p) for k, p in [("account", "acc"), ("reel", "reel"), ("output", "out"), ("topic", "top"),
                                        ("format", "fmt"), ("source", "src"), ("conn", "aic"), ("desktop", "dev"),
                                        ("run", "drun"), ("snapshot", "snap")]}
    with db.tx() as conn:
        conn.execute(db.accounts.insert().values(id=ids["account"], user_id=uid, handle="alexhormozi"))
        conn.execute(db.reels.insert().values(id=ids["reel"], user_id=uid, account_id=ids["account"], ig_pk="123",
                                              code="C1", url="https://www.instagram.com/reel/C1/"))
        conn.execute(db.outputs.insert().values(id=ids["output"], user_id=uid, kind="reel_script", title="t"))
        conn.execute(db.topics.insert().values(id=ids["topic"], user_id=uid, slug="ads", label="Ads"))
        conn.execute(db.formats.insert().values(id=ids["format"], user_id=uid, recipe_key="VF01|x", vf="VF01"))
        conn.execute(db.profile_sources.insert().values(id=ids["source"], user_id=uid, kind="website", via="server"))
        conn.execute(db.ai_connections.insert().values(id=ids["conn"], user_id=uid, provider="gemini", model="m"))
        conn.execute(db.devices.insert().values(id=ids["desktop"], user_id=uid, kind="desktop", token_hash="th_a"))
        conn.execute(db.discover_runs.insert().values(id=ids["run"], user_id=uid, mode="niche"))
        conn.execute(db.insight_snapshots.insert().values(id=ids["snapshot"], user_id=uid, computed_at=now))
    ids["job"] = jobs.enqueue(uid, "score_account", {"account_id": ids["account"]})
    return ids


def _fill(path: str, ids: dict) -> str | None:
    params = re.findall(r"{([^}:]+)(?::[^}]*)?}", path)
    out = path
    for p in params:
        key = None
        for rx, name, k in PARAM_MAP:
            if re.search(rx, path) and (name is None or name == p):
                key = k
                break
        if key is None:
            return None
        out = re.sub(r"{" + re.escape(p) + r"(?::[^}]*)?}", ids[key], out, count=1)
    return out


def test_every_id_route_returns_404_for_another_members_ids(app, member, other_client):
    ids = seed_member_a(member.id)
    unmapped, checked, wrong = [], [], []
    for path, methods, r in iter_routes(app):
        if not isinstance(r, APIRoute) or "{" not in path or path.startswith(SKIP_PREFIXES):
            continue
        url = _fill(path, ids)
        if url is None:
            unmapped.append(path)
            continue
        for method in sorted(methods & {"GET", "PATCH", "DELETE", "PUT", "POST"}):
            if method == "POST" and not re.search(r"/(cancel|retry|analyze|redo|test|paste|pull|track)$", path):
                continue
            resp = other_client.request(method, url, json={})
            checked.append((method, path))
            if resp.status_code != 404:
                wrong.append((method, path, resp.status_code, resp.text[:120]))
    assert not unmapped, f"add these routes to PARAM_MAP: {unmapped}"
    assert not wrong, wrong
    assert ("GET", "/api/jobs/{job_id}") in checked


def test_member_files_are_private(member_client, other_client, member, other_member):
    p = storage.member_path(member.id, "reels/r1/cover.jpg")
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xd8jpeg")
    url = f"/files/{member.id}/reels/r1/cover.jpg"
    r = member_client.get(url)
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
    assert "private" in r.headers["cache-control"]
    assert other_client.get(url).status_code == 404
    assert other_client.get(f"/files/{other_member.id}/../{member.id}/reels/r1/cover.jpg").status_code == 404
    assert member_client.get(f"/files/{member.id}/reels/r1/missing.jpg").status_code == 404


def test_scoped_helper_requires_a_user(rv_env, member, other_member):
    ids = seed_member_a(member.id)
    with db.tx(immediate=False) as conn:
        assert db.get_owned(conn, db.reels, member.id, ids["reel"]) is not None
        assert db.get_owned(conn, db.reels, other_member.id, ids["reel"]) is None
        assert db.update_owned(conn, db.reels, other_member.id, ids["reel"], {"starred": True}) == 0
        with pytest.raises(ValueError):
            db.scoped(conn, db.reels, "")


def test_home_and_me_show_only_own_data(member_client, other_client, member):
    seed_member_a(member.id)
    assert member_client.get("/api/me").json()["setup"]["competitors"] == 1
    assert other_client.get("/api/me").json()["setup"]["competitors"] == 0
    assert len(member_client.get("/api/home").json()["jobs"]) == 1
    assert other_client.get("/api/home").json()["jobs"] == []
