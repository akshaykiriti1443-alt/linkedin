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
from .schema import ReelAnalysis

SYSTEM = """You are a short-form video strategist. You break down Instagram reels so a creator can learn from them and reuse what works.

You get: still frames from the reel (with timestamps), the timed transcript, the original caption, and the creator's profile.

Work like this:
- Read the frames for on-screen text, setting, and editing. The visuals often carry the message when the audio is only music.
- Split the reel into its skeleton: the hook (the first 1-3 seconds), the meat (ordered beats), and the CTA.
- Quote spoken and on-screen words exactly. Do not invent lines you cannot see or hear; say so in confidence_notes.
- The action plan is for the viewer: concrete steps they can take today.
- hook_variations, script_ideas, ready_script and fill_in_template are for the creator in the profile. Reuse this reel's skeleton, not its topic, unless the topic fits their niche.
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
