"""ffmpeg helpers: probe, pull audio, pull key frames, transcribe."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise SystemExit(f"'{tool}' is not installed. Install ffmpeg first (it ships ffprobe too).")
    return path


@dataclass
class Probe:
    duration: float
    has_audio: bool
    width: int
    height: int


def probe(video: Path) -> Probe:
    out = subprocess.run(
        [_need("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return Probe(
        duration=float(data.get("format", {}).get("duration") or 0),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        width=int(v.get("width") or 0),
        height=int(v.get("height") or 0),
    )


def frame_times(duration: float, max_frames: int) -> list[float]:
    """Dense at the start (the hook lives there), even spread after, one near the end (CTA)."""
    if duration <= 0:
        return [0.0]
    hook = [t for t in (0.2, 1.0, 2.0, 3.0) if t < duration]
    rest_n = max(max_frames - len(hook) - 1, 0)
    start = min(3.5, duration)
    step = (duration - start) / (rest_n + 1) if rest_n else 0
    body = [start + step * (i + 1) for i in range(rest_n)]
    end = [max(duration - 0.5, 0)]
    times = sorted({round(t, 2) for t in hook + body + end})
    return times[:max_frames]


def extract_frames(video: Path, out_dir: Path, times: list[float], width: int) -> list[tuple[float, Path]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = _need("ffmpeg")
    frames = []
    for i, t in enumerate(times):
        dest = out_dir / f"frame_{i:02d}_{t:06.2f}s.jpg"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{t}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(dest)],
            check=True,
        )
        if dest.exists():
            frames.append((t, dest))
    return frames


def load_audio(video: Path, rate: int = 16000):
    """Decode the audio track to 16 kHz mono float32 with ffmpeg (no PyAV needed)."""
    import numpy as np

    raw = subprocess.run(
        [_need("ffmpeg"), "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(rate),
         "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


_whisper = None


def transcribe(video: Path, model_size: str) -> dict:
    """Local speech-to-text with faster-whisper. Returns text plus timed segments."""
    global _whisper
    from faster_whisper import WhisperModel

    if _whisper is None:
        print(f"  loading whisper '{model_size}' (first run downloads it)...")
        _whisper = WhisperModel(model_size, device="auto", compute_type="int8")
    audio = load_audio(video)
    if audio.size == 0:
        return {"language": "", "language_probability": 0, "text": "", "segments": []}
    segments, info = _whisper.transcribe(audio, vad_filter=True, beam_size=5)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
    return {
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "text": " ".join(s["text"] for s in segs).strip(),
        "segments": segs,
    }


def timed_transcript(tr: dict) -> str:
    if not tr.get("segments"):
        return "(no speech detected)"
    return "\n".join(f"[{s['start']:>6.1f}s] {s['text']}" for s in tr["segments"])
