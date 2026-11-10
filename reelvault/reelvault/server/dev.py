"""Local dev server: web + worker on one port with a throwaway data folder. WP1.

    python -m reelvault.server.dev --port 9100                  # temp DATA_DIR, web + in-process worker
    python -m reelvault.server.dev --port 9101 --data-dir /tmp/x --seed --split

--seed    adds an active member (member@example.com) and prints both logins and the community code
--split   runs the worker as a second OS process (like production), instead of a thread
The data folder holds a dev-only master key (dev-master-key.txt) and admin password (dev-admin.txt);
never point --data-dir inside the repo. Stop with Ctrl+C (the worker process is stopped too).
"""
from __future__ import annotations

import argparse
import os
import secrets
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ADMIN_EMAIL = "admin@example.com"
MEMBER_EMAIL = "member@example.com"
FORBIDDEN_PORTS = {8765}


def _read_or_make(path: Path, make) -> str:
    if path.exists():
        return path.read_text().strip()
    val = make()
    path.write_text(val + "\n")
    os.chmod(path, 0o600)
    return val


def configure(port: int, data_dir: str | None, *, log_level: str = "warning") -> dict:
    """Set the env for a dev server and return the logins. Usable from tests/scripts too."""
    if port in FORBIDDEN_PORTS:
        raise SystemExit(f"Port {port} is reserved. Pick another one.")
    d = Path(data_dir or tempfile.mkdtemp(prefix="rv-dev-")).resolve()
    repo = Path(__file__).resolve().parents[2]
    if d.is_relative_to(repo):
        raise SystemExit("Put the dev data folder outside the repo (it holds a dev key).")
    d.mkdir(parents=True, exist_ok=True)
    from .security import genkey

    key = _read_or_make(d / "dev-master-key.txt", lambda: genkey("dev"))
    admin_pw = _read_or_make(d / "dev-admin.txt", lambda: "dev-" + secrets.token_urlsafe(9))
    os.environ.update({"DATA_DIR": str(d), "PUBLIC_URL": f"http://localhost:{port}", "RV_MASTER_KEYS": key,
                       "ADMIN_EMAIL": ADMIN_EMAIL, "ADMIN_PASSWORD": admin_pw,
                       "LOG_LEVEL": os.environ.get("LOG_LEVEL", log_level), "RV_MODELS_DIR": os.environ.get(
                           "RV_MODELS_DIR", str(d / "models"))})
    from . import settings

    settings.reload()
    return {"data_dir": str(d), "admin_email": ADMIN_EMAIL, "admin_password": admin_pw}


def seed_member(password: str = "member-pass-123") -> dict:
    from sqlalchemy import select

    from . import db, security

    with db.tx(immediate=False) as conn:
        got = conn.execute(select(db.users.c.id).where(db.users.c.email == MEMBER_EMAIL)).first()
    if got:
        return {"member_email": MEMBER_EMAIL, "member_password": password, "member_id": got[0]}
    h = security.hash_password(password)
    uid = db.new_id("usr")
    now = db.utcnow()
    with db.tx() as conn:
        conn.execute(db.users.insert().values(id=uid, email=MEMBER_EMAIL, name="Demo Member", password_hash=h,
                                              status="active", role="member", created_at=now,
                                              membership_checked_at=now, settings=dict(db.USER_SETTINGS_DEFAULT)))
        db.upsert(conn, db.profiles, {"user_id": uid, "brand_id": "me", "status": "empty", "answers": {},
                                      "updated_at": now}, do_nothing=True)
    return {"member_email": MEMBER_EMAIL, "member_password": password, "member_id": uid}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ReelVault Members dev server (web + worker).")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--split", action="store_true", help="run the worker as its own process")
    a = ap.parse_args(argv)
    info = configure(a.port, a.data_dir)

    import uvicorn

    from . import api_admin, db
    from .app import create_app, prepare_runtime

    prepare_runtime()
    if a.seed:
        info.update(seed_member())
    with db.tx() as conn:  # a fresh, known community code for this dev run
        info["community_code"] = api_admin.create_community_code(conn)
    print(f"ReelVault Members dev server: http://localhost:{a.port}")
    for k, v in info.items():
        print(f"  {k}: {v}")
    sys.stdout.flush()
    worker = None

    def _exit(*_a):  # uvicorn re-raises SIGTERM after shutdown; turn it into SystemExit so `finally` runs
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _exit)
    if a.split:
        worker = subprocess.Popen([sys.executable, "-m", "reelvault.server.jobs"], env=dict(os.environ,
                                                                                           RV_ROLE="worker"))
    try:
        app = create_app("web" if a.split else "all", prepare=False)
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    finally:
        if worker is not None:
            worker.terminate()
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
