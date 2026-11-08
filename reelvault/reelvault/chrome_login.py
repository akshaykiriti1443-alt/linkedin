"""Log in to Instagram in a normal Chrome window.

ReelVault opens Chrome (its own separate profile, kept in the ReelVault folder)
on Instagram's real login page. You log in exactly as usual: password, SMS
code, "Resend code", "Try another way", WhatsApp, "This was me". As soon as
Instagram signs you in, ReelVault reads the login cookie from that window
through Chrome's local debugging port, saves it, and closes the window.

Your password never passes through ReelVault.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

LOGIN_URL = "https://www.instagram.com/accounts/login/"
TIMEOUT_S = 15 * 60

STATE = {"status": "idle", "message": ""}   # idle | waiting | done | error
LAST = {"port": None, "proc": None}          # the open login window (used by tests)
_lock = threading.Lock()


def find_chrome() -> str | None:
    """Chrome first, then Edge (always present on Windows), then Chromium."""
    env = os.environ.get("REELVAULT_CHROME")
    if env and Path(env).exists():
        return env
    if sys.platform == "win32":
        try:
            import winreg

            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    key = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
                    path = winreg.QueryValue(key, None)
                    if path and Path(path).exists():
                        return path
                except OSError:
                    pass
        except ImportError:
            pass
        bases = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
            for base in bases:
                p = Path(base) / rel
                if base and p.exists():
                    return str(p)
    if sys.platform == "darwin":
        p = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if p.exists():
            return str(p)
    for name in ("google-chrome", "chrome", "chromium", "chromium-browser", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cdp(port: int, method: str, params: dict | None = None) -> dict:
    """Send one DevTools command to the browser and return its result."""
    import websocket  # websocket-client

    info = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5))
    ws = websocket.create_connection(info["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "DevTools error"))
                return msg.get("result", {})
    finally:
        ws.close()


def instagram_cookies(port: int) -> dict:
    cookies = _cdp(port, "Storage.getCookies").get("cookies", [])
    return {c["name"]: c["value"] for c in cookies if "instagram.com" in c.get("domain", "")}


def _set(status: str, message: str) -> None:
    STATE.update(status=status, message=message)


def start(profile_dir: Path, on_login, extra_args: list[str] | None = None) -> dict:
    """Open the login window and watch it in the background.

    on_login(cookies) must return the Instagram username (or raise with a readable message).
    """
    with _lock:
        if STATE["status"] == "waiting":
            return {"ok": True, "message": STATE["message"]}
        exe = find_chrome()
        if not exe:
            _set("error", "Couldn't find Google Chrome or Microsoft Edge on this computer.")
            return {"error": STATE["message"]}
        port = _free_port()
        profile_dir.mkdir(parents=True, exist_ok=True)
        args = [exe, f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}",
                "--no-first-run", "--no-default-browser-check", "--new-window", *(extra_args or []), LOGIN_URL]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            _set("error", f"Couldn't open Chrome: {e}")
            return {"error": STATE["message"]}
        LAST.update(port=port, proc=proc)
        name = "Edge" if "msedge" in exe.lower() else "Chrome"
        _set("waiting", f"A {name} window opened on Instagram's login page. Log in there as usual. "
                        "It closes by itself when you're in.")
        threading.Thread(target=_watch, args=(proc, port, on_login), daemon=True).start()
        return {"ok": True, "message": STATE["message"]}


def _watch(proc: subprocess.Popen, port: int, on_login) -> None:
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        time.sleep(2)
        try:
            cookies = instagram_cookies(port)
        except Exception:
            if proc.poll() is not None:
                _set("error", "The login window was closed before you finished logging in. Click the button to try again.")
                return
            continue  # Chrome still starting up
        if cookies.get("sessionid") and cookies.get("ds_user_id"):
            try:
                user = on_login(cookies)
            except Exception as e:
                _set("error", f"Logged in, but ReelVault couldn't use the login: {e}")
                return
            _set("done", f"Connected as @{user}.")
            try:
                _cdp(port, "Browser.close")
            except Exception:
                proc.terminate()
            return
    _set("error", "Login timed out after 15 minutes. Click the button to try again.")
    try:
        proc.terminate()
    except Exception:
        pass
