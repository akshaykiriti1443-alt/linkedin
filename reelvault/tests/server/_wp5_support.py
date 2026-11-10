"""Shared helpers for the WP5 tests: seeded members, processed reels, a fake AI that answers every schema, and a
fake OpenAI-compatible HTTP server (port of .../saas/ai/test_fake.py). Not a test module."""

from __future__ import annotations

import json
import random
import re
import shutil
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORTS = range(9200, 9220)

DIGEST = {
    "brand_id": "me",
    "niche_label": "Meta ads for local dentists",
    "language": "en",
    "search_terms": ["dental marketing"],
    "who_we_help": "Local dentists who want more new patients",
    "dream_outcome": "A full calendar",
    "top_pains": [
        "Ads stop working after two weeks",
        "Leads never pick up the phone",
        "Agencies take money and vanish",
    ],
    "failed_attempts": ["Boosting posts"],
    "objections": ["Too expensive"],
    "offer": "Done-for-you Meta ads, $2k a month",
    "proof": ["Took Smile Co from 12 to 41 new patients a month in 90 days"],
    "difference": "We fix the follow-up too",
    "voice": {"tone": ["direct"], "always": ["patients"], "never": ["leads"], "style_refs": [], "samples": []},
    "platforms": "Instagram",
    "main_cta": "Comment AUDIT",
    "capabilities": {
        "on_camera": True,
        "voice": True,
        "screen": True,
        "broll": False,
        "hands": False,
        "second_person": False,
        "location": False,
        "editor": False,
        "hours_per_week": 4,
        "languages": ["en"],
    },
    "covered_topics": [],
    "hard_nos": ["No income claims"],
    "competitors": [],
}


def free_port() -> int:
    for p in PORTS:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port in 9200-9219")


# ============================================================================================== seeding


def approve_profile(uid: str, digest: dict | None = None) -> None:
    from reelvault.server import db

    with db.tx() as c:
        c.execute(db.profiles.delete().where(db.profiles.c.user_id == uid))
        c.execute(
            db.profiles.insert().values(
                user_id=uid,
                brand_id="me",
                status="approved",
                answers={},
                digest=digest or DIGEST,
                version=1,
                approved_at=db.utcnow(),
            )
        )


def add_topics(uid: str, slugs=("ad-creative", "client-results")) -> None:
    from reelvault.server import db

    with db.tx() as c:
        for i, s in enumerate(slugs):
            c.execute(
                db.topics.insert().values(
                    id=db.new_id("top"),
                    user_id=uid,
                    slug=s,
                    label=s.replace("-", " "),
                    description=f"reels that show {s}",
                    status="active",
                    position=i,
                )
            )


def add_account(uid: str, handle: str = "alexhormozi", role: str = "competitor") -> str:
    from reelvault.server import db

    aid = db.new_id("acc")
    with db.tx() as c:
        c.execute(
            db.accounts.insert().values(
                id=aid, user_id=uid, handle=handle, role=role, status="active", followers=120000, baseline_views=40000
            )
        )
    return aid


def add_reel(uid: str, account_id: str = "acc_test", **kw) -> str:
    from reelvault.server import db

    rid = db.new_id("reel")
    code = "C" + "".join(random.choice("abcdefghijk0123456789") for _ in range(10))
    vals = dict(
        id=rid,
        user_id=uid,
        account_id=account_id,
        ig_pk=str(random.randint(10**17, 10**18)),
        code=code,
        url=f"https://www.instagram.com/reel/{code}/",
        tier="priority",
        sample_weight=1.0,
        caption="How we got 41 new patients. Comment AUDIT.",
        views=180000,
        likes=5100,
        comments=93,
        score=4.4,
        band="outlier",
        basis="views",
        duration=24.0,
        audio={"type": "original", "title": "Original audio"},
    )
    vals.update(kw)
    with db.tx() as c:
        c.execute(db.reels.insert().values(**vals))
    return rid


_PROCESSED: dict[str, tuple] = {}


def processed_clip(name: str = "talking_head", tmp_root: Path | None = None):
    """(Signals, reel_dir) for one generated fixture clip, processed once per test session (no speech-to-text)."""
    if name in _PROCESSED:
        return _PROCESSED[name]
    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
    import make_media  # noqa: E402

    from reelvault import media, signals

    clips = make_media.ensure(ROOT / "tests" / "fixtures" / "media")
    import tempfile

    out = Path(tempfile.mkdtemp(prefix=f"wp5_{name}_"))
    orig = signals.transcribe_server
    signals.transcribe_server = lambda *a, **k: dict(media.EMPTY_TRANSCRIPT)
    try:
        sig = signals.process(
            clips[name],
            out,
            ig_audio={"type": "original", "title": "Original audio"},
            words=False,
            whisper_model="base",
        )
    finally:
        signals.transcribe_server = orig
    _PROCESSED[name] = (sig, out)
    return sig, out


def give_signals(uid: str, rid: str, name: str = "talking_head", *, transcript: dict | None = None) -> None:
    """Copy a processed clip into the reel's folder and write its reel_signals row (signals_state done)."""
    from reelvault.server import db, storage

    sig, src = processed_clip(name)
    dest = storage.member_path(uid, f"reels/{rid}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    row = sig.to_row()
    if transcript is not None:
        row["transcript"] = transcript
    with db.tx() as c:
        c.execute(db.reel_signals.delete().where(db.reel_signals.c.reel_id == rid))
        c.execute(db.reel_signals.insert().values(reel_id=rid, user_id=uid, **row))
        c.execute(
            db.reels.update()
            .where(db.reels.c.id == rid)
            .values(media_state="have", signals_state="done", video_path=f"reels/{rid}/video.mp4")
        )


def set_mode(uid: str, mode: str | None) -> None:
    from reelvault.server import db

    with db.tx() as c:
        c.execute(db.users.update().where(db.users.c.id == uid).values(ai_mode=mode))


# ============================================================================================== fake AI answers


def _ids(rx: str, text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(rx, text)))


def quick_tag(rid: str, i: int = 0) -> dict:
    return {
        "reel_id": rid,
        "visual_format": "VF01",
        "visual_format_other": "",
        "visual_format_2": ["VF02"],
        "visual_confidence": 0.8,
        "visual_evidence": [1, 2],
        "visual_subject": "Man talking at a desk",
        "recipe": {
            "headline_box": True,
            "caption_style": "word_by_word_highlight",
            "caption_look": "yellow caps",
            "face_bubble": False,
            "broll_share": "light",
            "setting": "desk",
            "framing": "medium",
        },
        "sound_primary": "SD01",
        "sound_secondary": [],
        "topics": ["ad-creative"] if i % 2 == 0 else ["nope"],
        "new_topic": "",
        "hook_type": "bold_claim",
        "hook_line": "This ad got 41 patients.",
        "cta_type": "comment_keyword",
        "has_text_hook": True,
        "one_line": "Shows an ad that worked " + "x" * 200,
    }


def breakdown(rid: str) -> dict:
    hook = {
        "spoken": "This ad got 41 patients",
        "on_screen_text": "41 PATIENTS",
        "visual": "face close-up",
        "hook_type": "bold_claim",
        "why_it_works": "A number",
        "ends_at_seconds": 2.0,
    }
    return {
        "schema_version": 1,
        "reel_id": rid,
        "title": "Ad that got 41 patients",
        "summary": "A short talk.",
        "topics": ["ad-creative"],
        "format": {
            "primary": "VF01",
            "secondary": [],
            "other_label": "",
            "confidence": 0.8,
            "evidence_frames": [1],
            "why": "face in every frame",
            "recipe": "sit at a desk",
        },
        "skeleton": {
            "structure_name": "Claim > Proof > CTA",
            "hook": hook,
            "meat": [{"start_seconds": 2, "point": "proof", "visual": "screenshot", "purpose": "proof"}],
            "cta": {"text": "Comment AUDIT", "cta_type": "comment_keyword", "placement": "end", "keyword": "AUDIT"},
            "pacing": "fast",
            "retention_devices": ["open loop"],
        },
        "sound": {
            "primary": "SD01",
            "secondary": [],
            "evidence_seconds": [1.0],
            "music": "none",
            "cues": [{"t": 999.0, "kind": "whoosh", "what": "invented", "purpose": "none"}],
            "takeaway": "voice only",
        },
        "edit": {
            "jump_cuts": True,
            "zoom_punch_ins": "few",
            "caption_style": "word_by_word_highlight",
            "caption_look": "yellow",
            "headline_box": True,
            "broll_share": "light",
            "visual_hook": "face_closeup",
            "graphics": [],
            "proof_inserts": True,
            "transitions": ["hard_cut"],
            "framing": "medium",
            "setting": "desk",
            "look": "natural",
            "loop_ending": False,
            "rehooks": 1,
            "face_bubble": False,
            "speed_effects": [],
            "cta_visual": "keyword on screen at the end",
        },
        "why_it_worked": ["number in the hook"],
        "what_to_steal": ["open on the number"],
        "what_to_avoid": [],
        "weaknesses": [
            {"id": "W1", "kind": "slow_hook", "note": "Takes 3 s to land", "at_s": 0.5},
            {"id": "W1", "kind": "generic_cta", "note": "Generic CTA", "at_s": None},
        ],
        "fill_in_template": "This [THING] got [RESULT].",
        "hook_formula": "This [THING] got [RESULT]",
        "confidence_notes": "",
    }


def _cue(ref_rid: str | None = None) -> dict:
    return {
        "t": "00:00.0",
        "until": "",
        "kind": "hit",
        "what": "bass hit",
        "purpose": "stops the scroll",
        "level": "loud",
        "pattern": "SD15",
        "source_hint": "",
        "ref": {
            "reel_id": ref_rid,
            "url": "https://evil.example/",
            "start": "0:00",
            "end": "0:02",
            "watch_for": "the hit",
        }
        if ref_rid
        else None,
    }


def script(src: str, weak: str | None, pain: str) -> dict:
    beats = [
        {"t_start": 0, "t_end": 3, "say": "Your ads die in two weeks.", "on_screen_text": "ADS DIE", "visual": "face"},
        {"t_start": 3, "t_end": 30, "say": "Here is why.", "on_screen_text": "", "visual": "screen"},
    ]
    brief = {
        "format": "VF01",
        "format_recipe": "desk talking head",
        "aspect": "9:16",
        "length_s": 30,
        "pace": "cut every 2 s",
        "caption_style": "word_by_word_highlight",
        "caption_look": "yellow",
        "broll": [],
        "graphics": [],
        "proof_inserts": ["Smile Co dashboard"],
        "color_look": "natural",
        "music": {"type": "none", "mood": "", "bpm_range": "", "level": "", "options": [], "notes": ""},
        "references": [{"reel_id": src, "url": "", "start": "0:00", "end": "0:03", "watch_for": "the hook"}],
        "dos": ["look at the lens"],
        "donts": ["read a script"],
    }
    return {
        "title": "Why your ads die",
        "based_on": [src],
        "angle": "Dentist version",
        "format": "VF01",
        "sub_format": "",
        "topic": "ad-creative",
        "pain": pain,
        "proof_used": "none available",
        "upgrades": [
            {
                "vs_source": src,
                "fixes": f"{src}:{weak}" if weak else f"{src}:general",
                "change": "Open on the result",
                "why": "faster hook",
            }
        ],
        "length_s": 30,
        "hook": {
            "spoken": "Your ads die in two weeks",
            "on_screen_text": "ADS DIE",
            "visual": "face",
            "hook_type": "pain_point",
            "why_it_works": "pain",
            "ends_at_seconds": 3,
        },
        "beats": beats,
        "cta": {"text": "Comment AUDIT", "cta_type": "comment_keyword", "placement": "end", "keyword": "AUDIT"},
        "caption": "Ads dying?",
        "hashtags": ["#dentist"],
        "editor_brief": brief,
        "sound_cue_sheet": [_cue(src), _cue(None)],
        "why_it_should_work": ["VF01 works here"],
        "too_close": [],
    }


def ad() -> dict:
    a15 = {
        "length_s": 30,
        "beats": [{"t_start": 0, "t_end": 15, "say": "Need patients?", "on_screen_text": "", "visual": "face"}],
        "cta": "Book a call",
        "editor_notes": "",
        "sound_cues": [],
    }
    a30 = dict(a15, length_s=30)
    angles = ["pain", "result", "contrarian", "curiosity", "callout"]
    return {
        "angle": "follow-up",
        "awareness": "problem_aware",
        "offer": "Meta ads",
        "ad_15": a15,
        "ad_30": a30,
        "hook_test": {
            "body": "ad_30",
            "hooks": [
                {"id": "H1", "angle": a, "spoken": a, "on_screen_text": a, "visual": "face", "first_frame": "face"}
                for a in angles
            ],
            "setup": ["Make 5 ads"],
            "read_results": ["3-second view rate"],
        },
        "primary_text": "Your ads die in two weeks. Here is the fix.",
        "headline": "Fix your ads",
        "description": "Free audit",
        "cta_button": "BOOK_NOW",
        "based_on": [],
        "policy_notes": [],
    }


def plan_chunk(idx: int, text: str) -> dict:
    fm = re.findall(r"- fmt:(\S+)", text)
    rl = re.findall(r"- reel:(\S+)", text)
    bo = f"fmt:{fm[0]}" if fm else f"reel:{rl[0]}"
    days = []
    for i in range(7):
        if i % 2 == 0:
            days.append(
                {
                    "date": "2020-01-01",
                    "post_type": "reel",
                    "topic": "ad-creative",
                    "format": "VF01",
                    "builds_on": bo,
                    "hook": f"hook {idx}-{i}",
                    "idea": "idea",
                    "cta_type": "comment_keyword",
                    "based_on": [],
                    "effort": "quick",
                }
            )
        else:
            days.append(
                {
                    "date": "2020-01-01",
                    "post_type": "rest",
                    "topic": "unsorted",
                    "format": "none",
                    "builds_on": "",
                    "hook": "",
                    "idea": "",
                    "cta_type": "none",
                    "based_on": [],
                    "effort": "quick",
                }
            )
    pillars = (
        [
            {"topic": "ad-creative", "label": "Ads", "share_pct": 50, "why": "x"},
            {"topic": "client-results", "label": "Results", "share_pct": 30, "why": "x"},
            {"topic": "unsorted", "label": "Other", "share_pct": 21, "why": "x"},
        ]
        if idx == 0
        else []
    )
    return {
        "kind": "content_plan",
        "chunk_index": idx,
        "pillars": pillars,
        "weeks": [{"week": idx + 1, "theme": f"week {idx + 1}", "days": days}],
        "batching_tips": ["film 3 at once"] if idx == 0 else [],
        "measure": ["views"] if idx == 0 else [],
    }


def report(text: str) -> dict:
    allowed = re.search(r"card_value must be one of: ([^\n]+)", text)
    vals = [v.strip() for v in allowed.group(1).split(",")] if allowed and "(none" not in allowed.group(1) else []
    vals.sort(key=lambda v: not v.startswith("visual_format:VF01"))
    cands = _ids(r'"reel_id": "(reel_[a-z0-9]+)"', text)
    pains = re.search(r"Customer pains \(copy exactly\): ([^\n]+)", text)
    pain = pains.group(1).split(" | ")[0] if pains else ""
    return {
        "kind": "competitor_report",
        "headline": ["a", "b", "c"],
        "competitors": [
            {
                # name a competitor the member really tracks (the server rejects reports that don't)
                "handle": (re.findall(r'"handle": "([^"]+)"', text) or ["alexhormozi"])[0],
                "what_they_do": "talks",
                "what_works": "VF01",
                "best_hooks": [],
                "cta_pattern": "",
                "sound_pattern": "",
                "gaps": [],
            }
        ],
        "working": [{"finding": "Talking head works", "card_value": vals[0], "how_to_use": "do it"}] if vals else [],
        "overused": [],
        "opportunities": [],
        "sound_design": [],
        "recommended_formats": [{"format": "VF01", "sub_format": "", "why": "you can film", "first_script_idea": "x"}],
        "remake_queue": [{"reel_id": c, "why": "fresh", "angle": "dentist"} for c in cands[:5]],
        "customer_gaps": [{"pain": pain, "gap": "nobody", "idea": "show it"}] if pain else [],
        "pain_coverage": [{"pain": pain, "covered": False}] if pain else [],
        "next_steps": ["Remake #1"],
    }


def fake_answer(schema_name: str, text: str) -> dict:
    """A valid answer for each schema, built from the ids the prompt carries (the fake AI used by all paths)."""
    if schema_name == "QuickTagBatch":
        m = re.search(r"Reel ids: ([^\n]+?)\.\n", text)
        ids = [x.strip() for x in m.group(1).split(",")] if m else []
        return {"tags": [quick_tag(r, i) for i, r in enumerate(ids)]}
    if schema_name == "TopicRetagBatch":
        ids = _ids(r"^- (reel_[a-z0-9]+):", text) or _ids(r"\n- (reel_[a-z0-9]+):", text)
        return {"tags": [{"reel_id": r, "topics": ["client-results"], "new_topic": ""} for r in ids]}
    if schema_name == "ReelBreakdown":
        rid = re.search(r"reel_id (reel_[a-z0-9]+)", text).group(1)
        return breakdown(rid)
    if schema_name == "FormatGuideBatch":
        ids = _ids(r"=== Format (fmt_[a-z0-9]+) ===", text)
        return {
            "guides": [
                {
                    "format_id": f,
                    "name": "Desk talking head + top headline box",
                    "what_it_is": "x",
                    "why_it_works": ["y"],
                    "shoot_steps": ["sit"],
                    "edit_steps": ["cut"],
                    "sound_recipe": ["voice only"],
                    "needs": ["on_camera"],
                    "time_to_make": "under_1h",
                }
                for f in ids
            ]
        }
    if schema_name == "ProfileDraft":
        blank = {"value": "", "items": [], "source_ids": [], "confidence": 0.0, "note": ""}
        ans = {f"q{i}": dict(blank) for i in range(1, 15)}
        ans["q1"] = {"value": "Local dentists", "items": [], "source_ids": ["src_x"], "confidence": 0.9, "note": "site"}
        ans["q3"] = {"value": "", "items": ["No patients"], "source_ids": [], "confidence": 0.3, "note": "guess"}
        ans["q7"] = {"value": "Meta ads", "items": [], "source_ids": [], "confidence": 0.8, "note": ""}
        return {
            "answers": ans,
            "competitor_handles": ["@SomeOne"],
            "niche_label": "Dentist ads",
            "search_terms": ["dentist ads"],
        }
    if schema_name == "TopicList":
        return {
            "topics": [
                {"slug": "Ad Creative", "label": "Ad creative", "description": "shows ads", "examples": []},
                {
                    "slug": "Canva templates",
                    "label": "Canva templates",
                    "description": "shows canva",
                    "examples": ["a"],
                },
            ]
        }
    if schema_name == "ReelScriptPack":
        src = _ids(r"=== Source reel (reel_[a-z0-9]+)", text)[0]
        weak = re.search(rf"{src}:(W\d)", text)
        pain = re.search(r"Use this customer pain: ([^\n]+)", text)
        sc = script(src, weak.group(1) if weak else None, pain.group(1) if pain else DIGEST["top_pains"][0])
        sub = re.search(r"set format to '(VF\d+)' and sub_format to '(fmt_[a-z0-9]+)'", text)
        if sub:   # a "Use this format" brief names the format to keep (fix round 2)
            sc["format"], sc["sub_format"] = sub.group(1), sub.group(2)
            sc["editor_brief"]["format"] = sub.group(1)
        return {"kind": "reel_script", "scripts": [sc]}
    if schema_name == "MetaAdPack":
        return {"kind": "meta_ads", "ads": [ad()]}
    if schema_name == "ContentPlanChunk":
        idx = int(re.search(r"chunk_index (\d+)", text).group(1))
        return plan_chunk(idx, text)
    if schema_name == "CompetitorReport":
        return report(text)
    raise KeyError(schema_name)


def schema_name_of(body: dict) -> str | None:
    rf = body.get("response_format") or {}
    if rf.get("type") == "json_schema":
        return rf["json_schema"]["name"]
    for name in (
        "QuickTagBatch",
        "TopicRetagBatch",
        "ReelBreakdown",
        "FormatGuideBatch",
        "ProfileDraft",
        "TopicList",
        "ReelScriptPack",
        "MetaAdPack",
        "ContentPlanChunk",
        "CompetitorReport",
    ):
        if f"Return a {name}" in json.dumps(body) or f"Return the {name}" in json.dumps(body):
            return name
    return None


def user_text(body: dict) -> str:
    content = body["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")


# ============================================================================================== fake server


class FakeAI:
    """A fake OpenAI-compatible server. `script` is a list of callables (body -> (status, dict, headers)) used
    in order; when empty, answers with fake_answer() for the request's schema."""

    def __init__(self):
        self.calls: list[dict] = []
        self.script: list = []
        self.port = free_port()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj, headers=None):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.calls.append({"path": self.path, "method": "GET", "auth": self.headers.get("authorization")})
                if fake.script and fake.script[0] == "401":
                    fake.script.pop(0)
                    return self._send(401, {"error": {"message": "invalid key"}})
                self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                fake.calls.append(
                    {"path": self.path, "method": "POST", "body": body, "auth": self.headers.get("authorization")}
                )
                if fake.script:
                    step = fake.script.pop(0)
                    status, obj, headers = step(body) if callable(step) else (401, {"error": {"message": "bad"}}, {})
                    return self._send(status, obj, headers)
                name = schema_name_of(body)
                content = json.dumps(fake_answer(name, user_text(body))) if name else "ok"
                self._send(200, chat(content, body.get("model")))

        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def chat(content: str, model: str | None = "fake-model", finish: str = "stop", refusal: str | None = None) -> dict:
    msg = {"role": "assistant", "content": content}
    if refusal:
        msg["refusal"] = refusal
    return {
        "id": "1",
        "object": "chat.completion",
        "created": 0,
        "model": model or "fake-model",
        "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def add_fake_connection(
    client, fake: FakeAI, *, model: str = "fake-vision-model", vision_name: bool = True, use_for: str = "all"
) -> dict:
    """Save a custom OpenAI-compatible connection to the fake server through the API (allow_local_ai_urls on)."""
    from reelvault.server import db

    db.set_setting("allow_local_ai_urls", True)
    r = client.post(
        "/api/ai/connections",
        json={
            "provider": "openai_compat",
            "preset": "custom",
            "base_url": fake.base_url,
            "model": model if not vision_name else "llava-vision-test",
            "api_key": "sk-test-1234567890abcd",
            "use_for": use_for,
            "options": {"max_images": 10},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()
