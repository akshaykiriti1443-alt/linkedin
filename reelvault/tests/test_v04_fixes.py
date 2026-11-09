"""ReelVault 0.4 review fixes: job ids, safe migration and ticks, scripts command line, media and update edge cases.

Claude, GitHub, pip and ffmpeg are faked.
"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import pytest

from reelvault import brain, media, update
from reelvault.config import Config
from reelvault.library import Library
from test_v04_api import OLD_SCHEMA, analysis, api, file_reel  # noqa: F401  (api is a fixture)


def old_vault(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path / "index.sqlite")
    db.executescript(OLD_SCHEMA)
    db.close()
    return path


# --------------------------------------------------------------------------
# Library: several requests at once
# --------------------------------------------------------------------------

def test_migration_is_safe_when_several_requests_open_the_vault_at_once(tmp_path):
    for trial in range(8):
        vault = old_vault(tmp_path / f"t{trial}")
        errors, gate = [], threading.Barrier(5)

        def open_it():
            gate.wait()
            try:
                Library(vault).db.close()
            except Exception as e:  # pragma: no cover - this is the bug
                errors.append(repr(e))

        threads = [threading.Thread(target=open_it) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors == []
        cols = {r[1] for r in sqlite3.connect(vault / "index.sqlite").execute("PRAGMA table_info(items)")}
        assert {"purposes", "purpose_guess", "purpose_confirmed", "extra"} <= cols


def test_two_ticks_at_once_both_stay(tmp_path):
    vault = tmp_path / "vault"
    lib = Library(vault)
    file_reel(lib, tmp_path, "LEARN1", analysis("Learn reel", guess="learn"))
    lib.db.close()
    for _ in range(15):
        Library(vault).update_extra("LEARN1", lambda e: e.update(todo_done=[]))
        gate = threading.Barrier(2)

        def tick(i):
            lib = Library(vault)
            gate.wait()
            lib.update_extra("LEARN1", lambda e: e.update(todo_done=sorted(set(e.get("todo_done", [])) | {i})))
            lib.db.close()

        threads = [threading.Thread(target=tick, args=(i,)) for i in (0, 1)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert Library(vault).row("LEARN1")["extra"]["todo_done"] == [0, 1]


# --------------------------------------------------------------------------
# Jobs: each has an id, and status says how it ended
# --------------------------------------------------------------------------

def test_jobs_have_ids_and_status_says_how_they_ended(api):
    app = api.app
    gate = threading.Event()
    job_a = app.start_job("Job A", lambda: gate.wait(5) and {"script": "scripts/a.md"}, tag="remix")
    assert job_a
    assert not app.start_job("Job B", lambda: None)  # busy
    _, s, _ = api("GET", "/api/status")
    assert s["job"] == "Job A" and s["job_id"] == job_a and s["job_tag"] == "remix"
    gate.set()
    end = time.time() + 5
    while app.JOB["name"] and time.time() < end:
        time.sleep(0.02)
    _, s, _ = api("GET", "/api/status")
    assert s["job"] is None and s["job_id"] is None
    done = s["finished"][-1]
    assert done == {"id": job_a, "name": "Job A", "tag": "remix", "ok": True,
                    "result": {"script": "scripts/a.md"}, "message": ""}

    def stop():
        raise SystemExit("Connect Instagram first. Go to Settings and click Connect Instagram.")

    job_c = app.start_job("Job C", stop, tag="run")
    assert job_c > job_a
    end = time.time() + 5
    while app.JOB["name"] and time.time() < end:
        time.sleep(0.02)
    _, s, _ = api("GET", "/api/status")
    done = s["finished"][-1]
    assert done["id"] == job_c and done["ok"] is False and done["message"].startswith("Connect Instagram first")


def test_start_endpoints_return_the_job_id(api, monkeypatch):
    import reelvault.remix as remix_mod

    def fake_remix(cfg, lib, topic, *a, **k):
        out = cfg.vault_dir / "scripts" / "x.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(topic, encoding="utf-8")
        return out

    monkeypatch.setattr(remix_mod, "remix", fake_remix)
    code, r, _ = api("POST", "/api/remix", {"topic": "Ads for gyms", "count": 1})
    assert code == 200 and r["ok"] and r["job_id"]
    end = time.time() + 5
    while api.app.JOB["name"] and time.time() < end:
        time.sleep(0.02)
    _, s, _ = api("GET", "/api/status")
    assert s["finished"][-1]["id"] == r["job_id"] and s["finished"][-1]["result"] == {"script": "scripts/x.md"}
    code, r, _ = api("POST", "/api/remix", {"topic": "x" * 5000})
    assert "too long" in r["error"]


# --------------------------------------------------------------------------
# Write scripts: the topic stays off the command line; runs never overwrite each other
# --------------------------------------------------------------------------

def test_scripts_keep_the_topic_off_the_command_line_and_never_overwrite(tmp_path, monkeypatch):
    from reelvault import remix

    cfg = Config(vault_dir=tmp_path / "vault", profile_file=tmp_path / "profile.md")
    lib = Library(cfg.vault_dir)
    file_reel(lib, tmp_path, "HOOK1", analysis("Hook reel"))
    calls = []
    monkeypatch.setattr(brain, "text", lambda prompt, instruction, *a: calls.append((prompt, instruction)) or "## Script")
    topic = 'Ads for gyms. Start with a hook like this one: "Stop boosting posts & do this instead"'
    first = remix.remix(cfg, lib, topic, [], count=3)
    second = remix.remix(cfg, lib, topic, [], count=3)
    prompt, instruction = calls[0]
    assert topic in prompt
    assert not any(c in instruction for c in '"&|<>^%') and "gyms" not in instruction
    assert first != second and first.exists() and second.exists()
    assert second.name.endswith("-2.md")


# --------------------------------------------------------------------------
# Media and files
# --------------------------------------------------------------------------

def test_reel_without_a_video_key_still_shows_its_video(api, tmp_path):
    app = api.app
    folder = tmp_path / "root" / "library" / "A & B" / "reel"
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"\x00")
    rec = {"folder": "library/A & B/reel", "frames": []}       # the first version wrote no "video" key
    assert app.reel_media(rec, tmp_path / "root")["video"].endswith("/video.mp4")
    assert app.reel_media(dict(rec, video=None), tmp_path / "root")["video"] is None  # a manual record


def test_browser_hanging_up_on_a_video_is_not_an_error(api, tmp_path):
    app = api.app
    f = tmp_path / "video.mp4"
    f.write_bytes(b"\x00" * 200_000)

    class Gone(io.RawIOBase):
        def write(self, b):
            raise ConnectionAbortedError(10053, "An established connection was aborted")

    h = app.Handler.__new__(app.Handler)
    h.headers = {"Range": "bytes=0-"}
    h.wfile = Gone()
    h.send_response = h.send_header = h.end_headers = lambda *a: None
    app.LOG.clear()
    app.Handler._send_file(h, f, "video/mp4")  # must not raise
    assert not any("Error" in line for line in app.LOG)


def test_ffmpeg_calls_open_no_console_window(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"streams": [], "format": {}}) if kw.get("text")
                                           else b"", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    monkeypatch.setattr(media, "_need", lambda tool: tool)
    media.probe(tmp_path / "v.mp4")
    media.extract_frames(tmp_path / "v.mp4", tmp_path / "frames", [0.5], 320)
    pytest.importorskip("numpy")
    media.load_audio(tmp_path / "v.mp4")
    assert len(seen) == 3 and all("creationflags" in kw for kw in seen)


# --------------------------------------------------------------------------
# Self-update edge cases
# --------------------------------------------------------------------------

def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("linkedin-x/reelvault/pyproject.toml", "[project]\nname='reelvault'\n")
        z.writestr("linkedin-x/reelvault/reelvault/__init__.py", "__version__ = '0.4.1'\n")
    return buf.getvalue()


def test_update_download_explains_a_locked_folder_plainly(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "source" / "old").mkdir(parents=True)
    monkeypatch.setattr(update, "_get", lambda url, timeout=20: _zip_bytes())

    def locked(path):
        raise PermissionError(5, "Access is denied", str(path / "old"))

    monkeypatch.setattr(update, "_rmtree", locked)
    with pytest.raises(update.UpdateError) as e:
        update.download(home)
    assert str(e.value).startswith("Could not save the update on this computer.")
    assert "Access is denied" in e.value.detail and "Errno" not in str(e.value)
    assert not (home / "source-download.zip").exists()
    monkeypatch.undo()
    monkeypatch.setattr(update, "_get", lambda url, timeout=20: _zip_bytes())
    assert update.download(home).name == "reelvault"  # works again once the folder is free


def test_relaunched_app_writes_app_log(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(update.subprocess, "Popen", lambda cmd, **kw: seen.update(kw))
    update.relaunch(tmp_path, 8899)
    assert Path(seen["stdout"].name) == tmp_path / "app.log" and seen["stderr"] is seen["stdout"]
    assert seen["stdout"].closed  # the helper does not keep the file open


def test_pip_output_is_read_as_utf8(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    update._pip(["--version"], lambda m: None)
    assert seen["env"]["PYTHONUTF8"] == "1" and seen["env"]["PYTHONIOENCODING"] == "utf-8"
    assert seen["encoding"] == "utf-8"
