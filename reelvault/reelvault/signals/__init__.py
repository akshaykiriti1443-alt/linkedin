"""Local signals for one reel: cuts, frames, sound map, transcript, visual stats, edit stats, contact sheets
(DESIGN-SAAS.md 6.1-6.5.1). Everything runs on the server's CPU for free; the AI gets the result as text.

    sig = process(video, out_dir, ig_audio=reel.audio, words=True, whisper_model="small")
    sig.to_row()          # the reel_signals columns (6.12)

Order (cheapest first, so a failure costs little, 6.1): probe -> cuts -> frames -> sound map (CED + DSP) ->
transcript only when the sound map finds speech -> visual stats -> edit stats -> sheets.
`out_dir` is the reel's folder (`users/<uid>/reels/<reel_id>/`); frames go to `out_dir/frames/`, sheets to
`out_dir/sheet_<dest>.jpg`. Raises VideoRejected (plain message) for videos over 5 min or 50 MB, or unreadable.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..media import NO_WINDOW, EMPTY_TRANSCRIPT, _need, cut_frame_times, extract_masters, transcribe_server

log = logging.getLogger("reelvault.signals")

SIGNALS_VERSION = 1          # bump when the pipeline changes (reel_signals.version, dedupe keys)
MAX_SECONDS = 300
MAX_BYTES = 50 * 1024 * 1024
MAX_FRAMES = 12


class VideoRejected(Exception):
    """The video cannot be processed; `message` is plain words for the member."""

    def __init__(self, message: str, code: str = "rejected"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class Signals:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    transcript: dict = field(default_factory=dict)
    cuts: list = field(default_factory=list)
    frames: list = field(default_factory=list)
    sound: dict = field(default_factory=dict)
    edit: dict = field(default_factory=dict)
    visual: dict = field(default_factory=dict)
    has_audio: bool = False
    skipped_asr: bool = False
    contact_sheet_path: str | None = None
    version: int = SIGNALS_VERSION
    timings: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        """Values for the reel_signals table (without reel_id / user_id / created_at)."""
        return {"version": self.version, "duration": self.duration, "width": self.width, "height": self.height,
                "fps": self.fps, "transcript": self.transcript, "cuts": self.cuts, "frames": self.frames,
                "contact_sheet_path": self.contact_sheet_path, "sound": self.sound, "edit": self.edit,
                "visual": self.visual, "error": None}

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(video: Path) -> VideoInfo:
    """ffprobe: duration, size, frame rate, audio presence. Raises VideoRejected when unreadable."""
    p = subprocess.run([_need("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    try:
        data = json.loads(p.stdout or "{}")
    except ValueError:
        data = {}
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if p.returncode != 0 or v is None:
        raise VideoRejected("This video can't be read. It may be damaged.", "unreadable")
    num, _, den = str(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    dur = float((data.get("format") or {}).get("duration") or v.get("duration") or 0)
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = str((v.get("tags") or {}).get("rotate") or "")
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = str(sd["rotation"])
    if rot.lstrip("-") in ("90", "270"):
        w, h = h, w
    return VideoInfo(duration=round(dur, 3), width=w, height=h, fps=round(fps, 3) if fps < 1000 else 0.0,
                     has_audio=any(s.get("codec_type") == "audio" for s in streams))


def check_video(video: Path, info: VideoInfo | None = None) -> VideoInfo:
    """Reject > 5 min or > 50 MB (6.1) with the member-facing message."""
    video = Path(video)
    if not video.is_file():
        raise VideoRejected("The video file is missing. ReelVault will download it again.", "missing")
    if video.stat().st_size > MAX_BYTES:
        raise VideoRejected("Video too big. ReelVault reads reels up to 50 MB.", "too_big")
    info = info or probe(video)
    if info.duration > MAX_SECONDS:
        raise VideoRejected("Video too long. ReelVault reads reels up to 5 minutes.", "too_long")
    if info.duration <= 0.1:
        raise VideoRejected("This video is empty.", "empty")
    return info


def process(video: Path, out_dir: Path, *, ig_audio: dict | None = None, words: bool = False,
            whisper_model: str = "base", has_audio: bool | None = None, whisper_threads: int = 2,
            whisper_workers: int = 1, transcribe=None, progress=None) -> Signals:
    """Run every local signal for one reel (see the module doc). `transcribe(audio, model, words)` can be injected
    (tests); `progress(step, message)` is called between steps (the job handler renews its lease there)."""
    from . import cuts as cuts_mod
    from . import editstats, frames as frames_mod, sound as sound_mod, soundmap, visual as visual_mod

    video, out_dir = Path(video), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tm: dict[str, float] = {}
    t0 = time.perf_counter()

    def lap(name: str, msg: str | None = None) -> None:
        nonlocal t0
        now = time.perf_counter()
        tm[name] = round(now - t0, 2)
        t0 = now
        if progress and msg:
            progress(name, msg)

    info = check_video(video)
    lap("probe", "Finding the cuts")
    cuts = cuts_mod.detect_cuts(video, info.fps)
    lap("cuts", "Picking frames")
    plan = cut_frame_times(info.duration, cuts, MAX_FRAMES)
    masters = extract_masters(video, out_dir / "frames", plan)
    if not masters:
        raise VideoRejected("ReelVault could not take pictures from this video.", "no_frames")
    lap("frames", "Listening to the sound")
    audio_ok = info.has_audio and has_audio is not False
    raw = sound_mod.analyze(video, keep_audio=True) if audio_ok else None
    lap("sound", "Reading the words")
    gate = raw.speech_likely() if raw is not None else False
    skipped = not (audio_ok and raw is not None and raw.has_audio and gate is not False)
    if skipped:
        tr = dict(EMPTY_TRANSCRIPT)
    else:
        fn = transcribe or (lambda a, m, w: transcribe_server(a, m, words=w, cpu_threads=whisper_threads,
                                                              workers=whisper_workers))
        tr = fn(raw.audio, whisper_model, words)
        tr = {**EMPTY_TRANSCRIPT, **(tr or {})}
    tr["model"] = None if skipped else whisper_model
    tr["skipped"] = skipped
    lap("transcript", "Measuring the picture")
    if raw is not None and raw.detector != "none":
        events = sound_mod.fuse(raw, tr.get("speech_spans"))
        smap = soundmap.build(raw, events, cuts, duration=info.duration, transcript=tr, ig_audio=ig_audio)
    else:
        smap = soundmap.empty(info.duration, has_audio=bool(raw is not None and raw.has_audio),
                              ig_audio=ig_audio)
    if raw is not None:
        raw.audio = None
    lap("soundmap")
    speech_ratio = sum(b - a for a, b in tr.get("speech_spans") or []) / max(0.01, info.duration)
    vis = visual_mod.compute(video, masters, reel_dir=out_dir, duration=info.duration, cuts=cuts,
                             speech_ratio=speech_ratio, width=info.width, height=info.height)
    lap("visual", "Making contact sheets")
    edit = editstats.compute(info.duration, cuts, tr, smap, len(masters))
    frames_out = [{"n": f["n"], "t": f["t"], "shot": f["shot"], "kind": f["kind"], "path": f"frames/{Path(f['path']).name}"}
                  for f in masters]
    frames_mod.write_index(out_dir, frames_out, duration=info.duration, per_frame=vis.get("per_frame"),
                           width=info.width, height=info.height)
    sizes = frames_mod.make_default_sheets(out_dir)
    lap("sheets")
    vis["sheet_bytes"] = sizes
    return Signals(duration=info.duration, width=info.width, height=info.height, fps=info.fps, transcript=tr,
                   cuts=cuts, frames=frames_out, sound=smap, edit=edit, visual=vis, has_audio=audio_ok,
                   skipped_asr=skipped, contact_sheet_path="sheet_paid_api.jpg", timings=tm)
