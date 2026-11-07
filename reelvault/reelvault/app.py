"""ReelVault desktop app: a control panel that opens in your browser.

Start it by double-clicking the ReelVault shortcut (it runs `pythonw -m reelvault.app`).
Everything lives in  %USERPROFILE%\\ReelVault  (or $REELVAULT_HOME).

    python -m reelvault.app            open the control panel
    python -m reelvault.app --run-once fetch + analyze once, then exit (used by the daily schedule)
"""

from __future__ import annotations

import collections
import io
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

PORT = int(os.environ.get("REELVAULT_PORT", "8765"))
HOME = Path(os.environ.get("REELVAULT_HOME", Path.home() / "ReelVault")).resolve()
TASK_NAME = "ReelVault daily"
IS_WIN = sys.platform == "win32"


# --------------------------------------------------------------------------
# Setup: data folder, settings file, tools on PATH
# --------------------------------------------------------------------------

def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[\n" + "".join(f"    {json.dumps(x, ensure_ascii=False)},\n" for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def save_settings(updates: dict) -> None:
    import tomllib

    p = HOME / "reelvault.toml"
    data = tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data.update(updates)
    p.write_text("".join(f"{k} = {_toml_value(v)}\n" for k, v in data.items()), encoding="utf-8")


def prepare_home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    os.chdir(HOME)
    if not (HOME / "reelvault.toml").exists():
        from .config import DEFAULT_CATEGORIES

        save_settings({
            "vault_dir": "vault",
            "instagram_username": "",
            "session_file": str(HOME / "instagram.session"),
            "brain": "claude-code",
            "model": "claude-opus-5-5",
            "effort": "medium",
            "whisper_model": "small",
            "max_frames": 12,
            "categories": DEFAULT_CATEGORIES,
        })
    if not (HOME / "profile.md").exists():
        from .cli import PROFILE_TEMPLATE

        (HOME / "profile.md").write_text(PROFILE_TEMPLATE, encoding="utf-8")
    # ffmpeg downloaded by the installer lives here
    for extra in (HOME / "tools" / "ffmpeg" / "bin", Path.home() / ".local" / "bin"):
        if extra.exists() and str(extra) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = str(extra) + os.pathsep + os.environ.get("PATH", "")


def cfg_lib():
    from .config import load_config
    from .library import Library

    cfg = load_config(HOME / "reelvault.toml")
    cfg.vault_dir = (HOME / cfg.vault_dir).resolve() if not cfg.vault_dir.is_absolute() else cfg.vault_dir
    cfg.profile_file = HOME / "profile.md"
    return cfg, Library(cfg.vault_dir)


# --------------------------------------------------------------------------
# Background jobs with a live log
# --------------------------------------------------------------------------

LOG: collections.deque[str] = collections.deque(maxlen=400)
JOB = {"name": None, "started": None, "result": None}
JOB_LOCK = threading.Lock()


class _LogWriter(io.TextIOBase):
    def __init__(self):
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            LOG.append(line)
        return len(s)


def log(msg: str) -> None:
    LOG.append(f"{datetime.now():%H:%M} {msg}")


def start_job(name: str, fn, *args) -> bool:
    with JOB_LOCK:
        if JOB["name"]:
            return False
        JOB.update(name=name, started=time.time(), result=None)

    def runner():
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _LogWriter()
        log(f"Started: {name}")
        try:
            JOB["result"] = fn(*args)
            log(f"Finished: {name}")
        except SystemExit as e:
            log(f"Stopped: {e}")
        except Exception as e:  # show any failure in the panel instead of crashing the app
            log(f"Error: {e}")
            LOG.extend(traceback.format_exc().splitlines()[-6:])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            with JOB_LOCK:
                JOB["name"] = None

    threading.Thread(target=runner, daemon=True).start()
    return True


def rebuild_outputs(lib) -> None:
    from .kb import export
    from .site import build

    build(lib)
    build(lib, standalone=True)
    export(lib)
    print("Library page and knowledge base updated.")


def job_fetch_and_analyze(limit: int | None):
    from . import fetch
    from .analyze import process_pending

    cfg, lib = cfg_lib()
    print("Checking your Saved reels on Instagram...")
    n = fetch.from_saved(cfg, lib, limit)
    print(f"Downloaded {n} new reel(s).")
    ok, bad = process_pending(cfg, lib)
    print(f"Analyzed {ok}, failed {bad}.")
    rebuild_outputs(lib)
    return {"new": n, "ok": ok, "bad": bad}


def job_links(urls: list[str]):
    from . import fetch
    from .analyze import process_pending

    cfg, lib = cfg_lib()
    print(f"Downloading {len(urls)} link(s)...")
    if (HOME / "instagram.session").exists() or cfg.session_file and Path(cfg.session_file).exists():
        n = fetch.from_links(cfg, lib, urls)
    else:
        n = fetch.from_urls(cfg, lib, urls)
    print(f"Downloaded {n} new reel(s).")
    ok, bad = process_pending(cfg, lib)
    print(f"Analyzed {ok}, failed {bad}.")
    rebuild_outputs(lib)
    return {"new": n, "ok": ok, "bad": bad}


def job_retry():
    from .analyze import process_pending

    cfg, lib = cfg_lib()
    ok, bad = process_pending(cfg, lib, retry_failed=True)
    print(f"Analyzed {ok}, failed {bad}.")
    rebuild_outputs(lib)


def job_remix(topic: str, category: str | None, count: int):
    from .remix import remix

    cfg, lib = cfg_lib()
    out = remix(cfg, lib, topic, [], category or None, None, count)
    print(f"Scripts saved: {out.name}")
    return {"script": out.relative_to(cfg.vault_dir).as_posix()}


# --------------------------------------------------------------------------
# Status checks
# --------------------------------------------------------------------------

_claude_cache = {"t": 0.0, "v": None}


def claude_status() -> dict:
    from .brain import find_claude

    if time.time() - _claude_cache["t"] < 20 and _claude_cache["v"]:
        return _claude_cache["v"]
    exe = find_claude()
    v = {"installed": bool(exe), "loggedIn": False}
    if exe:
        try:
            flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
            out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=20,
                                 creationflags=flags).stdout
            info = json.loads(out[out.find("{"):]) if "{" in out else {}
            v["loggedIn"] = bool(info.get("loggedIn"))
        except Exception:
            pass
    _claude_cache.update(t=time.time(), v=v)
    return v


_sched_cache = {"t": 0.0, "v": None}


def schedule_status() -> str | None:
    if not IS_WIN:
        return None
    if time.time() - _sched_cache["t"] < 30:
        return _sched_cache["v"]
    try:
        out = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True,
                             text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except Exception:
        return None
    m = re.search(r"Next Run Time:\s*(.+)", out)
    _sched_cache.update(t=time.time(), v=m.group(1).strip() if m else None)
    return _sched_cache["v"]


def status() -> dict:
    import shutil

    cfg, lib = cfg_lib()
    session = Path(cfg.session_file) if cfg.session_file else HOME / "instagram.session"
    cats = sorted({r["analysis"]["category"] for r in lib.all_done()})
    counts = lib.stats()
    lib.db.close()
    return {
        "home": str(HOME),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "claude": claude_status(),
        "instagram": {"username": cfg.instagram_username, "connected": session.exists()},
        "counts": counts,
        "categories": cats,
        "job": JOB["name"],
        "result": JOB["result"],
        "log": list(LOG)[-200:],
        "schedule": schedule_status(),
        "profile": (HOME / "profile.md").read_text(encoding="utf-8"),
        "library": (cfg.vault_dir / "index.html").exists(),
    }


# --------------------------------------------------------------------------
# Instagram login (password + 2FA; only the session is saved)
# --------------------------------------------------------------------------

_pending_ig = {"L": None, "user": None}


def _ig_save(L, user: str) -> None:
    path = HOME / "instagram.session"
    L.save_session_to_file(str(path))
    save_settings({"instagram_username": user, "session_file": str(path)})
    log(f"Instagram connected as @{user}.")


def ig_login(user: str, password: str) -> dict:
    import instaloader
    from instaloader.exceptions import (BadCredentialsException, ConnectionException,
                                        TwoFactorAuthRequiredException)

    user = user.strip().lstrip("@")
    L = instaloader.Instaloader(quiet=True)
    try:
        L.login(user, password)
    except TwoFactorAuthRequiredException:
        _pending_ig.update(L=L, user=user)
        return {"need2fa": True}
    except BadCredentialsException:
        return {"error": "Wrong username or password."}
    except ConnectionException as e:
        if "checkpoint" in str(e).lower() or "challenge" in str(e).lower():
            return {"error": "Instagram wants to confirm it's you. Open the Instagram app, tap 'This was me', "
                             "then try again."}
        return {"error": f"Instagram refused the login: {e}"}
    _ig_save(L, user)
    return {"ok": True}


def ig_2fa(code: str) -> dict:
    from instaloader.exceptions import BadCredentialsException

    L, user = _pending_ig["L"], _pending_ig["user"]
    if not L:
        return {"error": "Start the login again."}
    try:
        L.two_factor_login(code.strip())
    except BadCredentialsException:
        return {"error": "That code didn't work. Try the newest code."}
    _ig_save(L, user)
    _pending_ig.update(L=None, user=None)
    return {"ok": True}


# --------------------------------------------------------------------------
# Claude sign-in and the daily schedule
# --------------------------------------------------------------------------

def claude_signin() -> dict:
    from .brain import find_claude

    exe = find_claude()
    if not exe:
        return {"error": "Claude Code is not installed yet. Run the installer again."}
    if IS_WIN:
        # A small window opens; Claude's sign-in page opens in your browser.
        subprocess.Popen(["cmd", "/c", "start", "Sign in to Claude", "cmd", "/k", exe, "auth", "login", "--claudeai"])
    else:
        subprocess.Popen([exe, "auth", "login", "--claudeai"])
    _claude_cache["t"] = 0
    return {"ok": True}


def pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def set_schedule(hhmm: str | None) -> dict:
    if not IS_WIN:
        return {"error": "Daily runs are set up on Windows only."}
    flags = subprocess.CREATE_NO_WINDOW
    if not hhmm:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, creationflags=flags)
        return {"ok": True}
    if not re.fullmatch(r"\d{2}:\d{2}", hhmm):
        return {"error": "Pick a time like 09:00."}
    cmd = f'"{pythonw()}" -m reelvault.app --run-once'
    _sched_cache["t"] = 0
    r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/TN", TASK_NAME, "/ST", hhmm, "/TR", cmd],
                       capture_output=True, text=True, creationflags=flags)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()}
    return {"ok": True}


def run_once() -> None:
    prepare_home()
    logdir = HOME / "logs"
    logdir.mkdir(exist_ok=True)
    with open(logdir / f"run-{datetime.now():%Y-%m-%d}.txt", "a", encoding="utf-8") as f:
        sys.stdout = sys.stderr = f
        print(f"--- {datetime.now():%Y-%m-%d %H:%M} ---")
        try:
            job_fetch_and_analyze(30)
        except SystemExit as e:
            print(f"Stopped: {e}")
        except Exception:
            traceback.print_exc()


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _local_only(self) -> bool:
        # Block other websites from driving the app from your browser.
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        ok = host in ("127.0.0.1", "localhost") and (not origin or urlparse(origin).hostname in ("127.0.0.1", "localhost"))
        if not ok:
            self._json({"error": "forbidden"}, 403)
        return ok

    def do_GET(self):
        if not self._local_only():
            return
        path = unquote(urlparse(self.path).path)
        if path == "/":
            body = PANEL.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self._json(status())
        elif path.startswith("/vault/"):
            self._file(path[len("/vault/"):])
        else:
            self._json({"error": "not found"}, 404)

    def _file(self, rel: str):
        from .config import load_config

        cfg = load_config(HOME / "reelvault.toml")
        cfg.vault_dir = cfg.vault_dir if cfg.vault_dir.is_absolute() else HOME / cfg.vault_dir
        root = cfg.vault_dir.resolve()
        p = (root / rel).resolve()
        if root not in p.parents and p != root or not p.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix == ".md":
            ctype = "text/plain; charset=utf-8"
        size = p.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else end
            else:
                start = max(size - int(m.group(2)), 0)
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_POST(self):
        if not self._local_only():
            return
        path = urlparse(self.path).path
        b = self._body()
        busy = {"error": "ReelVault is busy with another task. Wait for it to finish."}
        if path == "/api/run":
            limit = int(b.get("limit") or 20)
            self._json({"ok": True} if start_job("Get new saved reels", job_fetch_and_analyze, limit) else busy)
        elif path == "/api/links":
            from .fetch import SHORTCODE_RE

            urls = [m.group(0) for m in SHORTCODE_RE.finditer(b.get("text", ""))]
            if not urls:
                return self._json({"error": "No Instagram reel links found. Paste links like instagram.com/reel/..."})
            self._json({"ok": True, "count": len(urls)} if start_job("Analyze pasted links", job_links, urls) else busy)
        elif path == "/api/retry":
            self._json({"ok": True} if start_job("Retry failed reels", job_retry) else busy)
        elif path == "/api/remix":
            topic = (b.get("topic") or "").strip()
            if not topic:
                return self._json({"error": "Type what the new video is about."})
            ok = start_job("Write scripts", job_remix, topic, b.get("category"), int(b.get("count") or 3))
            self._json({"ok": True} if ok else busy)
        elif path == "/api/ig/login":
            self._json(ig_login(b.get("username", ""), b.get("password", "")))
        elif path == "/api/ig/2fa":
            self._json(ig_2fa(b.get("code", "")))
        elif path == "/api/claude/signin":
            self._json(claude_signin())
        elif path == "/api/schedule":
            self._json(set_schedule(b.get("time")))
        elif path == "/api/profile":
            (HOME / "profile.md").write_text(b.get("text", ""), encoding="utf-8")
            self._json({"ok": True})
        elif path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)


def port_in_use() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--run-once" in argv:
        return run_once()
    url = f"http://127.0.0.1:{PORT}/"
    if port_in_use():  # already running: just show it
        webbrowser.open(url)
        return
    prepare_home()
    if sys.stdout is None:  # pythonw has no console
        sys.stdout = sys.stderr = open(HOME / "app.log", "a", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if "--no-browser" not in argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"ReelVault is running at {url}")
    server.serve_forever()


PANEL = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReelVault</title>
<style>
:root{--bg:#f2f3f6;--panel:#fff;--ink:#151a2b;--muted:#5f6577;--line:#dfe1e8;--chip:#e9ebf1;--accent:#3d3fd1;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;
--font:"Segoe UI",system-ui,-apple-system,sans-serif;--mono:Consolas,ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#10121a;--panel:#191c27;--ink:#e8eaf2;--muted:#9aa0b4;--line:#2a2e3d;--chip:#232737;--accent:#8f91ff;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;color-scheme:dark}}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--font)}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 40px;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}
@media (max-width:820px){.wrap{grid-template-columns:minmax(0,1fr)}}
header{grid-column:1/-1;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
h1{margin:0;font-size:24px}h1 small{font-size:14px;color:var(--muted);font-weight:400;margin-left:8px}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;display:flex;flex-direction:column;gap:10px;min-width:0}
section.wide{grid-column:1/-1}
h2{margin:0;font-size:16px}p{margin:0;color:var(--muted);font-size:14px}
.steps{display:grid;gap:8px}
.step{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:8px 0;border-top:1px solid var(--line)}
.step:first-child{border-top:0}.step .name{flex:1 1 200px}
.pill{font-size:12px;font-weight:600;border-radius:99px;padding:2px 10px;background:var(--chip)}
.pill.ok{color:var(--ok)}.pill.no{color:var(--warn)}
button,.btn{font:inherit;font-weight:600;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;background:var(--accent);color:#fff;text-decoration:none;display:inline-block}
button.ghost{background:var(--chip);color:var(--ink)}
button:disabled{opacity:.5;cursor:default}
input,textarea,select{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;width:100%;min-width:0}
textarea{min-height:90px;resize:vertical}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.row>*{flex:1 1 120px}.row>button{flex:0 0 auto}
pre{margin:0;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:300px;overflow:auto;font:12.5px/1.5 var(--mono);white-space:pre-wrap}
.counts{display:flex;gap:16px;flex-wrap:wrap}.counts b{font-size:22px;display:block}
.msg{font-size:14px;min-height:1.2em}.msg.err{color:var(--bad)}.msg.good{color:var(--ok)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style></head><body>
<div class="wrap">
<header><h1>ReelVault <small id="home"></small></h1>
<div class="row"><a class="btn" id="open-lib" href="/vault/index.html" target="_blank">Open my library</a><button class="ghost" id="quit">Close app</button></div></header>

<section>
<h2>Setup</h2>
<div class="steps">
 <div class="step"><span class="name">1. Video tools</span><span class="pill" id="s-ffmpeg"></span></div>
 <div class="step"><span class="name">2. Claude (your subscription)</span><span class="pill" id="s-claude"></span><button class="ghost" id="claude-signin">Sign in to Claude</button></div>
 <div class="step"><span class="name">3. Instagram <span id="ig-user"></span></span><span class="pill" id="s-ig"></span></div>
</div>
<div id="ig-form" class="steps">
 <p>Your password goes straight to Instagram. ReelVault keeps only the login session on this computer.</p>
 <div class="row"><input id="ig-u" placeholder="Instagram username" autocomplete="username"><input id="ig-p" type="password" placeholder="Password" autocomplete="current-password"><button id="ig-go">Connect</button></div>
 <div class="row" id="ig-2fa" hidden><input id="ig-code" placeholder="6-digit code from your authenticator or SMS" inputmode="numeric"><button id="ig-code-go">Confirm</button></div>
 <div class="msg" id="ig-msg"></div>
</div>
</section>

<section>
<h2>Your library</h2>
<div class="counts"><div><b id="c-done">0</b>analyzed</div><div><b id="c-pending">0</b>waiting</div><div><b id="c-failed">0</b>failed</div></div>
<div class="row"><input id="limit" type="number" min="1" max="200" value="20" aria-label="How many new reels"><button id="run">Get new saved reels</button></div>
<p>Downloads reels you saved on Instagram that aren't in the library yet, then analyzes them.</p>
<div class="row"><button class="ghost" id="retry">Retry failed reels</button></div>
<div class="msg" id="run-msg"></div>
</section>

<section>
<h2>Analyze specific reels</h2>
<p>Paste one or more Instagram reel links, one per line.</p>
<textarea id="links" placeholder="https://www.instagram.com/reel/..."></textarea>
<div class="row"><button id="links-go">Analyze these links</button></div>
<div class="msg" id="links-msg"></div>
</section>

<section>
<h2>Run every day</h2>
<p>ReelVault checks your saved reels each day at this time while the computer is on.</p>
<div class="row"><input id="sched-time" type="time" value="20:00" aria-label="Daily time"><button id="sched-on">Turn on</button><button class="ghost" id="sched-off">Turn off</button></div>
<div class="msg" id="sched-msg"></div>
</section>

<section>
<h2>Write new scripts</h2>
<p>Claude writes scripts for you from the hooks, structures and CTAs in your library.</p>
<input id="topic" placeholder="What is the new video about?">
<div class="row"><select id="cat"><option value="">Any category</option></select><select id="count"><option>1</option><option selected>3</option><option>5</option></select><button id="remix">Write scripts</button></div>
<div class="msg" id="remix-msg"></div>
</section>

<section>
<h2>About you</h2>
<p>Claude reads this to write hooks and scripts that fit your niche.</p>
<textarea id="profile" style="min-height:160px"></textarea>
<div class="row"><button class="ghost" id="profile-save">Save</button></div>
<div class="msg" id="profile-msg"></div>
</section>

<section class="wide">
<h2>Activity <span class="pill" id="job"></span></h2>
<pre id="log">Nothing yet.</pre>
</section>
</div>
<script>
const $ = s => document.querySelector(s);
const post = (u, b) => fetch(u, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(b || {})}).then(r => r.json());
const say = (id, r, good) => { const m = $(id); m.className = "msg " + (r.error ? "err" : "good"); m.textContent = r.error || good; };
let profileLoaded = false, lastResult = null;
function pill(el, ok, yes, no) { el.className = "pill " + (ok ? "ok" : "no"); el.textContent = ok ? yes : no; }
async function refresh() {
  let s; try { s = await (await fetch("/api/status")).json(); } catch (e) { $("#job").textContent = "app closed"; return; }
  $("#home").textContent = s.home;
  pill($("#s-ffmpeg"), s.ffmpeg, "Ready", "Missing: run the installer again");
  pill($("#s-claude"), s.claude.loggedIn, "Signed in", s.claude.installed ? "Not signed in" : "Not installed");
  $("#claude-signin").hidden = s.claude.loggedIn;
  pill($("#s-ig"), s.instagram.connected, "Connected", "Not connected");
  $("#ig-user").textContent = s.instagram.username ? "(@" + s.instagram.username + ")" : "";
  $("#ig-form").hidden = s.instagram.connected;
  for (const k of ["done", "pending", "failed"]) $("#c-" + k).textContent = s.counts[k] || 0;
  $("#open-lib").hidden = !s.library;
  const cat = $("#cat"), have = [...cat.options].map(o => o.value);
  s.categories.forEach(c => { if (!have.includes(c)) cat.add(new Option(c, c)); });
  if (!profileLoaded) { $("#profile").value = s.profile; profileLoaded = true; }
  $("#sched-msg").textContent = s.schedule ? "On. Next run: " + s.schedule : "Off.";
  $("#job").textContent = s.job ? "Working: " + s.job : "Idle";
  document.querySelectorAll("#run,#links-go,#remix,#retry").forEach(b => b.disabled = !!s.job);
  $("#log").textContent = s.log.length ? s.log.join("\n") : "Nothing yet.";
  $("#log").scrollTop = $("#log").scrollHeight;
  if (s.result && s.result.script && JSON.stringify(s.result) !== lastResult) {
    lastResult = JSON.stringify(s.result);
    $("#remix-msg").innerHTML = 'Done. <a target="_blank" href="/vault/' + encodeURI(s.result.script) + '">Open the scripts</a>';
  }
}
$("#ig-go").onclick = async () => {
  $("#ig-msg").textContent = "Connecting...";
  const r = await post("/api/ig/login", {username: $("#ig-u").value, password: $("#ig-p").value});
  $("#ig-p").value = "";
  if (r.need2fa) { $("#ig-2fa").hidden = false; $("#ig-msg").textContent = "Enter the code Instagram sent you."; return; }
  say("#ig-msg", r, "Connected."); refresh();
};
$("#ig-code-go").onclick = async () => { const r = await post("/api/ig/2fa", {code: $("#ig-code").value}); say("#ig-msg", r, "Connected."); refresh(); };
$("#claude-signin").onclick = async () => { const r = await post("/api/claude/signin"); say("#run-msg", r, "A sign-in page opens in your browser. Finish there, then come back."); };
$("#run").onclick = async () => say("#run-msg", await post("/api/run", {limit: $("#limit").value}), "Started. Watch the activity box below.");
$("#retry").onclick = async () => say("#run-msg", await post("/api/retry"), "Retrying failed reels.");
$("#links-go").onclick = async () => { const r = await post("/api/links", {text: $("#links").value}); say("#links-msg", r, `Started on ${r.count} link(s).`); };
$("#sched-on").onclick = async () => { say("#sched-msg", await post("/api/schedule", {time: $("#sched-time").value}), "On."); refresh(); };
$("#sched-off").onclick = async () => { say("#sched-msg", await post("/api/schedule", {time: null}), "Off."); refresh(); };
$("#remix").onclick = async () => say("#remix-msg", await post("/api/remix", {topic: $("#topic").value, category: $("#cat").value, count: $("#count").value}), "Writing... this takes a minute or two.");
$("#profile-save").onclick = async () => say("#profile-msg", await post("/api/profile", {text: $("#profile").value}), "Saved.");
$("#quit").onclick = async () => { await post("/api/quit"); document.body.innerHTML = "<p style='padding:24px'>ReelVault is closed. Double-click the ReelVault icon to open it again.</p>"; };
refresh(); setInterval(refresh, 2000);
</script></body></html>
"""

if __name__ == "__main__":
    main()
