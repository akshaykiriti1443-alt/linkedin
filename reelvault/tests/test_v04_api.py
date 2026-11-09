"""ReelVault 0.4 backend: library migration, purposes, to-dos, banks, remake, batch guess, UI files.

Claude is faked; no network, no Instagram.
"""

from __future__ import annotations

import copy
import http.client
import importlib
import json
import shutil
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from reelvault import brain
from reelvault.library import Library
from reelvault.schema import PurposeGuesses, RemakePack
from test_smoke import SAMPLE

OLD_SCHEMA = """
CREATE TABLE items (
    shortcode TEXT PRIMARY KEY, status TEXT NOT NULL, folder TEXT NOT NULL, category TEXT, title TEXT,
    owner TEXT, url TEXT, hook_type TEXT, cta_type TEXT, format TEXT, structure TEXT, error TEXT,
    added_at TEXT NOT NULL, analyzed_at TEXT, data TEXT
);
CREATE VIRTUAL TABLE search USING fts5(
    shortcode UNINDEXED, title, category, tags, summary, transcript, hook, onscreen, caption
);
"""


def analysis(title: str, guess: str | None = "hook", category: str = "Marketing & Sales") -> dict:
    a = copy.deepcopy(SAMPLE)
    a["title"], a["category"] = title, category
    if guess:
        a["purpose_guess"] = {"purpose": guess, "reason": "Because."}
    else:  # a reel analyzed before 0.4
        for k in ("purpose_guess", "claims_to_check", "client_explainer"):
            a.pop(k, None)
    return a


def file_reel(lib: Library, root: Path, code: str, a: dict, video: bool = True) -> Path:
    work = root / "work" / code
    (work / "frames").mkdir(parents=True)
    (work / "cover.jpg").write_bytes(b"\xff\xd8cover")
    (work / "frames" / "f01.jpg").write_bytes(b"\xff\xd8frame")
    if video:
        (work / "video.mp4").write_bytes(b"\x00" * 1000)
    record = {
        "shortcode": code,
        "meta": {"owner": "creator_" + code.lower(), "url": f"https://www.instagram.com/reel/{code}/",
                 "caption": "cap", "posted_at": "2026-11-05T10:00:00"},
        "duration": 12.0,
        "transcript": {"language": "en", "text": "Stop posting every day.", "segments": []},
        "frames": [{"t": 1.0, "file": "frames/f01.jpg"}],
        "video": "video.mp4" if video else None,
        "cover": "cover.jpg",
        "analysis": a,
    }
    lib.add_pending(code, work)
    return lib.file(code, work, record)


# --------------------------------------------------------------------------
# Library migration
# --------------------------------------------------------------------------

def test_old_database_gains_columns_and_keeps_data(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    db = sqlite3.connect(vault / "index.sqlite")
    db.executescript(OLD_SCHEMA)
    rec = {"shortcode": "OLD1", "meta": {}, "analysis": analysis("Old reel", guess=None), "folder": "library/x/y"}
    db.execute("INSERT INTO items(shortcode,status,folder,title,added_at,analyzed_at,data) VALUES(?,?,?,?,?,?,?)",
               ("OLD1", "done", "vault/library/x/y", "Old reel", "2026-01-01", "2026-01-01", json.dumps(rec)))
    db.commit()
    db.close()

    lib = Library(vault)
    cols = {r[1] for r in lib.db.execute("PRAGMA table_info(items)")}
    assert {"purposes", "purpose_guess", "purpose_confirmed", "extra"} <= cols
    row = lib.row("OLD1")
    assert row["analysis"]["title"] == "Old reel"
    assert row["purposes"] == [] and row["purpose_guess"] is None and row["purpose_confirmed"] is False
    assert row["extra"] == {}
    assert [r["shortcode"] for r in lib.unguessed()] == ["OLD1"]
    lib.db.close()
    Library(vault).db.close()  # opening again is fine (no duplicate columns)


def test_real_vault_database_migrates(tmp_path):
    real = Path(__file__).resolve().parents[1] / "vault" / "index.sqlite"
    if not real.exists():
        pytest.skip("no sample vault in this checkout")
    (tmp_path / "vault").mkdir()
    shutil.copy(real, tmp_path / "vault" / "index.sqlite")
    before = sqlite3.connect(tmp_path / "vault" / "index.sqlite").execute("SELECT shortcode, data FROM items").fetchall()
    lib = Library(tmp_path / "vault")
    after = lib.db.execute("SELECT shortcode, data FROM items").fetchall()
    assert [tuple(r) for r in after] == before
    assert all(r["purposes"] == [] for r in lib.rows_done())
    lib.db.close()


def test_new_reel_takes_the_ai_guess(tmp_path):
    lib = Library(tmp_path / "vault")
    file_reel(lib, tmp_path, "NEW1", analysis("New reel", guess="format"))
    row = lib.row("NEW1")
    assert row["purposes"] == ["format"]
    assert row["purpose_guess"] == {"purpose": "format", "reason": "Because."}
    assert row["purpose_confirmed"] is False
    lib.db.close()


def test_guess_skips_sorted_reels_and_keeps_remake(tmp_path):
    lib = Library(tmp_path / "vault")
    for code in ("OLDA", "OLDB", "OLDC"):
        file_reel(lib, tmp_path, code, analysis(code, guess=None))
    lib.set_purposes("OLDA", ["learn"], confirmed=True)       # you sorted it yourself: no guess needed
    lib.update_extra("OLDB", lambda e: e.setdefault("remakes", []).append({"script": "x"}))
    lib.set_purposes("OLDB", ["remake"], confirmed=None)      # a remake on a reel you have not sorted
    assert sorted(r["shortcode"] for r in lib.unguessed()) == ["OLDB", "OLDC"]
    assert lib.set_guess("OLDB", {"purpose": "hook", "reason": "r"})
    assert lib.row("OLDB")["purposes"] == ["hook", "remake"]  # the guess does not drop the remake
    assert lib.set_guess("OLDA", {"purpose": "hook", "reason": "r"})
    assert lib.row("OLDA")["purposes"] == ["learn"]           # confirmed purposes never change
    lib.db.close()


def test_add_record_accepts_a_breakdown_from_before_0_4(tmp_path):
    from reelvault.config import Config
    from reelvault.manual import add_record

    cfg = Config(vault_dir=tmp_path / "vault")
    lib = Library(cfg.vault_dir)
    rp = tmp_path / "old.json"
    rp.write_text(json.dumps({"shortcode": "OLDREC", "meta": {"owner": "x"}, "analysis": analysis("Old", guess=None)}))
    add_record(cfg, lib, rp)
    row = lib.row("OLDREC")
    assert row["purpose_guess"] is None and row["purposes"] == []
    assert row["analysis"]["claims_to_check"] == [] and row["analysis"]["client_explainer"] == ""
    assert [r["shortcode"] for r in lib.unguessed()] == ["OLDREC"]
    bad = analysis("Bad")
    del bad["skeleton"]
    rp.write_text(json.dumps({"shortcode": "BAD", "analysis": bad}))
    with pytest.raises(Exception):
        add_record(cfg, lib, rp)                              # a broken shape still fails fast
    lib.db.close()


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

@pytest.fixture()
def api(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("REELVAULT_HOME", str(home))
    import reelvault.app as app
    app = importlib.reload(app)
    app.prepare_home()
    vault = home / "vault"
    vault.mkdir(exist_ok=True)
    # Start from a pre-0.4 database, then file new reels into it.
    db = sqlite3.connect(vault / "index.sqlite")
    db.executescript(OLD_SCHEMA)
    db.close()
    lib = Library(vault)
    file_reel(lib, tmp_path, "HOOK1", analysis("Hook reel", guess="hook"))
    file_reel(lib, tmp_path, "LEARN1", analysis("Learn reel", guess="learn", category="Tech & AI"), video=False)
    file_reel(lib, tmp_path, "OLD1", analysis("Old reel", guess=None))
    lib.db.close()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", **(headers or {})}
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        ctype = r.getheader("Content-Type") or ""
        return r.status, (json.loads(raw) if "json" in ctype else raw), ctype

    call.app = app
    call.home = home
    yield call
    srv.shutdown()
    srv.server_close()


def wait_job(app, seconds=10):
    end = time.time() + seconds
    while app.JOB["name"] and time.time() < end:
        time.sleep(0.05)
    assert not app.JOB["name"], "job did not finish"


def test_reels_list_and_media_urls(api):
    code, body, _ = api("GET", "/api/reels")
    assert code == 200
    ids = [r["id"] for r in body["reels"]]
    assert set(ids) == {"HOOK1", "LEARN1", "OLD1"}
    assert body["categories"] == ["Marketing & Sales", "Tech & AI"]
    assert "creator_hook1" in body["creators"]
    hook = next(r for r in body["reels"] if r["id"] == "HOOK1")
    for k in ("title", "owner", "url", "category", "subcategory", "tags", "duration", "thumb", "posted_at",
              "analyzed_at", "purposes", "purpose_guess", "purpose_confirmed", "summary", "hook",
              "structure_name", "cta_type", "content_format", "has_remake"):
        assert k in hook, k
    assert hook["purposes"] == ["hook"] and hook["purpose_confirmed"] is False and hook["has_remake"] is False
    assert hook["hook"]["hook_type"] == "contrarian"
    # The folder is "Marketing - Sales" ('&' replaced, with spaces): the URL must still open the file.
    assert "Marketing%20-%20Sales" in hook["thumb"]
    code, raw, _ = api("GET", hook["thumb"])
    assert code == 200 and raw == b"\xff\xd8cover"

    code, full, _ = api("GET", "/api/reels/HOOK1")
    assert code == 200
    assert full["analysis"]["title"] == "Hook reel" and full["transcript"]["text"]
    assert full["extra"] == {} and full["purposes"] == ["hook"]
    for url in [full["media"]["video"], full["media"]["thumb"], *full["media"]["frames"]]:
        assert api("GET", url)[0] == 200, url
    code, learn, _ = api("GET", "/api/reels/LEARN1")
    assert learn["media"]["video"] is None and learn["media"]["thumb"]

    code, err, _ = api("GET", "/api/reels/NOPE")
    assert code == 404 and "error" in err


def test_set_purposes_confirms_and_needs_one(api):
    code, body, _ = api("POST", "/api/reels/HOOK1/purposes", {"purposes": ["format", "hook", "format"]})
    assert code == 200 and body["purposes"] == ["hook", "format"]
    _, full, _ = api("GET", "/api/reels/HOOK1")
    assert full["purposes"] == ["hook", "format"] and full["purpose_confirmed"] is True
    assert full["purpose_guess"]["purpose"] == "hook"  # the guess is kept

    code, err, _ = api("POST", "/api/reels/HOOK1/purposes", {"purposes": []})
    assert code == 400 and "at least one" in err["error"]
    code, err, _ = api("POST", "/api/reels/HOOK1/purposes", {"purposes": ["dance"]})
    assert code == 400
    code, err, _ = api("POST", "/api/reels/NOPE/purposes", {"purposes": ["learn"]})
    assert code == 404
    _, full, _ = api("GET", "/api/reels/HOOK1")
    assert full["purposes"] == ["hook", "format"]  # nothing changed by the bad calls


def test_todo_ticks(api):
    code, todos, _ = api("GET", "/api/todos")
    assert code == 200 and isinstance(todos, list)
    assert [(t["reel_id"], t["index"], t["done"]) for t in todos] == [("LEARN1", 0, False)]
    assert todos[0]["step"] == SAMPLE["action_plan"][0]["step"]

    code, body, _ = api("POST", "/api/reels/LEARN1/todo", {"index": 0, "done": True})
    assert code == 200 and body["todo_done"] == [0]
    assert api("GET", "/api/todos")[1] == []
    _, all_todos, _ = api("GET", "/api/todos?all=1")
    assert all_todos[0]["done"] is True
    assert api("GET", "/api/reels/LEARN1")[1]["extra"]["todo_done"] == [0]

    api("POST", "/api/reels/LEARN1/todo", {"index": 0, "done": False})
    assert len(api("GET", "/api/todos")[1]) == 1
    assert api("POST", "/api/reels/LEARN1/todo", {"index": 9, "done": True})[0] == 400
    assert api("POST", "/api/reels/LEARN1/todo", {"done": True})[0] == 400


def test_banks(api):
    _, body, _ = api("GET", "/api/banks/hooks")
    assert [h["reel_id"] for h in body["hooks"]] == ["HOOK1"]
    h = body["hooks"][0]
    assert h["spoken"] == "Stop posting every day." and h["hook_type"] == "contrarian"
    assert h["formula"] == "Stop [COMMON ADVICE]."
    assert len(h["variations"]) == 5
    _, body, _ = api("GET", "/api/banks/hooks?all=1")
    assert len(body["hooks"]) == 3

    _, body, _ = api("GET", "/api/banks/formats")
    assert body["formats"] == []
    api("POST", "/api/reels/LEARN1/purposes", {"purposes": ["learn", "format"]})
    _, body, _ = api("GET", "/api/banks/formats")
    f = body["formats"][0]
    assert f["reel_id"] == "LEARN1" and f["beats"] == ["reason", "step"]
    assert f["structure_name"] and f["template"] and f["script_ideas"] and f["pacing"]
    assert len(api("GET", "/api/banks/formats?all=1")[1]["formats"]) == 3


def test_remake_job_stores_a_pack(api, monkeypatch):
    seen = {}

    def fake_structured(prompt, instruction, system, cwd, model_cls, read_files, model, effort):
        seen.update(prompt=prompt, system=system, cls=model_cls)
        return RemakePack(core_idea="Post less, better.", whats_weak=["No proof"], your_angle="Ads angle",
                          script="HOOK: Stop boosting posts. [VISUAL: ads manager]", shot_list=["Talking head"],
                          caption="Comment ADS", hashtags=["#ads"], too_close=[{"line": "Stop posting every day.",
                                                                                "why": "Same opening words"}],
                          made_for="someone else")

    monkeypatch.setattr(brain, "structured", fake_structured)
    (api.home / "profile.md").write_text("I run a paid-ads agency.", encoding="utf-8")
    code, body, _ = api("POST", "/api/reels/HOOK1/remake", {"for": "Acme Dental", "notes": "Keep it short"})
    assert code == 200 and body["job"] == "Make it better: Hook reel"
    wait_job(api.app)
    assert seen["cls"] is RemakePack
    assert "I run a paid-ads agency." in seen["prompt"] and "Acme Dental" in seen["prompt"]
    assert "Keep it short" in seen["prompt"] and "Never borrow" in seen["system"]

    _, full, _ = api("GET", "/api/reels/HOOK1")
    packs = full["extra"]["remakes"]
    assert len(packs) == 1 and packs[0]["made_for"] == "Acme Dental" and packs[0]["created_at"]
    assert packs[0]["too_close"][0]["line"] == "Stop posting every day."
    assert full["purposes"] == ["hook", "remake"]
    reels = {r["id"]: r for r in api("GET", "/api/reels")[1]["reels"]}
    assert reels["HOOK1"]["has_remake"] is True
    assert api("POST", "/api/reels/NOPE/remake", {})[0] == 404


def test_batch_guess_for_older_reels(api, monkeypatch):
    calls = []

    def fake_structured(prompt, instruction, system, cwd, model_cls, read_files, model, effort):
        calls.append((prompt, model_cls, effort))
        return PurposeGuesses(guesses=[{"id": "OLD1", "purpose": "format", "reason": "Clear structure"},
                                       {"id": "HOOK1", "purpose": "learn", "reason": "not asked"},
                                       {"id": "GHOST", "purpose": "hook", "reason": "not a reel"}])

    monkeypatch.setattr(brain, "structured", fake_structured)
    code, body, _ = api("POST", "/api/sort/guess", {})
    assert code == 200 and body["count"] == 1
    wait_job(api.app)
    assert len(calls) == 1 and calls[0][1] is PurposeGuesses and calls[0][2] == "low"
    assert "OLD1" in calls[0][0] and "HOOK1" not in calls[0][0]

    _, old, _ = api("GET", "/api/reels/OLD1")
    assert old["purpose_guess"] == {"purpose": "format", "reason": "Clear structure"}
    assert old["purposes"] == ["format"] and old["purpose_confirmed"] is False
    _, hook, _ = api("GET", "/api/reels/HOOK1")
    assert hook["purpose_guess"]["purpose"] == "hook"  # reels that already had a guess are untouched
    assert api("POST", "/api/sort/guess", {})[1] == {"ok": True, "count": 0}
    assert len(calls) == 1


def test_ui_is_served_and_guarded(api, monkeypatch, tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>ReelVault</title>", encoding="utf-8")
    (ui / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(api.app, "UI_DIR", ui)
    code, raw, ctype = api("GET", "/")
    assert code == 200 and b"ReelVault" in raw and ctype.startswith("text/html")
    code, raw, ctype = api("GET", "/ui/app.js")
    assert code == 200 and ctype.startswith("text/javascript")
    assert api("GET", "/ui/../app.py")[0] == 404
    assert api("GET", "/ui/%2e%2e/%2e%2e/pyproject.toml")[0] == 404
    monkeypatch.setattr(api.app, "UI_DIR", tmp_path / "missing")
    code, raw, _ = api("GET", "/")
    assert code == 200 and b"installer again" in raw
    # Other websites cannot drive the new endpoints.
    assert api("POST", "/api/reels/HOOK1/purposes", {"purposes": ["learn"]},
               headers={"Origin": "https://evil.example"})[0] == 403


def test_existing_endpoints_still_answer(api):
    code, st, _ = api("GET", "/api/status")
    assert code == 200 and st["version"] == api.app.__version__ and "last_update" in st
    assert api("POST", "/api/remix", {"topic": ""})[1] == {"error": "Type what the new video is about."}
    assert api("POST", "/api/nope", {})[0] == 404


def test_app_module_still_starts_when_run():
    import reelvault.app as app

    src = Path(app.__file__).read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":\n    main()' in src  # `pythonw -m reelvault.app` needs it
