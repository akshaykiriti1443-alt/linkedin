"""Chrome login: open a real (headless) browser, simulate Instagram finishing the
login by setting its cookies, and check ReelVault picks them up and closes the window."""

from __future__ import annotations

import glob
import os
import time

import pytest

from reelvault import chrome_login


def _browser():
    found = chrome_login.find_chrome()
    if found:
        return found
    hits = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
    return hits[0] if hits else None


ARGS = ["--headless=new", "--no-sandbox", "--disable-gpu"]


def _wait(cond, secs=30):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return False


@pytest.fixture()
def browser(monkeypatch):
    exe = _browser()
    if not exe:
        pytest.skip("no Chrome/Chromium on this machine")
    monkeypatch.setenv("REELVAULT_CHROME", exe)
    monkeypatch.setattr(chrome_login, "LOGIN_URL", "about:blank")  # no network needed
    chrome_login.STATE.update(status="idle", message="")
    yield exe
    proc = chrome_login.LAST.get("proc")
    if proc and proc.poll() is None:
        proc.kill()


def test_login_is_detected_and_window_closes(browser, tmp_path):
    got = {}

    def on_login(cookies):
        got.update(cookies)
        return "tester"

    r = chrome_login.start(tmp_path / "profile", on_login, ARGS)
    assert r.get("ok"), r
    assert chrome_login.STATE["status"] == "waiting"
    port = chrome_login.LAST["port"]
    assert _wait(lambda: _alive(port))
    chrome_login._cdp(port, "Storage.setCookies", {"cookies": [
        {"name": "sessionid", "value": "abc123", "domain": ".instagram.com", "path": "/"},
        {"name": "ds_user_id", "value": "42", "domain": ".instagram.com", "path": "/"},
        {"name": "csrftoken", "value": "tok", "domain": ".instagram.com", "path": "/"},
        {"name": "other", "value": "x", "domain": ".example.com", "path": "/"},
    ]})
    assert _wait(lambda: chrome_login.STATE["status"] == "done"), chrome_login.STATE
    assert chrome_login.STATE["message"] == "Connected as @tester."
    assert got["sessionid"] == "abc123" and "other" not in got
    assert _wait(lambda: chrome_login.LAST["proc"].poll() is not None, 15)  # window closed itself


def test_closing_the_window_is_reported(browser, tmp_path):
    chrome_login.start(tmp_path / "profile", lambda c: "x", ARGS)
    port = chrome_login.LAST["port"]
    assert _wait(lambda: _alive(port))
    chrome_login.LAST["proc"].kill()
    assert _wait(lambda: chrome_login.STATE["status"] == "error", 20)
    assert "closed" in chrome_login.STATE["message"]


def _alive(port):
    try:
        chrome_login._cdp(port, "Browser.getVersion")
        return True
    except Exception:
        return False
