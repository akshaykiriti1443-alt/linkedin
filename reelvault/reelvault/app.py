"""ReelVault desktop app: one web app that opens in your browser (screens live in reelvault/ui/).

Start it by double-clicking the ReelVault shortcut (it runs `pythonw -m reelvault.app`).
Everything lives in  %USERPROFILE%\\ReelVault  (or $REELVAULT_HOME).

    python -m reelvault.app            open the app
    python -m reelvault.app --run-once fetch + analyze once, then exit (used by the daily schedule)
"""

from __future__ import annotations

import collections
import io
import itertools
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
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__

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


EXT_DIR = HOME / "chrome-extension"


def prepare_home() -> None:
    import shutil

    HOME.mkdir(parents=True, exist_ok=True)
    os.chdir(HOME)
    # Keep the Chrome extension folder up to date with this version of the app.
    shutil.copytree(Path(__file__).parent / "extension", EXT_DIR, dirs_exist_ok=True)
    if not (HOME / "reelvault.toml").exists():
        from .config import DEFAULT_CATEGORIES

        save_settings({
            "vault_dir": "vault",
            "instagram_username": "",
            "cookies_file": str(HOME / "instagram.cookies.json"),
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
# id: a number unique to each job (the page waits for that id). tag: what the job is about, e.g. "remake:<reel>".
JOB = {"name": None, "started": None, "result": None, "id": None, "tag": ""}
JOB_LOCK = threading.Lock()
# The last jobs that ended: [{"id", "name", "tag", "ok", "result", "message"}], newest last.
FINISHED: collections.deque[dict] = collections.deque(maxlen=20)
_JOB_IDS = itertools.count(int(time.time() * 1000))  # keeps growing across restarts of the app


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


def start_job(name: str, fn, *args, tag: str = "") -> int:
    """Run fn(*args) in the background. Returns the new job's id, or 0 if another job is running."""
    with JOB_LOCK:
        if JOB["name"]:
            return 0
        job_id = next(_JOB_IDS)
        JOB.update(name=name, started=time.time(), result=None, id=job_id, tag=tag)

    def runner():
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _LogWriter()
        log(f"Started: {name}")
        ok, result, message = False, None, ""
        try:
            result = JOB["result"] = fn(*args)
            ok = True
            log(f"Finished: {name}")
        except SystemExit as e:
            message = str(e)
            log(f"Stopped: {e}")
        except Exception as e:  # show any failure in the panel instead of crashing the app
            message = f"Something went wrong: {e}"
            log(f"Error: {e}")
            LOG.extend(traceback.format_exc().splitlines()[-6:])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            with JOB_LOCK:  # record the end before freeing the slot, so a poll never sees neither
                FINISHED.append({"id": job_id, "name": name, "tag": tag, "ok": ok, "result": result,
                                 "message": message})
                JOB["name"] = None

    threading.Thread(target=runner, daemon=True).start()
    return job_id


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
    if _ig_connected():
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
    return {"ok": ok, "bad": bad}


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


def _chrome_state() -> dict:
    from . import chrome_login

    return dict(chrome_login.STATE)


def status() -> dict:
    import shutil

    cfg, lib = cfg_lib()
    cats = sorted({r["analysis"]["category"] for r in lib.all_done()})
    counts = lib.stats()
    lib.db.close()
    with JOB_LOCK:  # one consistent picture of the running job and the ones that ended
        job = dict(JOB)
        finished = list(FINISHED)
    return {
        "home": str(HOME),
        "version": __version__,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "claude": claude_status(),
        "instagram": {"username": cfg.instagram_username, "connected": _ig_connected()},
        "counts": counts,
        "categories": cats,
        "job": job["name"],
        "job_id": job["id"] if job["name"] else None,
        "job_tag": job["tag"] if job["name"] else "",
        "result": job["result"],
        "finished": finished,
        "log": list(LOG)[-200:],
        "schedule": schedule_status(),
        "profile": (HOME / "profile.md").read_text(encoding="utf-8") if (HOME / "profile.md").exists() else "",
        "library": (cfg.vault_dir / "index.html").exists(),
        "chromeLogin": _chrome_state(),
        "extension": {"seen": _ext_seen(), "folder": str(EXT_DIR)},
        "igWaiting": time.time() < IG_WAIT["until"],
        "last_update": UPDATE_STATE["last"],
    }


# --------------------------------------------------------------------------
# Instagram login (password + 2FA; only the session is saved)
# --------------------------------------------------------------------------

_pending_ig = {"L": None, "user": None, "info": {}}
IG_SMS_URLS = (
    "https://www.instagram.com/api/v1/web/accounts/send_two_factor_login_sms/",
    "https://www.instagram.com/accounts/send_two_factor_login_sms/",
)


COOKIES = "instagram.cookies.json"


def _ig_connected() -> bool:
    from . import igweb

    return bool(igweb.load_cookies(HOME / COOKIES).get("sessionid"))


def save_ig_cookies(cookies: dict) -> str:
    """Check a login with Instagram, then keep it. Returns the username."""
    from . import igweb

    cookies = {k: v for k, v in cookies.items() if v}
    if not cookies.get("sessionid"):
        raise RuntimeError("That login has no Instagram session. Log in to Instagram first.")
    log("Checking the Instagram login...")
    s = igweb.session(cookies)
    try:
        user = igweb.whoami(s, cookies)
    except igweb.IGError as e:
        if "logged you out" in str(e) or "refused" in str(e):
            raise RuntimeError(str(e))
        user = ""  # Instagram changed something small; the login itself may still work
        log(f"Couldn't read your username ({e}); saving the login anyway.")
    igweb.save_cookies(HOME / COOKIES, cookies)
    save_settings({"instagram_username": user, "cookies_file": str(HOME / COOKIES)})
    log(f"Instagram connected{' as @' + user if user else ''}.")
    return user or "you"


def _ig_save(L, user: str) -> None:
    """Password login (instaloader) -> keep its cookies like any other login."""
    cookies = L.context._session.cookies.get_dict()
    save_ig_cookies(cookies)


def _capture_login_reply():
    """Instaloader drops Instagram's 2FA details (SMS on? authenticator app? which phone?).
    Watch the login reply so the panel can show how the code is sent."""
    import requests

    seen = {}
    real_post = requests.Session.post

    def post(self, url, *a, **kw):
        resp = real_post(self, url, *a, **kw)
        if "login/ajax" in url:
            try:
                seen.update(resp.json().get("two_factor_info") or {})
            except ValueError:
                pass
        return resp

    return seen, real_post, post


def _two_factor_hint(info: dict) -> dict:
    phone = info.get("obfuscated_phone_number") or ""
    return {
        "sms": bool(info.get("sms_two_factor_on")),
        "app": bool(info.get("totp_two_factor_on")),
        "whatsapp": bool(info.get("whatsapp_two_factor_on")),
        "phone": phone,
    }


def ig_login(user: str, password: str) -> dict:
    import instaloader
    import requests
    from instaloader.exceptions import (BadCredentialsException, ConnectionException,
                                        TwoFactorAuthRequiredException)

    user = user.strip().lstrip("@")
    L = instaloader.Instaloader(quiet=True)
    seen, real_post, spy = _capture_login_reply()
    requests.Session.post = spy
    try:
        L.login(user, password)
    except TwoFactorAuthRequiredException:
        _pending_ig.update(L=L, user=user, info=dict(seen))
        return {"need2fa": True, **_two_factor_hint(seen)}
    except BadCredentialsException:
        return {"error": "Wrong username or password."}
    except ConnectionException as e:
        if "checkpoint" in str(e).lower() or "challenge" in str(e).lower():
            return {"error": "Instagram wants to confirm it's you. Open the Instagram app, tap 'This was me', "
                             "then try again. Or use 'Connect with my browser' below."}
        return {"error": f"Instagram refused the login: {e}"}
    finally:
        requests.Session.post = real_post
    _ig_save(L, user)
    return {"ok": True}


def ig_resend_sms() -> dict:
    """Ask Instagram to text a fresh login code."""
    L, user = _pending_ig["L"], _pending_ig["user"]
    pending = L.context.two_factor_auth_pending if L else None
    if not pending:
        return {"error": "Start the login again: type your password and click Connect."}
    session, _, identifier = pending
    last = ""
    for url in IG_SMS_URLS:
        try:
            r = session.post(url, data={"username": user, "identifier": identifier})
        except Exception as e:  # network trouble
            last = str(e)
            continue
        if r.status_code == 404:
            last = "not found"
            continue
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.ok and body.get("status", "ok") == "ok":
            info = body.get("two_factor_info") or {}
            # Instagram may issue a new login id with the new code; keep it in step.
            if info.get("two_factor_identifier"):
                L.context.two_factor_auth_pending = (session, user, info["two_factor_identifier"])
            phone = info.get("obfuscated_phone_number") or _pending_ig["info"].get("obfuscated_phone_number") or ""
            return {"ok": True, "phone": phone}
        last = body.get("message") or f"HTTP {r.status_code}"
    if "wait" in str(last).lower() or "few minutes" in str(last).lower():
        return {"error": "Instagram says to wait a few minutes before asking for another code."}
    return {"error": f"Instagram did not send a new code ({last}). Use 'Connect with my browser' instead."}


def ig_2fa(code: str) -> dict:
    from instaloader.exceptions import BadCredentialsException

    L, user = _pending_ig["L"], _pending_ig["user"]
    if not L:
        return {"error": "Start the login again."}
    code = re.sub(r"\s+", "", code)
    try:
        L.two_factor_login(code)
    except BadCredentialsException:
        return {"error": "That code didn't work. Use the newest code, or click 'Text me a new code'."}
    _ig_save(L, user)
    _pending_ig.update(L=None, user=None, info={})
    return {"ok": True}


def _use_browser_cookies(cookies: dict) -> str:
    return save_ig_cookies(cookies)


IG_WAIT = {"until": 0.0}
WAIT_SECONDS = 15 * 60


def _ext_seen() -> bool:
    import tomllib

    p = HOME / "reelvault.toml"
    return bool(p.exists() and tomllib.loads(p.read_text(encoding="utf-8")).get("extension_seen"))


def open_instagram_tab() -> str:
    """Open instagram.com in a NEW TAB of your normal Chrome (Edge if no Chrome)."""
    from .chrome_login import find_chrome

    exe = find_chrome()
    url = "https://www.instagram.com/"
    if exe:
        subprocess.Popen([exe, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "Edge" if "msedge" in exe.lower() else "Chrome"
    webbrowser.open(url, new=2)
    return "your browser"


def ig_connect() -> dict:
    if not _ext_seen():
        return {"needExtension": True}
    IG_WAIT["until"] = time.time() + WAIT_SECONDS
    name = open_instagram_tab()
    log("Waiting for your Instagram login from the browser...")
    return {"ok": True, "message": f"A new Instagram tab opened in {name}. If you're already logged in, "
                                   "this connects in a few seconds. If not, log in there as usual."}


def ext_hello(body: dict) -> dict:
    if not _ext_seen():
        save_settings({"extension_seen": True})
        log("Chrome extension found.")
    return {"waiting": time.time() < IG_WAIT["until"]}


def ig_cookies_from_extension(body: dict) -> dict:
    if time.time() >= IG_WAIT["until"]:
        return {"error": "ReelVault isn't waiting for a login. Click Connect Instagram first."}
    cookies = body.get("cookies") or {}
    if not isinstance(cookies, dict):
        return {"error": "bad request"}
    try:
        user = save_ig_cookies({str(k): str(v) for k, v in cookies.items()})
    except Exception as e:
        log(f"Instagram login failed: {e}")
        return {"error": str(e)}
    IG_WAIT["until"] = 0.0
    return {"ok": True, "username": user}


def show_extension_folder() -> dict:
    if not (EXT_DIR / "manifest.json").exists():
        prepare_home()
    if IS_WIN:
        os.startfile(str(EXT_DIR))  # opens it in File Explorer
        return {"ok": True}
    return {"error": f"Open this folder yourself: {EXT_DIR}"}


def open_extensions_page() -> dict:
    from .chrome_login import find_chrome

    exe = find_chrome()
    if not exe:
        return {"error": "Couldn't find Chrome or Edge."}
    page = "edge://extensions/" if "msedge" in exe.lower() else "chrome://extensions/"
    subprocess.Popen([exe, page], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "page": page}


def ig_chrome_login() -> dict:
    from . import chrome_login

    return chrome_login.start(HOME / "chrome-profile", _use_browser_cookies)


def ig_from_browser(browser: str) -> dict:
    """Reuse the Instagram login already in your web browser: no password, no code."""
    import instaloader

    try:
        from instaloader.__main__ import get_cookies_from_instagram
    except ImportError:
        return {"error": "This option needs an update. Run the installer again."}
    if browser not in ("chrome", "edge", "firefox", "brave", "opera"):
        return {"error": "Pick a browser."}
    try:
        cookies = get_cookies_from_instagram("instagram", browser, None)
    except Exception as e:
        hint = (" Close every Chrome/Edge window and try again, or log in to instagram.com in Firefox and "
                "pick Firefox." if browser in ("chrome", "edge", "brave", "opera") else "")
        return {"error": f"Couldn't read the Instagram login from {browser}: {e}.{hint}"}
    if not cookies or not cookies.get("sessionid"):
        return {"error": f"You're not logged in to instagram.com in {browser.title()}. Log in there first."}
    try:
        user = save_ig_cookies(cookies)
    except RuntimeError as e:
        return {"error": f"The {browser.title()} login didn't work: {e}"}
    return {"ok": True, "username": user}


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
# Reels, purposes, banks and to-dos (the JSON the app's screens use)
# --------------------------------------------------------------------------

class ApiError(Exception):
    """A plain message for the user, sent as {"error": ...} with an HTTP code."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message, self.code = message, code


BUSY = "ReelVault is busy with another task. Wait for it to finish."
TOPIC_MAX = 4000  # "Write scripts": the topic plus the hook and format picked in Studio
NO_REEL = "That reel is not in your library."
GUESS_CHUNK = 200  # reels per Claude call when guessing purposes (one call for most libraries)


class _OpenLib:
    """with _OpenLib() as (cfg, lib): ... closes the database afterwards."""

    def __enter__(self):
        self.cfg, self.lib = cfg_lib()
        return self.cfg, self.lib

    def __exit__(self, *exc):
        self.lib.db.close()


def vault_url(rec: dict, name: str | None) -> str | None:
    """URL for a file in a reel's folder, served by /vault/<path>. Folder names have spaces, so quote them."""
    if not name:
        return None
    folder = str(rec.get("folder") or "").replace("\\", "/").strip("/")
    return "/vault/" + quote(f"{folder}/{name}" if folder else name)


def reel_media(rec: dict, root: Path) -> dict:
    folder = root / str(rec.get("folder") or "").replace("\\", "/")

    def have(name):
        return bool(name) and (folder / name).is_file()

    frames = [f["file"] for f in rec.get("frames") or [] if isinstance(f, dict) and f.get("file")]
    video = rec.get("video", "video.mp4")  # the first version wrote no "video" key; a manual record sets None
    thumb = rec.get("cover") if have(rec.get("cover")) else None
    if not thumb and frames:
        pick = frames[min(1, len(frames) - 1)]
        thumb = pick if have(pick) else None
    return {
        "video": vault_url(rec, video) if have(video) else None,
        "thumb": vault_url(rec, thumb),
        "frames": [vault_url(rec, f) for f in frames if have(f)],
    }


def reel_summary(rec: dict, root: Path) -> dict:
    a, meta = rec["analysis"], rec.get("meta") or {}
    sk = a.get("skeleton") or {}
    hook = sk.get("hook") or {}
    return {
        "id": rec["shortcode"],
        "title": a.get("title", ""),
        "owner": meta.get("owner") or "",
        "url": meta.get("url") or "",
        "category": a.get("category", ""),
        "subcategory": a.get("subcategory", ""),
        "tags": a.get("tags") or [],
        "duration": rec.get("duration"),
        "thumb": reel_media(rec, root)["thumb"],
        "posted_at": meta.get("posted_at"),
        "analyzed_at": rec.get("analyzed_at"),
        "purposes": rec["purposes"],
        "purpose_guess": rec["purpose_guess"],
        "purpose_confirmed": rec["purpose_confirmed"],
        "summary": a.get("summary", ""),
        "hook": {k: hook.get(k, "") for k in ("spoken", "on_screen_text", "hook_type")},
        "structure_name": sk.get("structure_name", ""),
        "cta_type": (sk.get("cta") or {}).get("cta_type", ""),
        "content_format": a.get("content_format", ""),
        "has_remake": bool(rec["extra"].get("remakes")),
    }


def api_reels() -> dict:
    with _OpenLib() as (cfg, lib):
        rows = lib.rows_done()
        reels = [reel_summary(r, cfg.vault_dir) for r in rows]
    return {
        "reels": reels,
        "categories": sorted({r["category"] for r in reels if r["category"]}),
        "creators": sorted({r["owner"] for r in reels if r["owner"]}, key=str.lower),
    }


def api_reel(code: str) -> dict:
    with _OpenLib() as (cfg, lib):
        rec = lib.row(code)
        if not rec:
            raise ApiError(NO_REEL, 404)
        rec["media"] = reel_media(rec, cfg.vault_dir)
    return rec


def api_set_purposes(code: str, body: dict) -> dict:
    from .purposes import clean

    wanted = body.get("purposes")
    if not isinstance(wanted, list):
        raise ApiError("Send the purposes as a list.")
    purposes = clean(wanted)
    if not purposes:
        raise ApiError("Pick at least one purpose.")
    with _OpenLib() as (_, lib):
        if not lib.row(code):
            raise ApiError(NO_REEL, 404)
        lib.set_purposes(code, purposes, confirmed=True)
    return {"ok": True, "purposes": purposes, "purpose_confirmed": True}


def api_todo(code: str, body: dict) -> dict:
    index = body.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise ApiError("Say which step to tick.")
    done = bool(body.get("done", True))
    with _OpenLib() as (_, lib):
        rec = lib.row(code)
        if not rec:
            raise ApiError(NO_REEL, 404)
        if not 0 <= index < len(rec["analysis"].get("action_plan") or []):
            raise ApiError("That step does not exist.")

        def change(extra):
            ticks = {i for i in extra.get("todo_done") or [] if isinstance(i, int)}
            (ticks.add if done else ticks.discard)(index)
            extra["todo_done"] = sorted(ticks)

        extra = lib.update_extra(code, change)
    return {"ok": True, "todo_done": extra["todo_done"]}


def _first_line(text: str) -> str:
    line = next((x.strip() for x in (text or "").splitlines() if x.strip()), "")
    return re.split(r"(?<=[.!?])\s+", line, maxsplit=1)[0] if line else ""


def _wants_all(query: dict) -> bool:
    return (query.get("all") or [""])[0] in ("1", "true", "yes")


def api_hooks(query: dict) -> dict:
    out = []
    with _OpenLib() as (_, lib):
        for rec in lib.rows_done():
            if not _wants_all(query) and "hook" not in rec["purposes"]:
                continue
            a = rec["analysis"]
            h = a["skeleton"]["hook"]
            out.append({
                "reel_id": rec["shortcode"], "title": a["title"], "owner": (rec.get("meta") or {}).get("owner") or "",
                "spoken": h.get("spoken", ""), "on_screen_text": h.get("on_screen_text", ""),
                "visual": h.get("visual", ""), "hook_type": h.get("hook_type", ""),
                "why_it_works": h.get("why_it_works", ""),
                "formula": _first_line(a.get("fill_in_template", "")) or h.get("spoken") or h.get("on_screen_text", ""),
                "variations": a.get("hook_variations") or [],
            })
    return {"hooks": out}


def api_formats(query: dict) -> dict:
    out = []
    with _OpenLib() as (_, lib):
        for rec in lib.rows_done():
            if not _wants_all(query) and "format" not in rec["purposes"]:
                continue
            a = rec["analysis"]
            sk = a["skeleton"]
            out.append({
                "reel_id": rec["shortcode"], "title": a["title"], "owner": (rec.get("meta") or {}).get("owner") or "",
                "structure_name": sk.get("structure_name", ""),
                "beats": [b.get("purpose", "") for b in sk.get("meat") or []],
                "pacing": sk.get("pacing", ""), "template": a.get("fill_in_template", ""),
                "script_ideas": a.get("script_ideas") or [],
            })
    return {"formats": out}


def api_todos(query: dict) -> list:
    out = []
    with _OpenLib() as (_, lib):
        for rec in lib.rows_done():
            if "learn" not in rec["purposes"]:
                continue
            ticks = set(rec["extra"].get("todo_done") or [])
            for i, s in enumerate(rec["analysis"].get("action_plan") or []):
                done = i in ticks
                if done and not _wants_all(query):
                    continue
                out.append({"reel_id": rec["shortcode"], "title": rec["analysis"]["title"], "index": i,
                            "step": s.get("step", ""), "why": s.get("why", ""), "effort": s.get("effort", ""),
                            "done": done})
    return out


def job_remake(code: str, made_for: str, notes: str):
    from .analyze import make_remake
    from .library import _now

    cfg, lib = cfg_lib()
    try:
        rec = lib.row(code)
        if not rec:
            raise SystemExit(NO_REEL)
        pack = make_remake(cfg, rec, made_for, notes)
        item = dict(pack.model_dump(), created_at=_now())
        extra = lib.update_extra(code, lambda e: e.setdefault("remakes", []).append(item))
        lib.set_purposes(code, lib.row(code)["purposes"] + ["remake"], confirmed=None)
        print(f"Your better version of \"{rec['analysis']['title']}\" is ready.")
        return {"reel": code, "remake": len(extra["remakes"]) - 1}
    finally:
        lib.db.close()


def api_remake(code: str, body: dict) -> dict:
    made_for = str(body.get("for") or "me").strip()[:120] or "me"
    notes = str(body.get("notes") or "").strip()[:4000]
    with _OpenLib() as (_, lib):
        rec = lib.row(code)
    if not rec:
        raise ApiError(NO_REEL, 404)
    title = rec["analysis"]["title"]
    name = f"Make it better: {title}"
    job_id = start_job(name, job_remake, code, made_for, notes, tag=f"remake:{code}")
    if not job_id:
        raise ApiError(BUSY, 409)
    return {"ok": True, "job": name, "job_id": job_id}


def job_guess():
    from .analyze import guess_purposes

    cfg, lib = cfg_lib()
    try:
        recs = lib.unguessed()
        if not recs:
            print("Every reel already has a guess.")
            return {"guessed": 0}
        n = 0
        for i in range(0, len(recs), GUESS_CHUNK):
            for code, guess in guess_purposes(cfg, recs[i:i + GUESS_CHUNK]).items():
                n += lib.set_guess(code, guess)
        print(f"Guessed purposes for {n} of {len(recs)} reel(s). Check them under Sort new reels.")
        return {"guessed": n}
    finally:
        lib.db.close()


def api_guess() -> dict:
    with _OpenLib() as (_, lib):
        n = len(lib.unguessed())
    if not n:
        return {"ok": True, "count": 0}
    job_id = start_job("Guess purposes for older reels", job_guess, tag="guess")
    if not job_id:
        raise ApiError(BUSY, 409)
    return {"ok": True, "count": n, "job_id": job_id}


# --------------------------------------------------------------------------
# Self-update
# --------------------------------------------------------------------------

UPDATE_STATE: dict = {"last": None}   # result of the last update run, read once at start


def _update_branch() -> str:
    from .config import load_config

    return load_config(HOME / "reelvault.toml").update_branch


def api_update_info(query: dict) -> dict:
    from . import update

    info = update.check(__version__, _update_branch(), force=_wants_all({"all": query.get("refresh")}))
    last = UPDATE_STATE["last"]
    if last and not last.get("ok") and not info["error"]:
        info["error"] = last.get("message") or "The last update did not work. Your current version still works."
    info["last_update"] = last
    return info


def api_update_apply(body: dict) -> dict:
    from . import update

    with JOB_LOCK:
        if JOB["name"]:
            raise ApiError(f"ReelVault is busy with \"{JOB['name']}\". Wait for it to finish, then update.", 409)
        JOB.update(name="Update ReelVault", started=time.time(), result=None, id=next(_JOB_IDS), tag="update")
    try:
        info = update.check(__version__, _update_branch(), force=True)
        if info["error"]:
            raise ApiError(info["error"], 502)
        if not info["available"] and not body.get("force"):
            raise ApiError("You already have the newest version.", 400)
        log(f"Downloading ReelVault {info['latest']}...")
        try:
            src = update.download(HOME, _update_branch())
            update.start_helper(src, PORT, HOME)
        except update.UpdateError as e:
            log(f"Update stopped: {e}" + (f" ({e.detail})" if e.detail else ""))
            raise ApiError(str(e), 502)
        log("Installing the update. ReelVault will close and open again in a minute.")
    except Exception:
        with JOB_LOCK:
            JOB["name"] = None
        raise
    threading.Thread(target=lambda: (time.sleep(1.0), os._exit(0)), daemon=True).start()
    return {"ok": True, "latest": info["latest"]}


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        if self._from_extension():
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    EXT_PATHS = ("/api/ext/hello", "/api/ig/cookies")

    def _from_extension(self) -> bool:
        origin = self.headers.get("Origin") or ""
        return origin.startswith(("chrome-extension://", "extension://")) and urlparse(self.path).path in self.EXT_PATHS

    def _local_only(self) -> bool:
        # Block other websites from driving the app from your browser.
        # Only the ReelVault extension may call its two endpoints.
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        ok = host in ("127.0.0.1", "localhost") and (
            not origin or urlparse(origin).hostname in ("127.0.0.1", "localhost") or self._from_extension())
        if not ok:
            self._json({"error": "forbidden"}, 403)
        return ok

    def do_OPTIONS(self):
        if not self._from_extension():
            return self._json({"error": "forbidden"}, 403)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):
        try:
            self._get()
        except ApiError as e:
            self._json({"error": e.message}, e.code)
        except ConnectionError:  # the browser hung up (e.g. it cancelled a video request): nothing to tell it
            pass
        except (Exception, SystemExit) as e:  # keep the app alive and tell the panel what went wrong
            LOG.append(f"Error: {e}")
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            self._post()
        except ApiError as e:
            self._json({"error": e.message}, e.code)
        except ConnectionError:
            pass
        except (Exception, SystemExit) as e:
            LOG.append(f"Error: {e}")
            self._json({"error": str(e)}, 500)

    REEL_PATH = re.compile(r"^/api/reels/([A-Za-z0-9_-]+)(?:/(purposes|todo|remake))?/?$")

    def _get(self):
        if not self._local_only():
            return
        url = urlparse(self.path)
        path = unquote(url.path)
        query = parse_qs(url.query)
        m = self.REEL_PATH.match(path)
        if path in ("/", "/index.html"):
            self._ui("index.html")
        elif path.startswith("/ui/"):
            self._ui(path[len("/ui/"):])
        elif path == "/api/status":
            self._json(status())
        elif path == "/api/reels":
            self._json(api_reels())
        elif m and not m.group(2):
            self._json(api_reel(m.group(1)))
        elif path == "/api/banks/hooks":
            self._json(api_hooks(query))
        elif path == "/api/banks/formats":
            self._json(api_formats(query))
        elif path == "/api/todos":
            self._json(api_todos(query))
        elif path == "/api/update":
            self._json(api_update_info(query))
        elif path.startswith("/vault/"):
            self._file(path[len("/vault/"):])
        else:
            self._json({"error": "not found"}, 404)

    def _ui(self, rel: str):
        """The app's screens: static files in reelvault/ui/."""
        root = UI_DIR.resolve()
        p = (root / rel).resolve()
        if root not in p.parents or not p.is_file():
            if rel == "index.html":
                return self._html(MISSING_UI)
            return self._json({"error": "not found"}, 404)
        ctype = UI_TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self._send_file(p, ctype, cache=False)

    def _html(self, page: str):
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        self._send_file(p, ctype)

    def _send_file(self, p: Path, ctype: str, cache: bool = True):
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
        if not cache:
            self.send_header("Cache-Control", "no-cache")
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
                except ConnectionError:  # Broken pipe, reset, or aborted (WinError 10053): the browser hung up
                    return
                left -= len(chunk)

    def _post(self):
        if not self._local_only():
            return
        path = urlparse(self.path).path
        try:
            b = self._body()
        except ValueError:
            raise ApiError("The request was not valid JSON.")
        if not isinstance(b, dict):
            raise ApiError("The request was not valid JSON.")
        busy = {"error": BUSY}
        m = self.REEL_PATH.match(path)
        if m and m.group(2) == "purposes":
            self._json(api_set_purposes(m.group(1), b))
        elif m and m.group(2) == "todo":
            self._json(api_todo(m.group(1), b))
        elif m and m.group(2) == "remake":
            self._json(api_remake(m.group(1), b))
        elif path == "/api/sort/guess":
            self._json(api_guess())
        elif path == "/api/update":
            self._json(api_update_apply(b))
        elif path == "/api/run":
            limit = int(b.get("limit") or 20)
            job_id = start_job("Get new saved reels", job_fetch_and_analyze, limit, tag="run")
            self._json({"ok": True, "job_id": job_id} if job_id else busy)
        elif path == "/api/links":
            from .fetch import SHORTCODE_RE

            urls = [m.group(0) for m in SHORTCODE_RE.finditer(b.get("text", ""))]
            if not urls:
                return self._json({"error": "No Instagram reel links found. Paste links like instagram.com/reel/..."})
            job_id = start_job("Analyze pasted links", job_links, urls, tag="links")
            self._json({"ok": True, "count": len(urls), "job_id": job_id} if job_id else busy)
        elif path == "/api/retry":
            job_id = start_job("Retry failed reels", job_retry, tag="retry")
            self._json({"ok": True, "job_id": job_id} if job_id else busy)
        elif path == "/api/remix":
            topic = (b.get("topic") or "").strip()
            if not topic:
                return self._json({"error": "Type what the new video is about."})
            if len(topic) > TOPIC_MAX:
                return self._json({"error": f"That is too long. Keep it under {TOPIC_MAX:,} characters."})
            job_id = start_job("Write scripts", job_remix, topic, b.get("category"), int(b.get("count") or 3),
                               tag="remix")
            self._json({"ok": True, "job_id": job_id} if job_id else busy)
        elif path == "/api/ig/login":
            self._json(ig_login(b.get("username", ""), b.get("password", "")))
        elif path == "/api/ig/2fa":
            self._json(ig_2fa(b.get("code", "")))
        elif path == "/api/ig/connect":
            self._json(ig_connect())
        elif path == "/api/ext/hello":
            self._json(ext_hello(b))
        elif path == "/api/ig/cookies":
            self._json(ig_cookies_from_extension(b))
        elif path == "/api/ext/folder":
            self._json(show_extension_folder())
        elif path == "/api/ext/open":
            self._json(open_extensions_page())
        elif path == "/api/ig/chrome":
            self._json(ig_chrome_login())
        elif path == "/api/ig/resend":
            self._json(ig_resend_sms())
        elif path == "/api/ig/browser":
            self._json(ig_from_browser(b.get("browser", "")))
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


def replace_old_copy() -> bool:
    """If an older ReelVault is still running in the background, close it so this one starts.
    Returns True when the port is free for us."""
    import urllib.request

    base = f"http://127.0.0.1:{PORT}"
    try:
        running = json.load(urllib.request.urlopen(base + "/api/status", timeout=10)).get("version")
    except Exception:
        running = None  # very old copy (no version) or not ReelVault
    if running == __version__:
        return False
    try:
        req = urllib.request.Request(base + "/api/quit", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass
    for _ in range(40):
        if not port_in_use():
            return True
        time.sleep(0.25)
    return False


def port_in_use() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--run-once" in argv:
        return run_once()
    url = f"http://127.0.0.1:{PORT}/"
    if port_in_use():
        if not replace_old_copy():  # this same version is already running: just show it
            webbrowser.open(url)
            return
    prepare_home()
    from .update import take_last_result

    UPDATE_STATE["last"] = take_last_result(HOME)
    if sys.stdout is None:  # pythonw has no console
        sys.stdout = sys.stderr = open(HOME / "app.log", "a", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if "--no-browser" not in argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"ReelVault is running at {url}")
    server.serve_forever()


UI_DIR = Path(__file__).parent / "ui"
UI_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".ico": "image/x-icon", ".woff2": "font/woff2",
}

MISSING_UI = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>ReelVault</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font:16px/1.5 system-ui,sans-serif;max-width:560px;margin:60px auto;padding:0 16px">
<h1>ReelVault</h1><p>The app's screens are missing from this install.</p>
<p>Run the ReelVault installer again to fix it. Your library is safe.</p></body></html>
"""


if __name__ == "__main__":
    main()
