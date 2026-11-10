"""Route order and edge behaviour (DESIGN-SAAS.md 12.2, 13: ROUTERS, SPA paths, MCP mounted last)."""
from starlette.routing import Mount

from reelvault.server import (api_accounts, api_admin, api_ai, api_auth, api_ext, api_files, api_outputs,
                              api_profile, api_reels, oauth)
from reelvault.server import app as app_module


def test_routers_list_is_exactly_section_13():
    assert app_module.ROUTERS == [api_auth.router, api_admin.router, api_files.router, api_ext.router,
                                  api_accounts.router, api_reels.router, api_ai.router, api_outputs.router,
                                  api_profile.router, oauth.router]


def test_mcp_is_mounted_last_and_spa_has_no_catch_all(app):
    last = app.routes[-1]
    assert isinstance(last, Mount) and last.path == "" and last.name == "mcp"
    mounts = [r for r in app.routes if isinstance(r, Mount)]
    assert [m.name for m in mounts] == ["static", "mcp"]
    paths = [p for p, _m, _r in app_module.iter_routes(app)]
    assert "/" in paths and "/healthz" in paths and "/api/jobs/{job_id}" in paths
    assert not any(p.startswith("/{") for p in paths), "no catch-all route may exist (it would swallow /mcp)"


def test_core_routes_exist(app):
    have = {(m, p) for p, ms, _r in app_module.iter_routes(app) for m in ms}
    for want in [("POST", "/api/auth/signup"), ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"),
                 ("GET", "/api/me"), ("POST", "/api/auth/password"), ("POST", "/api/auth/reset"),
                 ("POST", "/api/auth/reset-request"), ("GET", "/api/invite/{code}"), ("DELETE", "/api/me"),
                 ("GET", "/api/me/export"), ("POST", "/api/auth/reconfirm"), ("GET", "/api/admin/users"),
                 ("PATCH", "/api/admin/users/{user_id}"), ("POST", "/api/admin/users/{user_id}/approve"),
                 ("POST", "/api/admin/invites/community"), ("POST", "/api/admin/sync-members"),
                 ("POST", "/api/hooks/skool-removed"), ("GET", "/api/jobs"), ("POST", "/api/jobs/{job_id}/retry"),
                 ("GET", "/api/home"), ("GET", "/files/{user_id}/{rel:path}"), ("GET", "/api/version")]:
        assert want in have, want


def test_mcp_unauthenticated_is_401_with_www_authenticate(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401
    wa = r.headers.get("www-authenticate", "")
    assert wa.startswith("Bearer") and "resource_metadata=" in wa
    assert "/.well-known/oauth-protected-resource/mcp" in wa


def test_protected_resource_metadata(client):
    r = client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    assert r.json()["resource"] == "http://localhost/mcp"


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["db"] == "ok" and "version" in body and "disk_free_mb" in body


def test_spa_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/app.js").status_code == 200


def test_unknown_paths_are_json_404_not_spa_not_mcp(client):
    for path in ("/nope", "/api/nope", "/static/../etc/passwd", "/mcpx", "/reels", "/.well-known/other"):
        r = client.get(path)
        assert r.status_code == 404, path
        assert r.headers["content-type"].startswith("application/json"), path
        assert r.json()["code"] == "not_found", path
        assert "www-authenticate" not in r.headers


def test_errors_use_the_shared_shape(client):
    r = client.get("/api/me")
    assert r.status_code == 401 and r.json() == {"error": "Please sign in.", "code": "not_signed_in"}
    r = client.post("/api/auth/login", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "bad_input"


def test_version(client):
    assert client.get("/api/version").json()["extension_min"] == "2.0.0"


def test_cors_only_on_device_paths(client):
    o = {"Origin": "chrome-extension://abcdefghijklmnop", "Access-Control-Request-Method": "POST"}
    r = client.options("/api/ext/v2/hello", headers=o)
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == o["Origin"]
    assert "Authorization" in r.headers["access-control-allow-headers"]
    r = client.get("/api/version", headers={"Origin": o["Origin"]})
    assert "access-control-allow-origin" not in r.headers
    r = client.options("/api/worker/hello", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_big_json_body_is_refused(client):
    r = client.post("/api/auth/login", content=b"x", headers={"content-type": "application/json",
                                                              "content-length": str(9 * 1024 * 1024)})
    assert r.status_code == 413


def test_device_paths_refuse_sessions(member_client):
    # a session cookie must never open a device path (4.1); stub routers 404, real ones 401
    r = member_client.post("/api/ext/v2/hello", json={})
    assert r.status_code in (401, 404)
