"""Runs the extension's Node tests from pytest (DESIGN-SAAS.md 12.3). WP2.

- tests/ext/unit.test.mjs : the pure modules (bigint, validate, pacing, trim, endpoints), about 1 s.
- tests/ext/ext_e2e.mjs   : Chromium + the unpacked extension + the fake Instagram + the real app, about 75 s.

Needs node 20+ and Playwright for Node with its Chromium (`npm i -g playwright && npx playwright install chromium`).
Skipped when they are missing, or when RV_SKIP_EXT_E2E=1 (the unit file still runs when node exists).
Ports: RV_E2E_PORT_BASE (default 9140; the test uses base+1 and base+3).
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


def _node_env() -> dict:
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


def _run(args: list[str], env: dict, timeout: int) -> None:
    p = subprocess.run([NODE, "--test", "--test-reporter=spec", *args], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=timeout)
    if p.returncode != 0:
        pytest.fail(f"node --test {' '.join(args)} failed:\n{p.stdout[-12000:]}\n{p.stderr[-4000:]}")


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_extension_unit_modules():
    _run([str(HERE / "unit.test.mjs")], _node_env(), 120)


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_extension_end_to_end(tmp_path):
    if os.environ.get("RV_SKIP_EXT_E2E") == "1":
        pytest.skip("RV_SKIP_EXT_E2E=1")
    env = _node_env()
    if not _has_playwright(env):
        pytest.skip("Playwright for Node with Chromium is not installed")
    env["RV_E2E_TMP"] = str(tmp_path)
    _run([str(HERE / "ext_e2e.mjs")], env, 600)
