"""Self-update: version compare, check, 'not while a job runs', and a safe helper when pip fails."""

from __future__ import annotations

import http.client
import importlib
import json
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from reelvault import brain, update
from reelvault.schema import PurposeGuesses, ReelAnalysis, RemakePack


def test_versions_compare_as_numbers():
    assert update.is_newer("0.4.10", "0.4.9")
    assert update.is_newer("0.5", "0.4.99")
    assert update.is_newer("1.0.0", "0.9")
    assert not update.is_newer("0.4.0", "0.4")
    assert not update.is_newer("0.4.9", "0.4.10")
    assert not update.is_newer(None, "0.4.0")
    assert update.parse_version("v0.4.1rc1") == (0, 4, 1)


def test_check_reads_github_and_caches(monkeypatch):
    calls = []

    def fake_get(url, timeout=20):
        calls.append(url)
        return b'"""ReelVault"""\n\n__version__ = "0.4.2"\n'

    monkeypatch.setattr(update, "_get", fake_get)
    monkeypatch.setattr(update, "_cache", {"t": 0.0, "key": None, "v": None})
    info = update.check("0.4.0", "main")
    assert info == {"current": "0.4.0", "latest": "0.4.2", "available": True, "error": None}
    assert calls == ["https://raw.githubusercontent.com/akshaykiriti1443-alt/linkedin/main/reelvault/reelvault/__init__.py"]
    update.check("0.4.0", "main")
    assert len(calls) == 1  # cached
    update.check("0.4.0", "main", force=True)
    assert len(calls) == 2

    def offline(url, timeout=20):
        raise OSError("no network")

    monkeypatch.setattr(update, "_get", offline)
    info = update.check("0.4.0", "main", force=True)
    assert info["available"] is False and info["latest"] is None and "internet" in info["error"]


def test_find_project_in_github_zip_layout(tmp_path):
    proj = tmp_path / "linkedin-claude-kind-dijkstra-0o03in" / "reelvault"
    (proj / "reelvault").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("[project]\nname='reelvault'\n")
    (proj / "reelvault" / "__init__.py").write_text('__version__ = "0.4.1"\n')
    assert update.find_project(tmp_path) == proj
    with pytest.raises(update.UpdateError):
        update.find_project(tmp_path / "linkedin-claude-kind-dijkstra-0o03in" / "reelvault" / "reelvault")


def _src(tmp_path) -> Path:
    src = tmp_path / "src"
    (src / "reelvault").mkdir(parents=True)
    (src / "reelvault" / "__init__.py").write_text('__version__ = "0.4.1"\n')
    return src


def test_helper_keeps_old_version_when_pip_fails(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ran, relaunched = [], []

    def fake_run(cmd, **kw):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ERROR: No matching distribution found")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "relaunch", lambda h, p: relaunched.append((h, p)))
    monkeypatch.setattr(update, "wait_for_port", lambda port, seconds=60: True)
    assert update.apply(_src(tmp_path), 8899, home) is False
    assert len(ran) == 1  # stopped after the first pip failure; nothing else touched
    assert relaunched == [(home, 8899)]  # the app starts again either way
    log = (home / "update.log").read_text(encoding="utf-8")
    assert "FAILED" in log and "No matching distribution" in log
    last = update.take_last_result(home)
    assert last["ok"] is False and "could not install" in last["message"] and "still works" in last["message"]
    assert "No matching distribution" in last["detail"]
    assert update.take_last_result(home) is None  # shown once


def test_helper_installs_then_relaunches(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ran, relaunched = [], []
    monkeypatch.setattr(update.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "ok", ""))
    monkeypatch.setattr(update, "relaunch", lambda h, p: relaunched.append(p))
    monkeypatch.setattr(update, "wait_for_port", lambda port, seconds=60: True)
    src = _src(tmp_path)
    assert update.apply(src, 8899, home) is True
    assert ran[0][-3:] == ["install", "--upgrade", str(src)]
    assert ran[1][-4:] == ["install", "--force-reinstall", "--no-deps", str(src)]
    assert relaunched == [8899]
    last = update.take_last_result(home)
    assert last["ok"] is True and last["version"] == "0.4.1"
    assert "Updated to version 0.4.1." in (home / "update.log").read_text(encoding="utf-8")


def test_helper_gives_up_if_app_never_closes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ran = []
    monkeypatch.setattr(update.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    monkeypatch.setattr(update, "relaunch", lambda h, p: None)
    monkeypatch.setattr(update, "wait_for_port", lambda port, seconds=60: False)
    assert update.apply(_src(tmp_path), 8899, home) is False
    assert ran == []
    assert "did not close" in json.loads((home / "update-result.json").read_text())["message"]


@pytest.fixture()
def app_server(tmp_path, monkeypatch):
    monkeypatch.setenv("REELVAULT_HOME", str(tmp_path / "home"))
    import reelvault.app as app
    app = importlib.reload(app)
    app.prepare_home()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
        c.request(method, path, body=json.dumps(body or {}).encode() if method == "POST" else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        out = r.status, json.loads(r.read())
        c.close()
        return out

    call.app = app
    yield call
    srv.shutdown()
    srv.server_close()


def test_no_update_while_a_job_runs(app_server, monkeypatch):
    app = app_server.app
    downloads = []
    monkeypatch.setattr(update, "download", lambda *a: downloads.append(a))
    monkeypatch.setattr(update, "check", lambda *a, **k: {"current": "0.4.0", "latest": "0.4.1",
                                                           "available": True, "error": None})
    app.JOB["name"] = "Get new saved reels"
    try:
        code, body = app_server("POST", "/api/update")
    finally:
        app.JOB["name"] = None
    assert code == 409 and "Wait for it to finish" in body["error"]
    assert downloads == []


def test_update_info_and_nothing_new(app_server, monkeypatch):
    app = app_server.app
    monkeypatch.setattr(update, "check", lambda cur, branch, force=False: {
        "current": cur, "latest": cur, "available": False, "error": None})
    code, info = app_server("GET", "/api/update")
    assert code == 200 and info["current"] == app.__version__ and info["available"] is False
    assert info["error"] is None
    code, body = app_server("POST", "/api/update")
    assert code == 400 and "newest version" in body["error"]
    assert app.JOB["name"] is None  # the busy flag is released after an error
    app.UPDATE_STATE["last"] = {"ok": False, "message": "The update could not install. boom"}
    code, info = app_server("GET", "/api/update")
    assert "boom" in info["error"] and info["last_update"]["ok"] is False


# --------------------------------------------------------------------------
# Schema size (Windows command line)
# --------------------------------------------------------------------------

def test_schemas_stay_small():
    for model, limit in ((ReelAnalysis, 4500), (RemakePack, 4500), (PurposeGuesses, 4500)):
        size = len(json.dumps(brain.compact_schema(model.model_json_schema()), separators=(",", ":")))
        assert size < limit, (model.__name__, size)
    props = brain.compact_schema(ReelAnalysis.model_json_schema())["properties"]
    assert {"purpose_guess", "claims_to_check", "client_explainer"} <= set(props)
