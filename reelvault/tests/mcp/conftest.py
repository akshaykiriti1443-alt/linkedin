"""Fixtures for the MCP and OAuth tests (DESIGN-SAAS.md 12.4, WP6).

`live` starts the real app (role web) under uvicorn on a free port in 9220-9239, with PUBLIC_URL set to that
address, so the official mcp client talks to it over HTTP exactly like claude.ai would (minus TLS).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from reelvault.server.testing import *  # noqa: F401,F403

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "server"))   # WP5's seeding helpers (_wp5_support.py)



class Live:
    def __init__(self, url: str, app, server, thread):
        self.url, self.app, self.server, self.thread = url, app, server, thread

    @property
    def mcp_url(self) -> str:
        return self.url + "/mcp"


@pytest.fixture
def live(rv_env, monkeypatch):
    import uvicorn
    from _mcp_support import free_port

    from reelvault.server import settings
    from reelvault.server.app import create_app

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("PUBLIC_URL", url)
    monkeypatch.setenv("RV_TEST_ALLOW_HTTP_CIMD", "1")
    settings.reload()
    app = create_app("web")
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
                         proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, name="rv-uvicorn", daemon=True)
    th.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not th.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield Live(url, app, server, th)
    finally:
        server.should_exit = True
        th.join(timeout=15)
