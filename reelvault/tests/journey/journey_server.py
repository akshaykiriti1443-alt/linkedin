"""ReelVault Members dev server for the integration journey (web + worker, real pipeline). Test harness only.

    python journey_server.py --port 9301 --data-dir DIR --ai-port 9304 --site-port 9305 --ig-port 9303

- dev.configure(): admin login, throwaway master key, PUBLIC_URL http://localhost:<port>
- RV_TEST_PACING=1 (the extension test build's short gaps); the server's own CDN download (SERVER_CDN_FETCH=1, the
  default) is routed to the fake Instagram's CDN, which refuses every 5th video to the server (403), so those reels
  go the extension's ig_videos way (both video paths run),
  RV_MODELS_DIR from the environment (CED-tiny, whisper, YuNet; deploy/fetch_models.sh)
- a fake OpenAI-compatible AI on --ai-port: WP5's fake answers, except that quick tags follow each reel's LOCAL
  visual guess and the reel's real speech/sound, so lift comes from what the server measured
- a local "agency website" on --site-port (tests/fixtures/web/*.html); the server's SSRF guard is opened for
  127.0.0.1 in THIS process only, so the website import runs against it
Prints `READY {json}` and runs until SIGTERM.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "server"))
MODELS = os.environ.get("RV_MODELS_DIR", "")   # a filled models folder (deploy/fetch_models.sh); empty = DSP only
FORMAT_SUBJECT = {"VF01": "Woman talking to camera at a desk", "VF04": "Creator over a screenshot (green screen)",
                  "VF05": "Screen recording of an ads manager with a face bubble", "VF06": "Split screen reaction",
                  "VF09": "Slideshow of text slides", "VF19": "Animated shapes"}


def fake_ai(port: int):
    import _wp5_support as w5

    w5.PORTS = range(port, port + 1)
    real = w5.fake_answer

    def answer(schema_name, text):
        out = real(schema_name, text)
        if schema_name == "QuickTagBatch":
            blocks = re.split(r"=== Reel (reel_[a-z0-9]+)", text)
            guess = {}
            for i in range(1, len(blocks) - 1, 2):
                m = re.search(r"Local format guess: (VF\d\d)", blocks[i + 1])
                guess[blocks[i]] = (m.group(1) if m else "VF01", blocks[i + 1])
            for t in out["tags"]:
                vf, body = guess.get(t["reel_id"], ("VF01", ""))
                t["visual_format"] = vf
                t["visual_format_2"] = []
                t["visual_subject"] = FORMAT_SUBJECT.get(vf, "Creator talking")
                t["recipe"]["headline_box"] = vf in ("VF04", "VF01")
                t["recipe"]["face_bubble"] = vf == "VF05"
                t["recipe"]["setting"] = "studio" if vf == "VF04" else ("screen_only" if vf == "VF05" else "desk")
                t["sound_primary"] = "SD05" if "whoosh" in body.lower() or "hit" in body.lower() else "SD01"
                t["hook_type"] = "bold_claim" if vf in ("VF04", "VF05") else "question"
                t["one_line"] = f"{FORMAT_SUBJECT.get(vf, 'Creator')} with a clear hook."
        return out

    w5.fake_answer = answer
    return w5.FakeAI()


def site(port: int):
    web = REPO / "tests" / "fixtures" / "web"

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(web), **kw)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def open_ssrf_for_localhost():
    """Harness only: the website importer may read the local test site on 127.0.0.1."""
    from reelvault.server import security

    real = security.safe_fetch

    def safe_fetch(url, *a, **kw):
        if url.startswith("http://127.0.0.1:"):
            kw["allow_private"] = True
        return real(url, *a, **kw)

    security.safe_fetch = safe_fetch
    try:
        from reelvault.importers import website

        if getattr(website, "safe_fetch", None) is real:
            website.safe_fetch = safe_fetch
    except ImportError:
        pass


import httpx  # noqa: E402


class CdnToFake(httpx.BaseTransport):
    """httpx transport for jobs_media.TRANSPORT: *.cdninstagram.com -> the fake Instagram on 127.0.0.1 (self-signed)."""

    def __init__(self, port: int):
        self.port = port
        self.inner = httpx.HTTPTransport(verify=False)

    def handle_request(self, request):
        if request.url.host.endswith("cdninstagram.com"):
            request.url = request.url.copy_with(host="127.0.0.1", port=self.port)
            request.headers["x-journey-server"] = "1"
        return self.inner.handle_request(request)

    def close(self):   # shared across downloads: keep the inner pool open
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9301)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--ai-port", type=int, default=9304)
    ap.add_argument("--site-port", type=int, default=9305)
    ap.add_argument("--ig-port", type=int, default=9303)
    a = ap.parse_args(argv)
    if 8765 in (a.port, a.ai_port, a.site_port):
        raise SystemExit("Port 8765 is the desktop app's. Pick another one.")
    os.environ.update({"SERVER_CDN_FETCH": "1", "RV_TEST_PACING": "1"})
    if MODELS:
        os.environ["RV_MODELS_DIR"] = MODELS
    from reelvault.server import db, dev

    info = dev.configure(a.port, a.data_dir)
    import uvicorn

    from reelvault.server.app import create_app, prepare_runtime

    prepare_runtime()
    open_ssrf_for_localhost()
    from reelvault.server import jobs_media

    jobs_media.TRANSPORT = CdnToFake(a.ig_port)
    db.set_setting("allow_local_ai_urls", True)
    ai = fake_ai(a.ai_port)
    s = site(a.site_port)
    info.update({"port": a.port, "ai_base": ai.base_url, "site": f"http://127.0.0.1:{a.site_port}/home.html"})
    print("READY " + json.dumps(info), flush=True)

    def _exit(*_a):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _exit)
    try:
        uvicorn.run(create_app("all", prepare=False), host="127.0.0.1", port=a.port, log_level="warning")
    finally:
        ai.stop()
        s.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
