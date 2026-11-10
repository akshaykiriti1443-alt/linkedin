"""Fake Instagram for the integration journey: 3 competitors x 250 reels, real synthetic videos on the CDN.

    python journey_ig.py --port 9303 --cert-dir DIR

Built on WP2's tests/ext/fake_ig_server.py (same HTTPS handler, same answer shapes from make_fixtures.py), with:
- three competitor accounts (dental.growth.co, smile.marketing, clinic.ads.lab), each a 250-reel timeline with
  planted outliers (make_fixtures.account_timeline), plus the WP2 accounts for discovery;
- signed CDN links that expire 7 days from now (not from the fixture date);
- each reel's video = one of WP3's synthetic clips (tests/fixtures/media/*.mp4), chosen by the reel index so the
  planted outliers get distinct formats (talking head, green screen, screen recording...).
"""
from __future__ import annotations

import argparse
import importlib.util
import ssl
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("rv_fake_ig", REPO / "tests" / "ext" / "fake_ig_server.py")
fis = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fis)
mk = fis.mk

NOW = int(time.time())
mk.FIXTURE_NOW = NOW            # media() signs CDN links for FIXTURE_NOW + 7 days
MEDIA = REPO / "tests" / "fixtures" / "media"

COMPETITORS = [
    {"pk": 70000001, "username": "dental.growth.co", "full_name": "Dental Growth Co"},
    {"pk": 70000002, "username": "smile.marketing", "full_name": "Smile Marketing"},
    {"pk": 70000003, "username": "clinic.ads.lab", "full_name": "Clinic Ads Lab"},
]
# Outliers (planted indexes in account_timeline) get the "winning" formats; normal reels get a mix.
OUTLIER_IDX = {5, 17, 31, 44, 66, 90, 121, 150, 188, 230}
WIN = ["greenscreen", "screen", "sfx_cuts"]
NORMAL = ["talking_head", "slideshow", "split", "speech", "noaudio", "talking_head", "slideshow"]

TIMELINES: dict[int, list[dict]] = {}
VIDEO_OF: dict[int, str] = {}
for n, c in enumerate(COMPETITORS):
    tl = mk.account_timeline(250, seed=21 + n, owner=c, now=NOW - n * 977)  # distinct pks per account
    TIMELINES[c["pk"]] = tl
    for i, m in enumerate(tl):
        VIDEO_OF[m["pk"]] = WIN[(i + n) % 3] if i in OUTLIER_IDX else NORMAL[(i * 3 + n) % len(NORMAL)]

fis.USERS.update({c["username"]: c for c in COMPETITORS})
for tl in TIMELINES.values():
    fis.BY_PK.update({m["pk"]: m for m in tl})
VIDEO_BYTES = {p.stem: p.read_bytes() for p in MEDIA.glob("*.mp4")}
COVER = None


def _cover() -> bytes:
    global COVER
    if COVER is None:
        import cv2
        import numpy as np

        img = np.full((640, 360, 3), (60, 90, 160), np.uint8)
        cv2.putText(img, "REEL", (90, 330), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (250, 250, 250), 4)
        COVER = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
    return COVER


class H(fis.H):
    def _clips(self, e):
        f = e["form"]
        try:
            target = int(f.get("target_user_id") or 0)
        except ValueError:
            target = 0
        if target not in TIMELINES:
            return super()._clips(e)
        if e["headers"].get("x-csrftoken") != e["cookies"].get("csrftoken"):
            return self._send(403, {"message": "CSRF token missing or incorrect", "status": "fail"})
        tl = TIMELINES[target]
        size = int(f.get("page_size") or 12)
        offset = (mk.offset_of(f["max_id"]) or 0) if f.get("max_id") else 0
        items = tl[offset:offset + size]
        more = offset + size < len(tl)
        return self._send(200, mk.clips_page(items, mk.cursor_for(offset + size) if more else None, more))

    def _feed(self, p, q):
        owner = int(p.split("/")[5])
        if owner not in TIMELINES:
            return super()._feed(p, q)
        tl = TIMELINES[owner]
        start = 0
        if q.get("max_id"):
            mpk = int(q["max_id"].split("_")[0])
            idx = next((i for i, m in enumerate(tl) if m["pk"] == mpk), None)
            if idx is None:
                idx = next((i - 1 for i, m in enumerate(tl) if m["pk"] < mpk), len(tl) - 1)
            start = idx + 1
        return self._send(200, mk.feed_page(tl[start:start + 12], owner, start + 12 < len(tl)))

    def _cdn(self, u):
        name = u.path.rsplit("/", 1)[-1]
        if name.endswith(".mp4") and "_audio" not in name:
            pk = int(name.split("_")[0])
            clip = VIDEO_OF.get(pk)
            if clip and self.headers.get("x-journey-server") and pk % 5 == 0:
                return self._send(403, b"", "text/plain")   # the CDN refuses the server: extension fallback
            if clip:
                return self._send(200, VIDEO_BYTES[clip], "video/mp4")
        if name.endswith(".jpg"):
            return self._send(200, _cover(), "image/jpeg")
        return super()._cdn(u)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9303)
    ap.add_argument("--cert-dir", required=True)
    a = ap.parse_args(argv)
    cert, key = fis.make_cert(Path(a.cert_dir))
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    srv.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"journey fake instagram on https://127.0.0.1:{a.port}", flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
