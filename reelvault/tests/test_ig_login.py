"""Instagram login flow in the app, against a fake Instagram (no network).

Covers: 2FA prompt shows how the code is sent, 'Text me a new code' (with the
first endpoint missing and a new login id issued), and finishing with the new code.
"""

from __future__ import annotations

import json

import requests


def _resp(status: int, body: dict, cookies: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r.cookies = requests.utils.cookiejar_from_dict(cookies or {})
    return r


def test_two_factor_with_resend(tmp_path, monkeypatch):
    monkeypatch.setenv("REELVAULT_HOME", str(tmp_path))
    import importlib

    import reelvault.app as app
    app = importlib.reload(app)  # pick up REELVAULT_HOME
    app.prepare_home()

    calls = []

    def fake_get(self, url, *a, **kw):
        if url.endswith("/api/v1/users/42/info/"):
            return _resp(200, {"user": {"username": "someone"}, "status": "ok"})
        self.cookies.set("csrftoken", "tok")
        return _resp(200, {})

    def fake_post(self, url, *a, **kw):
        data = kw.get("data") or {}
        calls.append((url, dict(data)))
        if "login/ajax/two_factor" in url:
            ok = data.get("verificationCode") == "654321" and data.get("identifier") == "id-2"
            if ok:  # Instagram sets the login cookies on success
                self.cookies.set("sessionid", "S", domain=".instagram.com")
                self.cookies.set("ds_user_id", "42", domain=".instagram.com")
            return _resp(200, {"status": "ok"} if ok else {"status": "fail", "message": "bad code"},
                         {"csrftoken": "tok2"})
        if "login/ajax" in url:
            return _resp(200, {"two_factor_required": True, "two_factor_info": {
                "two_factor_identifier": "id-1", "sms_two_factor_on": True, "totp_two_factor_on": False,
                "obfuscated_phone_number": "+91 ••••• •••12"}})
        if "/api/v1/web/accounts/send_two_factor_login_sms/" in url:
            return _resp(404, {})
        if "send_two_factor_login_sms" in url:
            assert data["identifier"] == "id-1"
            return _resp(200, {"status": "ok", "two_factor_info": {"two_factor_identifier": "id-2"}})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    monkeypatch.setattr(requests.Session, "post", fake_post)

    r = app.ig_login("@someone", "pw")
    assert r["need2fa"] and r["sms"] and not r["app"] and r["phone"].endswith("12")

    assert "error" in app.ig_2fa("111111")          # old/wrong code is rejected
    r = app.ig_resend_sms()
    assert r["ok"] and r["phone"].endswith("12")     # falls back to the second endpoint
    assert app.ig_2fa("654 321") == {"ok": True}     # spaces ignored, new login id used
    saved = json.loads((tmp_path / "instagram.cookies.json").read_text())
    assert saved["sessionid"] == "S" and saved["ds_user_id"] == "42"
    assert 'instagram_username = "someone"' in (tmp_path / "reelvault.toml").read_text()
    assert requests.Session.post is fake_post        # the login spy was removed


def test_resend_without_login(tmp_path, monkeypatch):
    monkeypatch.setenv("REELVAULT_HOME", str(tmp_path))
    import importlib

    import reelvault.app as app
    app = importlib.reload(app)
    assert "error" in app.ig_resend_sms()
