"""Self-update from GitHub.

The app checks the newest version number on GitHub. "Update now" downloads the code,
starts this module as a small helper, and quits. The helper waits for the app to close,
installs the new code with pip, and starts the app again. If pip fails, the old version
stays installed and the error goes to HOME/update.log (the app shows it on the next start).

    python -m reelvault.update --apply <src_dir> --port <port>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import traceback
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

REPO = "akshaykiriti1443-alt/linkedin"
DEFAULT_BRANCH = "claude/kind-dijkstra-0o03in"
LOG_NAME = "update.log"
RESULT_NAME = "update-result.json"     # last helper run: {"ok", "message", "at", "version"}
IS_WIN = sys.platform == "win32"


class UpdateError(RuntimeError):
    """message: a plain sentence for the user. detail: the technical reason (goes to update.log)."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


KEEP = "Your current version still works."


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

def parse_version(v: str) -> tuple[int, ...]:
    """'0.4.10' -> (0, 4, 10). Non-number parts count as 0, so odd tags never crash."""
    parts = []
    for piece in str(v or "").strip().lstrip("vV").split("."):
        m = re.match(r"\d+", piece)
        parts.append(int(m.group(0)) if m else 0)
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(latest: str | None, current: str) -> bool:
    return bool(latest) and parse_version(latest) > parse_version(current)


def version_url(branch: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{branch}/reelvault/reelvault/__init__.py"


def zip_url(branch: str) -> str:
    return f"https://github.com/{REPO}/archive/refs/heads/{branch}.zip"


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ReelVault-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_version(branch: str = DEFAULT_BRANCH) -> str:
    try:
        text = _get(version_url(branch), timeout=10).decode("utf-8", "replace")
    except Exception as e:
        raise UpdateError(f"Could not reach GitHub to check for updates. Check your internet. ({e})")
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", text)
    if not m:
        raise UpdateError("Could not read the newest version number from GitHub.")
    return m.group(1)


_cache: dict = {"t": 0.0, "key": None, "v": None}
CACHE_SECONDS = 3600


def check(current: str, branch: str = DEFAULT_BRANCH, force: bool = False) -> dict:
    """{"current", "latest", "available", "error"}; cached for an hour."""
    key = (current, branch)
    if not force and _cache["v"] and _cache["key"] == key and time.time() - _cache["t"] < CACHE_SECONDS:
        return dict(_cache["v"])
    try:
        latest = latest_version(branch)
        v = {"current": current, "latest": latest, "available": is_newer(latest, current), "error": None}
    except UpdateError as e:
        v = {"current": current, "latest": None, "available": False, "error": str(e)}
    _cache.update(t=time.time(), key=key, v=v)
    return dict(v)


# --------------------------------------------------------------------------
# Download (runs inside the app)
# --------------------------------------------------------------------------

def find_project(root: Path) -> Path:
    """The folder with pyproject.toml whose package is reelvault/ (the zip nests it under linkedin-<branch>/reelvault)."""
    for py in sorted(root.rglob("pyproject.toml"), key=lambda p: len(p.parts)):
        if (py.parent / "reelvault" / "__init__.py").exists():
            return py.parent
    raise UpdateError("The download does not contain ReelVault. Try again later.")


def download(home: Path, branch: str = DEFAULT_BRANCH) -> Path:
    """Download and unzip the newest code into HOME/source (replacing what was there). Returns the project folder."""
    src = home / "source"
    tmp = home / "source-download.zip"
    try:
        data = _get(zip_url(branch), timeout=120)
    except Exception as e:
        raise UpdateError(f"Could not download the update. Check your internet and try again. ({e})")
    try:
        tmp.write_bytes(data)
        if src.exists():
            _rmtree(src)
        src.mkdir(parents=True)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(src)
    except zipfile.BadZipFile:
        raise UpdateError("The update download was broken. Try again.")
    except OSError as e:  # a file is in use (antivirus, Explorer), read-only, or the disk is full
        raise UpdateError(f"Could not save the update on this computer. Close other windows and try again. {KEEP}",
                          f"{type(e).__name__}: {e}")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return find_project(src)


def _rmtree(path: Path) -> None:
    """shutil.rmtree that also removes read-only files (Windows refuses to delete those)."""
    def retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _detach_kwargs() -> dict:
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "close_fds": True}
    if IS_WIN:
        kw["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                               | subprocess.CREATE_NO_WINDOW)
    else:
        kw["start_new_session"] = True
    return kw


def start_helper(src_dir: Path, port: int, home: Path) -> subprocess.Popen:
    """Start `python -m reelvault.update --apply ...` detached, with the app's own Python."""
    cmd = [sys.executable, "-m", "reelvault.update", "--apply", str(src_dir), "--port", str(port)]
    env = dict(os.environ, REELVAULT_HOME=str(home), REELVAULT_PORT=str(port))
    return subprocess.Popen(cmd, cwd=str(home), env=env, **_detach_kwargs())


# --------------------------------------------------------------------------
# The helper (runs after the app quits)
# --------------------------------------------------------------------------

def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def wait_for_port(port: int, seconds: float = 60) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if port_free(port):
            return True
        time.sleep(0.5)
    return port_free(port)


def app_python() -> str:
    """pythonw on Windows (no console window), python elsewhere."""
    exe = Path(sys.executable)
    if IS_WIN:
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    elif exe.name.startswith("pythonw"):
        return str(exe.with_name(exe.name.replace("pythonw", "python")))
    return str(exe)


def relaunch(home: Path, port: int) -> None:
    env = dict(os.environ, REELVAULT_HOME=str(home), REELVAULT_PORT=str(port))
    kw = _detach_kwargs()
    # The app only opens app.log itself when it has no stdout (pythonw from the icon). Give it the log here.
    try:
        out = open(home / "app.log", "a", encoding="utf-8")
    except OSError:
        out = None
    if out is not None:
        kw["stdout"] = kw["stderr"] = out
    try:
        subprocess.Popen([app_python(), "-m", "reelvault.app"], cwd=str(home), env=env, **kw)
    finally:
        if out is not None:
            out.close()


def _pip(args: list[str], log) -> None:
    cmd = [sys.executable, "-m", "pip", *args]
    log("$ " + " ".join(cmd))
    flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
    # pip writes in the Windows code page unless told otherwise; we read UTF-8 (for names like José).
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=1800, creationflags=flags, env=env)
    for line in ((r.stdout or "") + (r.stderr or "")).splitlines()[-40:]:
        log("  " + line)
    if r.returncode != 0:
        last = [x for x in (r.stderr or r.stdout or "").splitlines() if x.strip()]
        raise UpdateError(f"The update could not install. {KEEP} Details are in update.log.",
                          f"pip: {last[-1] if last else 'unknown error'}")


def _source_version(src_dir: Path) -> str | None:
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""",
                  (src_dir / "reelvault" / "__init__.py").read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


def apply(src_dir: Path, port: int, home: Path) -> bool:
    """Install src_dir over the current version, then start the app again (always)."""
    home.mkdir(parents=True, exist_ok=True)
    logf = open(home / LOG_NAME, "a", encoding="utf-8")

    def log(msg: str) -> None:
        logf.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        logf.flush()

    ok, message, detail, version = False, "", "", None
    try:
        log(f"--- Updating from {src_dir}")
        if not wait_for_port(port, 60):
            raise UpdateError(f"ReelVault did not close in time, so the update did not run. {KEEP} Try again.")
        version = _source_version(src_dir)
        _pip(["install", "--upgrade", str(src_dir)], log)
        _pip(["install", "--force-reinstall", "--no-deps", str(src_dir)], log)
        ok, message = True, f"Updated to version {version}." if version else "Updated."
        log(message)
    except Exception as e:
        if isinstance(e, UpdateError):
            message, detail = str(e), e.detail
        else:
            message, detail = f"The update failed. {KEEP} Details are in update.log.", f"{type(e).__name__}: {e}"
        log("FAILED: " + message + (f" ({detail})" if detail else ""))
        for line in traceback.format_exc().splitlines()[-6:]:
            log("  " + line)
    try:
        (home / RESULT_NAME).write_text(json.dumps({"ok": ok, "message": message, "detail": detail, "version": version,
                                                    "at": datetime.now().isoformat(timespec="seconds")}),
                                        encoding="utf-8")
    except OSError:
        pass
    try:
        relaunch(home, port)
        log("Started ReelVault again.")
    except Exception as e:
        log(f"Could not start ReelVault again: {e}")
    logf.close()
    return ok


def take_last_result(home: Path) -> dict | None:
    """The result of the last update run, read once (the app calls this when it starts)."""
    p = home / RESULT_NAME
    if not p.exists():
        return None
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        v = None
    try:
        p.replace(home / (RESULT_NAME + ".seen"))
    except OSError:
        pass
    return v if isinstance(v, dict) else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reelvault.update")
    ap.add_argument("--apply", required=True, help="folder with the new ReelVault code (has pyproject.toml)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("REELVAULT_PORT", "8765")))
    ap.add_argument("--home", default=os.environ.get("REELVAULT_HOME", str(Path.home() / "ReelVault")))
    a = ap.parse_args(argv)
    return 0 if apply(Path(a.apply), a.port, Path(a.home)) else 1


if __name__ == "__main__":
    sys.exit(main())
