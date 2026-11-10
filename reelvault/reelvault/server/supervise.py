"""`reelvault-start`: the image's default command (DESIGN-SAAS.md 2.2, 12.5). WP1.

RV_ROLE=supervise (default): migrate once, then run TWO OS processes (web = uvicorn, worker = job runner) and restart
either one if it dies. RV_ROLE=web | worker | all runs just that role in this process.
PORT (default 8000) and HOST (default 0.0.0.0) pick the listen address.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def _uvicorn_argv(role: str) -> list[str]:
    from .settings import get_settings

    s = get_settings()
    proxies = ",".join(str(n) for n in s.trusted_proxies) or "127.0.0.1"
    return [sys.executable, "-m", "uvicorn", "reelvault.server.app:create_app", "--factory",
            "--host", os.environ.get("HOST", "0.0.0.0"), "--port", os.environ.get("PORT", "8000"),
            "--proxy-headers", f"--forwarded-allow-ips={proxies}", "--no-server-header",
            "--log-level", "warning"]


def supervise() -> int:
    from .app import prepare_runtime, setup_logging

    setup_logging()
    prepare_runtime()  # migrate + bootstrap once, before both processes start
    specs = {"web": _uvicorn_argv("web"), "worker": [sys.executable, "-m", "reelvault.server.jobs"]}
    procs: dict[str, subprocess.Popen] = {}
    stopping = False
    backoff = {k: 1.0 for k in specs}

    started: dict[str, float] = {}

    def start(name: str) -> None:
        env = dict(os.environ, RV_ROLE=name)
        procs[name] = subprocess.Popen(specs[name], env=env)
        started[name] = time.monotonic()

    def stop(*_a) -> None:
        nonlocal stopping
        stopping = True
        for p in procs.values():
            if p.poll() is None:
                p.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for name in specs:
        start(name)
    while not stopping:
        time.sleep(1.0)
        for name, p in list(procs.items()):
            if p.poll() is not None and not stopping:
                print(f'{{"level":"error","msg":"{name} exited with {p.returncode}; restarting"}}', flush=True)
                if time.monotonic() - started.get(name, 0) > 60:
                    backoff[name] = 1.0  # it ran fine for a while: restart quickly
                time.sleep(backoff[name])
                backoff[name] = min(30.0, backoff[name] * 2)
                start(name)
    deadline = time.time() + 20
    for p in procs.values():
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
    return 0


def health() -> int:
    """`reelvault-start health`: exit 0 when this container is healthy (Docker HEALTHCHECK).
    web / supervise / all: GET /healthz on PORT answers 200 with ok=true (and, for supervise/all, the worker's
    heartbeat is fresh once it has started). worker: its heartbeat in the database is under 3 minutes old."""
    import json
    import urllib.request

    role = os.environ.get("RV_ROLE", "supervise").strip() or "supervise"
    if role == "worker":
        from datetime import datetime, timedelta

        from . import db

        beat = db.get_setting("internal.worker_heartbeat") or {}
        at = beat.get("at")
        if not at:
            return 1
        age = db.utcnow() - datetime.fromisoformat(at.replace("Z", "+00:00"))
        return 0 if age < timedelta(minutes=3) else 1
    url = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            body = json.loads(r.read() or b"{}")
    except Exception:  # noqa: BLE001
        return 1
    if not body.get("ok"):
        return 1
    worker = body.get("worker")
    if role in ("supervise", "all") and worker is not None and not worker.get("ok"):
        return 1
    return 0


def main() -> None:
    if sys.argv[1:2] == ["health"]:
        sys.exit(health())
    role = os.environ.get("RV_ROLE", "supervise").strip() or "supervise"
    if role == "supervise":
        sys.exit(supervise())
    if role == "worker":
        from .jobs import main as worker_main

        worker_main()
        return
    os.environ["RV_ROLE"] = role
    os.execv(sys.executable, _uvicorn_argv(role))


if __name__ == "__main__":
    main()
