"""The instagram.com web connection, against fake Instagram answers (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from reelvault import igweb
from reelvault.config import Config
from reelvault.library import Library

COOKIES = {"sessionid": "s", "csrftoken": "c", "ds_user_id": "42", "mid": "m"}


def _resp(status, body=None, raw=b"", headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode() if body is not None else raw
    r.headers.update(headers or {})
    r._content_consumed = True  # lets iter_content() stream from the fake body
    return r


def reel(code, video=True, owner="creator"):
    m = {"code": code, "pk": str(igweb.shortcode_to_id(code)), "taken_at": 1762300000,
         "caption": {"text": f"caption {code}"}, "user": {"username": owner}, "like_count": 5,
         "play_count": 900, "video_duration": 12.5}
    if video:
        m["video_versions"] = [{"url": f"https://cdn.example/{code}-small.mp4", "width": 360, "height": 640},
                               {"url": f"https://cdn.example/{code}.mp4", "width": 720, "height": 1280}]
    return m


class FakeIG:
    def __init__(self, pages, logged_in=True):
        self.pages, self.logged_in, self.calls = pages, logged_in, []

    def get(self, sess, url, params=None, **kw):
        self.calls.append((url, params, dict(sess.headers)))
        if url.startswith("https://cdn.example/"):
            return _resp(200, raw=b"\x00\x00\x00\x18ftypmp42video")
        if not self.logged_in:
            return _resp(302, headers={"Location": "https://www.instagram.com/accounts/login/"})
        if url.endswith("/api/v1/users/42/info/"):
            return _resp(200, {"user": {"username": "scaleitads"}, "status": "ok"})
        if "/api/v1/feed/saved/posts/" in url:
            i = int((params or {}).get("max_id") or 0)
            items, more = self.pages[i], i + 1 < len(self.pages)
            return _resp(200, {"items": [{"media": m} for m in items], "more_available": more,
                               "next_max_id": str(i + 1) if more else None, "status": "ok"})
        if "/api/v1/media/" in url:
            mid = int(url.split("/api/v1/media/")[1].split("/")[0])
            return _resp(200, {"items": [reel(igweb.id_to_shortcode(mid))], "status": "ok"})
        return _resp(404, {})


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    def install(fake):
        monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: fake.get(self, url, **kw))
        monkeypatch.setattr(igweb.time, "sleep", lambda s: None)
        monkeypatch.setattr("reelvault.fetch.time.sleep", lambda s: None)
        igweb.save_cookies(tmp_path / "c.json", COOKIES)
        cfg = Config(vault_dir=tmp_path / "vault", cookies_file=str(tmp_path / "c.json"))
        return cfg, Library(cfg.vault_dir)
    return install


def test_shortcode_round_trip():
    for code in ("DfXyDVPopFX", "B", "Ca_-9z"):
        assert igweb.id_to_shortcode(igweb.shortcode_to_id(code)) == code


def test_whoami_sends_web_headers(setup):
    fake = FakeIG([[]])
    setup(fake)
    s = igweb.session(COOKIES)
    assert igweb.whoami(s, COOKIES) == "scaleitads"
    headers = fake.calls[-1][2]
    assert headers["X-IG-App-ID"] == igweb.WEB_APP_ID and headers["X-CSRFToken"] == "c"


def test_saved_feed_pages_skip_photos_and_known(setup):
    from reelvault import fetch

    fake = FakeIG([[reel("AAA1"), reel("PHOTO", video=False)], [reel("BBB2"), reel("CCC3")]])
    cfg, lib = setup(fake)
    lib.add_pending("CCC3", cfg.inbox / "CCC3")  # already in the library
    assert fetch.from_saved(cfg, lib) == 2
    for code in ("AAA1", "BBB2"):
        folder = cfg.inbox / code
        assert (folder / "video.mp4").read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
        meta = json.loads((folder / "meta.json").read_text())
        assert meta["owner"] == "creator" and meta["caption"] == f"caption {code}" and meta["views"] == 900
    big = [c for c in fake.calls if c[0].startswith("https://cdn.example/AAA1")]
    assert big[0][0].endswith("AAA1.mp4")  # picked the largest video
    assert not (cfg.inbox / "PHOTO").exists()


def test_limit(setup):
    from reelvault import fetch

    cfg, lib = setup(FakeIG([[reel("AAA1"), reel("BBB2"), reel("CCC3")]]))
    assert fetch.from_saved(cfg, lib, limit=1) == 1


def test_links(setup):
    from reelvault import fetch

    cfg, lib = setup(FakeIG([[]]))
    n = fetch.from_links(cfg, lib, ["https://www.instagram.com/reel/DfXyDVPopFX/?igsh=abc"])
    assert n == 1 and (cfg.inbox / "DfXyDVPopFX" / "video.mp4").exists()


def test_logged_out_is_clear(setup):
    from reelvault import fetch

    cfg, lib = setup(FakeIG([[]], logged_in=False))
    with pytest.raises(SystemExit, match="Connect Instagram again"):
        fetch.from_saved(cfg, lib)


def test_not_connected(tmp_path):
    from reelvault import fetch

    cfg = Config(vault_dir=tmp_path / "v", cookies_file=str(tmp_path / "none.json"))
    with pytest.raises(SystemExit, match="Connect Instagram first"):
        fetch.from_saved(cfg, Library(cfg.vault_dir))
