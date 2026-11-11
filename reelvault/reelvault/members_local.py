"""ReelVault Members on your own computer (localhost), for the owner to try before hosting.

    python -m reelvault.members_local --setup --source <repo>/reelvault   # once: models + Chrome extension
    python -m reelvault.members_local                                      # start: http://localhost:8800

Everything lives in %USERPROFILE%\\ReelVault-Members (or $RVM_HOME):
    data\\            the database and files
    models\\          sound, face and speech models (downloaded once, checked by SHA-256)
    chrome-extension\\ the Chrome extension built for http://localhost:8800 (Load unpacked)
    login.txt        your owner login
    secret-key.txt   the key that encrypts saved API keys (keep it; never share it)

The web app and the job worker run in this one process. Your AI work runs through Claude Code on this
computer (your Claude plan): the launcher pairs a desktop worker for the owner account automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import shutil
import socket
import sys
import tarfile
import tempfile
import threading
import time
import webbrowser
import zipfile
from pathlib import Path

HOME = Path(os.environ.get("RVM_HOME", Path.home() / "ReelVault-Members")).resolve()
PORT = int(os.environ.get("RVM_PORT", "8800"))
URL = f"http://localhost:{PORT}"
OWNER_EMAIL = "owner@reelvault.local"

CED_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/audio-tagging-models/"
           "sherpa-onnx-ced-tiny-audio-tagging-2024-04-19.tar.bz2")
CED_DIR_IN_TAR = "sherpa-onnx-ced-tiny-audio-tagging-2024-04-19"
CED_MODEL_SHA = "73aa22e783115f6a4ae169b36089907e07d0fd44795595eddea9c1bfc74cc945"
CED_LABELS_SHA = "cdd1049833c4b86127c2773ac0d14a2754b6a6d0d1798002ed5c66e699708429"
YUNET_URLS = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
)
YUNET_SHA = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
WHISPER_MODELS = {"small": "536b0662742c02347bc0e980a01041f333bce120",
                  "base": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"}


def say(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------------------------- environment

def _read_or_make(path: Path, make) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    val = make()
    path.write_text(val + "\n", encoding="utf-8")
    return val


def _add_tools_to_path() -> None:
    """ffmpeg from this install or from the ReelVault desktop app, and Claude Code's folder."""
    for extra in (HOME / "tools" / "ffmpeg" / "bin", Path.home() / "ReelVault" / "tools" / "ffmpeg" / "bin",
                  Path.home() / ".local" / "bin"):
        if extra.exists() and str(extra) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = str(extra) + os.pathsep + os.environ.get("PATH", "")


def configure() -> dict:
    """Point the server at HOME and return the owner login. Safe to call more than once."""
    _add_tools_to_path()
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "data").mkdir(exist_ok=True)
    from .server.security import genkey

    key = _read_or_make(HOME / "secret-key.txt", lambda: genkey("local"))
    login = HOME / "login.txt"
    if login.exists():
        pw = next((ln.split(":", 1)[1].strip() for ln in login.read_text(encoding="utf-8").splitlines()
                   if ln.lower().startswith("password:")), "")
    else:
        pw = "rv-" + secrets.token_urlsafe(10)
        login.write_text(f"ReelVault Members (on this computer)\nOpen: {URL}\nEmail: {OWNER_EMAIL}\n"
                         f"Password: {pw}\n", encoding="utf-8")
    os.environ.update({
        "DATA_DIR": str(HOME / "data"), "PUBLIC_URL": URL, "RV_MASTER_KEYS": key,
        "ADMIN_EMAIL": OWNER_EMAIL, "ADMIN_PASSWORD": pw, "RV_MODELS_DIR": str(HOME / "models"),
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "warning"),
        "HF_HUB_VERBOSITY": "error",
    })
    from .server import settings

    settings.reload()
    return {"url": URL, "email": OWNER_EMAIL, "password": pw}


# --------------------------------------------------------------------------------------------- models

def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str) -> bytes:
    import requests

    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


def fetch_models(with_whisper: bool = True) -> None:
    """Same models and checksums as deploy/fetch_models.sh, in Python so it works on Windows."""
    models = HOME / "models"
    ced = models / "ced-tiny"
    if (ced / "model.int8.onnx").exists() and _sha(ced / "model.int8.onnx") == CED_MODEL_SHA \
            and (ced / "class_labels_indices.csv").exists() \
            and _sha(ced / "class_labels_indices.csv") == CED_LABELS_SHA:
        say("Sound model: already there")
    else:
        say("Sound model: downloading (about 6 MB)...")
        blob = _download(CED_URL)
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:bz2") as tf:
                for name in ("model.int8.onnx", "class_labels_indices.csv"):
                    member = tf.getmember(f"{CED_DIR_IN_TAR}/{name}")
                    with tf.extractfile(member) as src, open(Path(tmp) / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            if _sha(Path(tmp) / "model.int8.onnx") != CED_MODEL_SHA or \
                    _sha(Path(tmp) / "class_labels_indices.csv") != CED_LABELS_SHA:
                raise SystemExit("The sound model download looks damaged. Run setup again.")
            ced.mkdir(parents=True, exist_ok=True)
            for name in ("model.int8.onnx", "class_labels_indices.csv"):
                shutil.move(str(Path(tmp) / name), str(ced / name))
    yn = models / "yunet" / "face_detection_yunet_2023mar.onnx"
    if yn.exists() and _sha(yn) == YUNET_SHA:
        say("Face model: already there")
    else:
        yn.parent.mkdir(parents=True, exist_ok=True)
        for url in YUNET_URLS:
            say("Face model: downloading...")
            try:
                blob = _download(url)
            except Exception as e:  # try the next mirror
                say(f"  that source failed ({e})")
                continue
            if hashlib.sha256(blob).hexdigest() == YUNET_SHA:
                yn.write_bytes(blob)
                break
        else:
            raise SystemExit("Could not download the face model. Check your internet and run setup again.")
    if with_whisper:
        os.environ["HF_HOME"] = str(models / "whisper")
        from faster_whisper import download_model

        for name, rev in WHISPER_MODELS.items():
            say(f"Speech model '{name}': checking (the first time downloads a few hundred MB)...")
            download_model(name, revision=rev)
    say("Models ready.")


# --------------------------------------------------------------------------------------------- extension

def build_extension(source: Path) -> Path:
    """Build the members extension for http://localhost:PORT and unpack it for 'Load unpacked'."""
    deploy = source / "deploy"
    sys.path.insert(0, str(deploy))
    try:
        import build_extension as be  # deploy/build_extension.py from the downloaded source
    finally:
        sys.path.pop(0)
    blob = be.build_zip([URL], build="zip", src=source / "extension-members")
    out = HOME / "chrome-extension"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(out)
    # a single top folder inside the zip -> use its contents
    inner = [p for p in out.iterdir()]
    if len(inner) == 1 and inner[0].is_dir() and (inner[0] / "manifest.json").exists():
        tmp = HOME / "chrome-extension.tmp"
        shutil.move(str(inner[0]), str(tmp))
        shutil.rmtree(out)
        shutil.move(str(tmp), str(out))
    if not (out / "manifest.json").exists():
        raise SystemExit("The Chrome extension build is missing its manifest. Run setup again.")
    say(f"Chrome extension ready: {out}")
    return out


# --------------------------------------------------------------------------------------------- run

def port_in_use() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def _wait_until_up(timeout: float = 90) -> bool:
    import requests

    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.get(f"{URL}/healthz", timeout=3).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def link_desktop_worker() -> str:
    """Pair this computer's Claude Code with the owner account (once) and start the worker loop."""
    from sqlalchemy import select

    from . import members_worker
    from .server import api_ext, db

    with db.read() as c:
        row = c.execute(select(db.users.c.id, db.users.c.ai_mode).where(db.users.c.email == OWNER_EMAIL)).first()
    if row is None:
        return "owner account not found"
    uid, mode = row
    cfg = members_worker.load_config()
    if cfg.get("server") != URL or not cfg.get("token"):
        code, _ = api_ext.new_pairing_code(uid, "desktop")
        members_worker.pair(URL, code, name="This computer (Claude Code)")
    if mode is None:
        with db.tx() as c:
            c.execute(db.users.update().where(db.users.c.id == uid).values(ai_mode="desktop"))
    w = members_worker.Worker()
    w.start()
    _WORKER["w"] = w
    return "linked"


_WORKER: dict = {}


def run(open_browser: bool = True) -> int:
    if port_in_use():
        say(f"ReelVault Members is already running at {URL}")
        if open_browser:
            webbrowser.open(URL)
        return 0
    info = configure()
    if sys.stdout is None:  # pythonw: no console
        sys.stdout = sys.stderr = open(HOME / "members.log", "a", encoding="utf-8")
    import uvicorn

    from .server.app import create_app, prepare_runtime

    prepare_runtime()
    app = create_app("all", prepare=False)

    def after_start():
        if not _wait_until_up():
            say("The server did not start in time. See members.log.")
            return
        try:
            say(f"Claude Code worker: {link_desktop_worker()}")
        except Exception as e:  # keep the server up even if Claude Code isn't set up yet
            say(f"Claude Code worker not linked yet: {e}")
        say(f"ReelVault Members is running: {URL}  (login: {info['email']} / see {HOME / 'login.txt'})")
        if open_browser:
            webbrowser.open(URL)

    threading.Thread(target=after_start, daemon=True).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    finally:
        w = _WORKER.get("w")
        if w is not None:
            w.stop()
    return 0


def setup(source: Path | None, with_whisper: bool = True) -> int:
    info = configure()
    fetch_models(with_whisper=with_whisper)
    if source is not None:
        build_extension(source)
    from .server.app import prepare_runtime

    prepare_runtime()  # create the database and the owner account now
    say(json.dumps({"url": info["url"], "email": info["email"], "login_file": str(HOME / "login.txt"),
                    "extension": str(HOME / "chrome-extension")}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ReelVault Members on this computer.")
    ap.add_argument("--setup", action="store_true", help="download models and build the Chrome extension")
    ap.add_argument("--source", help="the reelvault source folder (has deploy/ and extension-members/)")
    ap.add_argument("--no-whisper", action="store_true", help="skip the speech models during setup")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    if a.setup:
        return setup(Path(a.source).resolve() if a.source else None, with_whisper=not a.no_whisper)
    return run(open_browser=not a.no_browser)


if __name__ == "__main__":
    sys.exit(main())
