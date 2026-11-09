"""End-to-end smoke test with a fake Claude server (no API key, no Instagram login).

Run:  python -m pytest -q tests/        (needs ffmpeg; downloads whisper 'tiny' once)
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SAMPLE = {
    "title": "Post less, hook better",
    "category": "Content Creation & Social Media",
    "subcategory": "Posting strategy",
    "tags": ["hooks", "posting", "instagram", "consistency", "reels"],
    "language": "en",
    "content_format": "talking_head",
    "summary": "The creator says daily posting is a trap. Three posts with strong hooks beat seven weak ones.",
    "key_takeaways": ["Quality of hook beats volume", "Write the hook first"],
    "action_plan": [{"step": "Write 3 hooks before filming", "why": "The hook decides reach", "effort": "quick"}],
    "skeleton": {
        "structure_name": "Contrarian > Reason > Steps > CTA",
        "hook": {"spoken": "Stop posting every day.", "on_screen_text": "STOP POSTING DAILY",
                 "visual": "Bold text over the creator", "hook_type": "contrarian",
                 "why_it_works": "It attacks common advice, so people stay to hear why.", "ends_at_seconds": 2.0},
        "meat": [
            {"start_seconds": 2.0, "point": "Three good posts beat seven lazy ones", "visual": "Talking head", "purpose": "reason"},
            {"start_seconds": 6.0, "point": "Write the hook first", "visual": "Text overlay 'STEP 1'", "purpose": "step"},
        ],
        "cta": {"text": "Comment PLAN and I will send my template", "cta_type": "comment_keyword",
                "placement": "end", "keyword": "PLAN"},
        "pacing": "Fast, cut every 2s",
        "retention_devices": ["numbered steps"],
    },
    "visuals": {"on_screen_text": ["STOP POSTING DAILY"], "visual_style": "Test pattern background",
                "editing_techniques": ["text overlay"], "what_the_visuals_say": "Less posting, more care."},
    "fill_in_template": "Stop [COMMON ADVICE]. Here is why: [REASON]. Step 1: [STEP]. Comment [KEYWORD].",
    "hook_variations": ["Stop doing X", "You do not need Y", "Everyone says Z. Wrong.", "Quit A", "B is a lie"],
    "script_ideas": [{"title": "Stop cold DMs", "hook": "Stop sending cold DMs.", "outline": ["why", "do this"], "cta": "Comment DM"}],
    "ready_script": "HOOK: Stop sending cold DMs. [VISUAL: phone]\nMEAT: ...\nCTA: Comment DM.",
    "why_it_performed": "Contrarian hook plus a keyword CTA drives comments.",
    "notable_quotes": ["Three good posts beat seven lazy ones."],
    "confidence_notes": "",
    "purpose_guess": {"purpose": "hook", "reason": "The contrarian opening stops the scroll."},
    "claims_to_check": ["Three posts beat seven"],
    "client_explainer": "Post fewer reels with better openings. The first two seconds decide who keeps watching.",
}

REQUESTS: list[dict] = []


class FakeClaude(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        REQUESTS.append({"path": self.path, "headers": dict(self.headers), "body": body})
        structured = "format" in body.get("output_config", {})
        text = json.dumps(SAMPLE) if structured else "## Script 1: Stop cold DMs\nHOOK: ..."
        msg = {"id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
               "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
               "usage": {"input_tokens": 10, "output_tokens": 10}}
        if not body.get("stream"):
            out = json.dumps(msg).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        start = dict(msg, content=[], stop_reason=None)
        events = [
            ("message_start", {"type": "message_start", "message": start}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 10}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())


def make_video(path: Path, seconds: int = 6, audio: bool = True) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=360x640:rate=25:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
    subprocess.run(cmd + ["-c:v", "libx264", str(path)], check=True)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), FakeClaude)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.chdir(tmp_path)
    src = Path(os.environ.get("REELVAULT_TEST_VIDEOS", tmp_path / "in"))
    if not src.exists():
        src.mkdir()
        make_video(src / "reel_a.mp4")
        make_video(src / "reel_silent.mp4", audio=False)
    (tmp_path / "reelvault.toml").write_text('vault_dir = "vault"\nbrain = "api"\nwhisper_model = "tiny"\nmax_frames = 6\n')
    yield tmp_path, src
    srv.shutdown()


def test_full_pipeline(env, capsys):
    from reelvault.cli import main

    root, src = env
    main(["fetch", "--folder", str(src)])
    main(["process"])
    main(["site"])
    main(["kb"])
    main(["search", "hook"])
    main(["remix", "--topic", "cold DMs", "--count", "1"])
    main(["status"])
    out = capsys.readouterr().out
    assert "0 failed" in out, out

    vault = root / "vault"
    notes = list(vault.glob("library/*/*/note.md"))
    assert len(notes) >= 2
    folder = notes[0].parent
    for f in ("video.mp4", "analysis.json", "transcript.txt", "transcript_timed.txt"):
        assert (folder / f).exists(), f
    assert list((folder / "frames").glob("*.jpg"))
    assert "## Skeleton" in notes[0].read_text()
    assert (vault / "index.html").read_text().count('"id":') >= 2
    for f in ("README.md", "hooks.md", "ctas.md", "structures.md", "templates.md", "action-plans.md"):
        assert (vault / "knowledge-base" / f).exists(), f
    assert list((vault / "scripts").glob("*.md"))

    analysis_calls = [r for r in REQUESTS if "format" in r["body"].get("output_config", {})]
    req = analysis_calls[0]
    assert req["body"]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["headers"].get("anthropic-beta", "")
    assert any(b["type"] == "image" for b in req["body"]["messages"][0]["content"])
