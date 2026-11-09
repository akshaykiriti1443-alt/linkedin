"""Turn one downloaded reel into a full breakdown.

Steps: probe -> frames -> audio -> transcript -> Claude (frames + transcript
+ caption) -> structured ReelAnalysis -> file into the library.
"""

from __future__ import annotations

import base64
import json
import traceback
from pathlib import Path

from . import brain
from .config import Config
from .library import Library
from .media import extract_frames, frame_times, probe, timed_transcript, transcribe
from .purposes import PURPOSES, clean_guess
from .schema import PurposeGuesses, ReelAnalysis, RemakePack

SYSTEM = """You are a short-form video strategist. You break down Instagram reels so a creator can learn from them and reuse what works.

You get: still frames from the reel (with timestamps), the timed transcript, the original caption, and the creator's profile.

Work like this:
- Read the frames for on-screen text, setting, and editing. The visuals often carry the message when the audio is only music.
- Split the reel into its skeleton: the hook (the first 1-3 seconds), the meat (ordered beats), and the CTA.
- Quote spoken and on-screen words exactly. Do not invent lines you cannot see or hear; say so in confidence_notes.
- The action plan is for the viewer: concrete steps they can take today.
- hook_variations, script_ideas, ready_script and fill_in_template are for the creator in the profile. Reuse this reel's skeleton, not its topic, unless the topic fits their niche.
- purpose_guess: why this creator most likely saved the reel (learn, hook, format or remake), with one short reason.
- claims_to_check: numbers, stats or bold claims the viewer should verify before repeating them.
- client_explainer: 2-3 plain sentences the creator could paste to a client.
- Write in plain, simple words. Short sentences. Active voice.
- Pick the category from the allowed list only."""


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def build_content(frames: list[tuple[float, Path]], transcript: dict, meta: dict, duration: float,
                  has_audio: bool) -> list[dict]:
    content: list[dict] = []
    for t, path in frames:
        content.append({"type": "text", "text": f"Frame at {t:.1f}s"})
        content.append(_image_block(path))
    audio_note = "" if has_audio else "\nNOTE: this video has no audio track."
    content.append({
        "type": "text",
        "text": (
            f"Reel by @{meta.get('owner', 'unknown')} · length {duration:.1f}s{audio_note}\n\n"
            f"<caption>\n{meta.get('caption') or '(none)'}\n</caption>\n\n"
            f"<transcript language=\"{transcript.get('language', '?')}\">\n{timed_transcript(transcript)}\n</transcript>\n\n"
            "Break this reel down."
        ),
    })
    return content


def build_text_prompt(frames: list[tuple[float, Path]], transcript: dict, meta: dict, duration: float,
                      has_audio: bool) -> str:
    """Same input as build_content, for Claude Code: frames are files it opens with Read."""
    frame_list = "\n".join(f"- {t:.1f}s: frames/{p.name}" for t, p in frames)
    audio_note = "" if has_audio else "\nNOTE: this video has no audio track."
    return (
        f"Reel by @{meta.get('owner', 'unknown')} · length {duration:.1f}s{audio_note}\n\n"
        f"Still frames (open every one with the Read tool before answering):\n{frame_list}\n\n"
        f"<caption>\n{meta.get('caption') or '(none)'}\n</caption>\n\n"
        f"<transcript language=\"{transcript.get('language', '?')}\">\n{timed_transcript(transcript)}\n</transcript>\n"
    )


class Analyzer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.context = (
            "<allowed_categories>\n" + "\n".join(cfg.categories) + "\n</allowed_categories>\n\n"
            "<creator_profile>\n" + cfg.profile_text() + "\n</creator_profile>"
        )
        if cfg.brain != "api":
            return
        import anthropic

        self.client = anthropic.Anthropic()
        # The system prompt + profile is identical for every reel, so cache it.
        self.system = [
            {"type": "text", "text": SYSTEM},
            {
                "type": "text",
                "text": (
                    "<allowed_categories>\n" + "\n".join(cfg.categories) + "\n</allowed_categories>\n\n"
                    "<creator_profile>\n" + cfg.profile_text() + "\n</creator_profile>"
                ),
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def ask(self, folder: Path, frames, tr: dict, meta: dict, duration: float, has_audio: bool) -> ReelAnalysis:
        if self.cfg.brain == "api":
            return self.ask_claude(build_content(frames, tr, meta, duration, has_audio))
        return brain.structured(
            build_text_prompt(frames, tr, meta, duration, has_audio),
            "Read every frame listed below, then break this reel down. Answer only with the structured output.",
            SYSTEM + "\n\n" + self.context,
            folder, ReelAnalysis, read_files=True, model=self.cfg.model, effort=self.cfg.effort,
        )

    def ask_claude(self, content: list[dict]) -> ReelAnalysis:
        resp = self.client.beta.messages.parse(
            model=self.cfg.model,
            max_tokens=16000,
            system=self.system,
            messages=[{"role": "user", "content": content}],
            output_format=ReelAnalysis,
            output_config={"effort": self.cfg.effort},
            # If a safety filter declines, the API retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            cat = resp.stop_details.category if resp.stop_details else None
            raise RuntimeError(f"Claude declined this reel (category: {cat}).")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("Response hit max_tokens before the breakdown finished.")
        if resp.parsed_output is None:
            raise RuntimeError("Claude returned no structured output.")
        return resp.parsed_output

    def process(self, lib: Library, code: str, folder: Path) -> Path:
        video = folder / "video.mp4"
        if not video.exists():
            raise FileNotFoundError(f"No video.mp4 in {folder}")
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8")) if (folder / "meta.json").exists() else {}

        info = probe(video)
        print(f"  {info.duration:.0f}s video, audio={'yes' if info.has_audio else 'no'}")

        frames = extract_frames(video, folder / "frames", frame_times(info.duration, self.cfg.max_frames),
                                self.cfg.frame_width)

        if info.has_audio:
            tr = transcribe(video, self.cfg.whisper_model)
        else:
            tr = {"language": "", "text": "", "segments": []}
        (folder / "transcript.txt").write_text(tr["text"] or "(no speech detected)", encoding="utf-8")
        (folder / "transcript_timed.txt").write_text(timed_transcript(tr), encoding="utf-8")

        print(f"  asking Claude ({len(frames)} frames, {len(tr['segments'])} speech segments)...")
        analysis = self.ask(folder, frames, tr, meta, info.duration, info.has_audio)

        record = {
            "shortcode": code,
            "meta": meta,
            "duration": round(info.duration, 1),
            "transcript": tr,
            "frames": [{"t": t, "file": f"frames/{p.name}"} for t, p in frames],
            "video": "video.mp4",
            "analysis": analysis.model_dump(),
            "model": self.cfg.model,
        }
        return lib.file(code, folder, record)


# --------------------------------------------------------------------------
# On demand: "Make it better" and purpose guesses for older reels
# --------------------------------------------------------------------------

REMAKE_SYSTEM = """You help one creator make their own, stronger version of a reel they saved.

You get the original reel's breakdown (skeleton, transcript, caption) and the creator's profile.

Rules:
- Borrow the idea and what makes it work. Never borrow the original creator's wording, stories, jokes or claims.
- Write for the creator in the profile: their niche, their offer, their voice. If made_for is a client name,
  write it for that client's business instead, still in plain words.
- whats_weak: what the original does badly or leaves out. your_angle: how the new version is better.
- script: the full script, ready to shoot, with [VISUAL] cues. Hook first, then the beats, then the CTA.
- too_close: list every line in your script that is still close to the original's words or claims, and say why.
  Better: rewrite those lines so the list is empty.
- Plain, simple words. Short sentences. Active voice."""

GUESS_SYSTEM = """You sort saved Instagram reels by why the creator saved them. Pick one purpose per reel:
- learn: saved for the advice or information.
- hook: the first seconds stopped the scroll; worth borrowing the opening.
- format: the way the video is built is worth reusing.
- remake: the creator could make a stronger version for their own niche.
Give one short reason per reel, in plain words. Copy each reel id exactly. Answer for every reel."""


def ask_structured(cfg: Config, prompt: str, instruction: str, system: str, model_cls, effort: str | None,
                   cwd: Path | None = None):
    """One text-only Claude call that returns model_cls (Claude Code, or the API when brain = "api")."""
    if cfg.brain != "api":
        return brain.structured(prompt, instruction, system, cwd or cfg.vault_dir, model_cls, read_files=False,
                                model=cfg.model, effort=effort)
    import anthropic

    resp = anthropic.Anthropic().beta.messages.parse(
        model=cfg.model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt + "\n\n" + instruction}],
        output_format=model_cls,
        output_config={"effort": effort or cfg.effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request.")
    if resp.parsed_output is None:
        raise RuntimeError("Claude returned no structured output.")
    return resp.parsed_output


def remake_brief(rec: dict) -> dict:
    a = rec["analysis"]
    sk = a["skeleton"]
    return {
        "title": a["title"],
        "creator": rec.get("meta", {}).get("owner", ""),
        "summary": a["summary"],
        "structure": sk["structure_name"],
        "hook": sk["hook"],
        "meat": sk["meat"],
        "cta": sk["cta"],
        "why_it_performed": a.get("why_it_performed", ""),
        "transcript": (rec.get("transcript") or {}).get("text", ""),
        "caption": rec.get("meta", {}).get("caption", ""),
    }


def make_remake(cfg: Config, rec: dict, made_for: str = "me", notes: str = "") -> RemakePack:
    made_for = (made_for or "me").strip() or "me"
    who = "me (the creator in the profile)" if made_for.lower() == "me" else f"my client: {made_for}"
    prompt = (
        f"<creator_profile>\n{cfg.profile_text()}\n</creator_profile>\n\n"
        f"<original_reel>\n{json.dumps(remake_brief(rec), indent=2, ensure_ascii=False)}\n</original_reel>\n\n"
        f"made_for: {made_for}\nWrite it for {who}.\n"
        + (f"\n<notes_from_creator>\n{notes.strip()}\n</notes_from_creator>\n" if notes and notes.strip() else "")
    )
    print(f"Writing a better version for {made_for}...")
    pack = ask_structured(cfg, prompt, "Make a stronger, original version of this reel. Answer only with the "
                          "structured output.", REMAKE_SYSTEM, RemakePack, cfg.effort)
    pack.made_for = made_for
    return pack


def guess_brief(rec: dict) -> dict:
    a = rec["analysis"]
    sk = a["skeleton"]
    return {
        "id": rec["shortcode"],
        "title": a["title"],
        "summary": a["summary"],
        "hook": sk["hook"].get("spoken") or sk["hook"].get("on_screen_text", ""),
        "structure_name": sk["structure_name"],
    }


def guess_purposes(cfg: Config, recs: list[dict]) -> dict[str, dict]:
    """One low-effort Claude call for all the reels. Returns {reel id: {"purpose", "reason"}}."""
    if not recs:
        return {}
    briefs = [guess_brief(r) for r in recs]
    prompt = (
        "Purposes: " + ", ".join(f"{k} ({v})" for k, v in PURPOSES.items()) + "\n\n"
        f"<reels>\n{json.dumps(briefs, indent=1, ensure_ascii=False)}\n</reels>\n"
    )
    print(f"Guessing purposes for {len(briefs)} reel(s)...")
    out = ask_structured(cfg, prompt, "Guess one purpose for every reel. Answer only with the structured output.",
                         GUESS_SYSTEM, PurposeGuesses, "low")
    wanted = {b["id"] for b in briefs}
    result = {}
    for g in out.guesses:
        guess = clean_guess({"purpose": g.purpose, "reason": g.reason})
        if g.id in wanted and guess:
            result[g.id] = guess
    return result


def process_pending(cfg: Config, lib: Library, retry_failed: bool = False, limit: int | None = None) -> tuple[int, int]:
    rows = lib.pending(retry_failed)[:limit] if limit else lib.pending(retry_failed)
    if not rows:
        print("Nothing to process.")
        return 0, 0
    analyzer = Analyzer(cfg)
    ok = bad = 0
    for i, row in enumerate(rows, 1):
        code = row["shortcode"]
        print(f"[{i}/{len(rows)}] {code}")
        try:
            dest = analyzer.process(lib, code, Path(row["folder"]))
            print(f"  -> {dest}")
            ok += 1
        except brain.BrainError as e:
            lib.mark_failed(code, str(e))
            print(f"  {e}")
            bad += 1
            msg = str(e).lower()
            if "not signed in" in msg or "not installed" in msg or "usage limit" in msg:
                print("  stopping this run; it will continue next time.")
                break
        except Exception as e:
            name = type(e).__name__
            if name == "AuthenticationError":
                raise SystemExit("Claude API key is missing or wrong. Set ANTHROPIC_API_KEY.")
            if name == "RateLimitError":
                lib.mark_failed(code, "rate limited")
                print("  rate limited; stopping. Run again later to continue.")
                bad += 1
                break
            lib.mark_failed(code, f"{e}\n{traceback.format_exc()}")
            print(f"  failed: {e}")
            bad += 1
    return ok, bad
