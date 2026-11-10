"""Runs the web UI tests (tests/web/ui.test.mjs) from pytest. WP7.

Chromium + Playwright for Node + the real app with seeded data (tests/web/serve.py), about 3-4 minutes:
every member and admin screen at 1280 px and 390 px, light and dark (no console errors, no sideways scroll),
signup -> approve -> the 5 onboarding steps with a fake extension and a fake AI, and the build-on buttons.
Needs node 20+ and Playwright for Node with Chromium. Skipped when they are missing or RV_SKIP_WEB_UI=1.
Ports: RV_WEB_PORT_BASE (default 9240; uses base+1, base+2 and base+11).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NODE = shutil.which("node")


def _env() -> dict:
    env = dict(os.environ)
    if NODE and "NODE_PATH" not in env:
        try:
            env["NODE_PATH"] = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True,
                                              timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    env["RV_PYTHON"] = sys.executable
    return env


def _has_playwright(env: dict) -> bool:
    if not NODE:
        return False
    probe = ("const {createRequire} = require('module'); let pw; try { pw = require('playwright'); } catch { "
             "pw = createRequire(require('path').join(process.env.NODE_PATH || '', 'x.js'))('playwright'); } "
             "process.exit(require('fs').existsSync(pw.chromium.executablePath()) ? 0 : 1);")
    try:
        return subprocess.run([NODE, "-e", probe], env=env, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(os.environ.get("RV_SKIP_WEB_UI") == "1", reason="RV_SKIP_WEB_UI=1")
def test_web_ui():
    env = _env()
    if not _has_playwright(env):
        pytest.skip("node or Playwright for Node (with Chromium) is not installed")
    p = subprocess.run([NODE, "--test", "--test-reporter=spec", str(HERE / "ui.test.mjs")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        pytest.fail(f"web UI tests failed:\n{p.stdout[-15000:]}\n{p.stderr[-4000:]}")
