"""ffmpeg helpers: probe, pull audio, pull key frames, transcribe."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise SystemExit(f"'{tool}' is not installed. Install ffmpeg first (it ships ffprobe too).")
    return path


# The app runs under pythonw (no console). Without this flag Windows opens a black window for every
# ffmpeg/ffprobe call: about 14 per reel.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass
class Probe:
    duration: float
    has_audio: bool
    width: int
    height: int


def probe(video: Path) -> Probe:
    out = subprocess.run(
        [_need("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
        capture_output=True, text=True, check=True, creationflags=NO_WINDOW,
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
            check=True, creationflags=NO_WINDOW,
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
        capture_output=True, check=True, creationflags=NO_WINDOW,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


_whisper = None
_whisper_device = None
GPU_ERRORS = ("cublas", "cudnn", "cuda", "cudart", "nvrtc", "gpu")


def _load_whisper(model_size: str, device: str):
    global _whisper, _whisper_device
    import os

    os.environ.setdefault("HF_HUB_VERBOSITY", "error")  # hide the harmless "HF_TOKEN" download warning
    from faster_whisper import WhisperModel

    _whisper = WhisperModel(model_size, device=device, compute_type="int8")
    _whisper_device = device


def transcribe(video: Path, model_size: str) -> dict:
    """Local speech-to-text with faster-whisper. Returns text plus timed segments.

    Tries the graphics card first (fast). If its NVIDIA speech libraries are missing,
    it switches to the normal processor (CPU) and keeps going.
    """
    if _whisper is None:
        print(f"  loading speech-to-text '{model_size}' (the first run downloads it)...")
        try:
            _load_whisper(model_size, "auto")
        except Exception as e:
            if not any(k in str(e).lower() for k in GPU_ERRORS):
                raise
            _load_whisper(model_size, "cpu")
    audio = load_audio(video)
    if audio.size == 0:
        return {"language": "", "language_probability": 0, "text": "", "segments": []}
    try:
        segs, info = _run(audio)
    except Exception as e:
        # The GPU libraries fail only when the first audio is processed, so catch it here.
        if _whisper_device == "cpu" or not any(k in str(e).lower() for k in GPU_ERRORS):
            raise
        print("  graphics-card speech tools aren't installed; using the processor instead (a bit slower).")
        _load_whisper(model_size, "cpu")
        segs, info = _run(audio)
    return {
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "text": " ".join(s["text"] for s in segs).strip(),
        "segments": segs,
    }


def _run(audio):
    segments, info = _whisper.transcribe(audio, vad_filter=True, beam_size=5)
    # segments is lazy: the real work (and any GPU error) happens while reading it
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
    return segs, info


def timed_transcript(tr: dict) -> str:
    if not tr.get("segments"):
        return "(no speech detected)"
    return "\n".join(f"[{s['start']:>6.1f}s] {s['text']}" for s in tr["segments"])


# ============================================================================================== server additions (WP3)
# DESIGN-SAAS.md 6.1-6.2. The desktop app keeps using transcribe(video, model_size) above, unchanged.

def cut_frame_times(duration: float, cuts: list[float], max_frames: int = 12) -> list[dict]:
    """Cut-aware frame picking (6.1): hook 0.2/1/2/3 s, `cut + 0.20` for cuts after 3.2 s (at most 7, spread evenly
    by index), body frames between 3.5 s and duration - 0.6 when fewer than 7 cut frames (never within 0.4 s of
    another frame), and the end frame at max(duration - 0.5, 0).

    Returns [{"n", "t", "shot", "kind": "hook" | "cut" | "body" | "end"}] sorted by time, at most max_frames."""
    if duration <= 0:
        return [{"n": 0, "t": 0.0, "shot": 0, "kind": "hook"}]
    cuts = sorted(c for c in cuts if 0 < c < duration)
    hook = [t for t in (0.2, 1.0, 2.0, 3.0) if t < duration]
    end_t = round(max(duration - 0.5, 0.0), 2)
    cut_room = max(0, min(7, max_frames - len(hook) - 1))
    cut_ts = [round(c + 0.20, 2) for c in cuts if c > 3.2 and c + 0.20 < duration - 0.05]
    if len(cut_ts) > cut_room:
        if cut_room <= 0:
            cut_ts = []
        elif cut_room == 1:
            cut_ts = [cut_ts[len(cut_ts) // 2]]
        else:
            idx = [round(i * (len(cut_ts) - 1) / (cut_room - 1)) for i in range(cut_room)]
            cut_ts = [cut_ts[i] for i in sorted(set(idx))]
    picked: list[tuple[float, str]] = [(t, "hook") for t in hook] + [(t, "cut") for t in cut_ts]

    def free(t: float, gap: float = 0.4) -> bool:
        return all(abs(t - p) >= gap for p, _ in picked) and abs(t - end_t) >= gap

    if len(cut_ts) < 7:
        room = max(0, max_frames - len(picked) - 1)
        want = min(7 - len(cut_ts), room)
        lo, hi = 3.5, duration - 0.6
        if want > 0 and hi > lo:
            # try an even spread; skip slots that sit on an existing frame
            n_slots = want
            while n_slots <= want * 3 and sum(1 for _, k in picked if k == "body") < want:
                step = (hi - lo) / (n_slots + 1)
                for i in range(n_slots):
                    t = round(lo + step * (i + 1), 2)
                    if free(t) and sum(1 for _, k in picked if k == "body") < want:
                        picked.append((t, "body"))
                n_slots += 1
    if all(abs(end_t - p) >= 0.05 for p, _ in picked):
        picked.append((end_t, "end"))
    else:   # a hook/cut frame already sits at the end: it becomes the end frame
        k = min(range(len(picked)), key=lambda i: abs(picked[i][0] - end_t))
        picked[k] = (picked[k][0], "end")
    picked = sorted(picked)
    if len(picked) > max_frames:   # keep the end frame
        picked = picked[:max_frames - 1] + [picked[-1]]
    out = []
    for i, (t, kind) in enumerate(picked):
        shot = sum(1 for c in cuts if t >= c)
        out.append({"n": i, "t": t, "shot": shot, "kind": kind})
    return out


def frame_name(n: int, t: float) -> str:
    """frames/f00_000.20.jpg style names (3.17)."""
    return f"f{n:02d}_{t:06.2f}.jpg"


def extract_masters(video: Path, out_dir: Path, frames: list[dict], width: int = 768) -> list[dict]:
    """768 px wide JPEG q=4 masters (6.1). Adds "path" (relative to out_dir.parent) to each frame that was written.
    One ffmpeg call per frame (fast seek); frames ffmpeg cannot decode are dropped from the list."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("f*.jpg"):
        old.unlink()
    ff = _need("ffmpeg")
    done = []
    for f in frames:
        name = frame_name(f["n"], f["t"])
        dest = out_dir / name
        subprocess.run([ff, "-y", "-loglevel", "error", "-ss", f"{f['t']:.3f}", "-i", str(video), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-q:v", "4", str(dest)], check=False, creationflags=NO_WINDOW)
        if not dest.exists() and f["t"] > 0.1:   # past the last decodable frame: step back a little
            subprocess.run([ff, "-y", "-loglevel", "error", "-sseof", "-0.3", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-q:v", "4", str(dest)], check=False, creationflags=NO_WINDOW)
        if dest.exists() and dest.stat().st_size > 0:
            done.append(dict(f, path=f"{out_dir.name}/{name}"))
    return done


def contact_sheet(images: list[Path | None], dest: Path, *, cols: int, rows: int, tile_w: int, tile_h: int,
                  q: int) -> Path:
    """Tile images (reading order) into one JPEG with ffmpeg (concat list + scale/pad + tile). A None or missing
    image leaves a black tile. No text is burned in (the prompt maps tile numbers to times)."""
    import tempfile

    ff = _need("ffmpeg")
    n = cols * rows
    imgs = list(images)[:n]
    real = [Path(p) for p in imgs if p is not None and Path(p).exists()]
    # The placeholder must have the same size as the real images: a size change mid-stream makes ffmpeg rebuild the
    # filter graph, and the tile filter would then start over (an all-black sheet).
    bw, bh = tile_w, tile_h
    if real:
        out = subprocess.run([_need("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0", str(real[0])], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout.strip().split(",")
        if len(out) == 2 and out[0].isdigit() and out[1].isdigit():
            bw, bh = int(out[0]), int(out[1])
    with tempfile.TemporaryDirectory() as td:
        black = Path(td) / "black.jpg"
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=black:s={bw}x{bh}",
                        "-frames:v", "1", str(black)], check=True, creationflags=NO_WINDOW)
        lst = Path(td) / "list.txt"
        lines = []
        for i in range(n):
            p = imgs[i] if i < len(imgs) else None
            p = p if p is not None and Path(p).exists() else black
            lines.append(f"file '{Path(p).resolve().as_posix()}'")
        lst.write_text("\n".join(lines) + "\n")
        vf = (f"scale={tile_w}:{tile_h}:force_original_aspect_ratio=decrease,"
              f"crop='min(iw,{tile_w})':'min(ih,{tile_h})',"
              f"pad={tile_w}:{tile_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,tile={cols}x{rows}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-vf", vf, "-frames:v", "1", "-q:v", str(q), str(dest)], check=True, creationflags=NO_WINDOW)
    return dest


_server_models: dict[str, object] = {}
_server_lock = None


def whisper_path(name: str, models_dir: Path | None = None) -> str:
    """The baked faster-whisper snapshot for `name` (small/base) under $RV_MODELS_DIR/whisper (HF cache layout,
    deploy/fetch_models.sh), or `name` itself when it is not there (dev boxes download it)."""
    import os

    root = Path(models_dir or os.environ.get("RV_MODELS_DIR", "/opt/reelvault/models")) / "whisper" / "hub"
    repo = root / f"models--Systran--faster-whisper-{name}"
    ref = repo / "refs" / "main"
    snaps = repo / "snapshots"
    if ref.exists() and (snaps / ref.read_text().strip() / "model.bin").exists():
        return str(snaps / ref.read_text().strip())
    if snaps.is_dir():
        for s in sorted(snaps.iterdir()):
            if (s / "model.bin").exists():
                return str(s)
    return name


def server_whisper(name: str, *, cpu_threads: int = 2, workers: int = 1):
    """One shared CPU int8 model per name for the whole media pool (6.2: load once, ~0.7 GB for small + base)."""
    global _server_lock
    import os
    import threading

    if _server_lock is None:
        _server_lock = threading.Lock()
    with _server_lock:
        m = _server_models.get(name)
        if m is None:
            os.environ.setdefault("HF_HUB_VERBOSITY", "error")
            from faster_whisper import WhisperModel

            m = WhisperModel(whisper_path(name), device="cpu", compute_type="int8", cpu_threads=cpu_threads,
                             num_workers=max(1, workers))
            _server_models[name] = m
        return m


def speech_spans(spans: list[tuple[float, float]], gap: float = 0.35) -> list[list[float]]:
    """Merge word (or segment) spans whose gap is < 0.35 s (6.2)."""
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if out and a - out[-1][1] < gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([float(a), float(b)])
    return [[round(float(a), 2), round(float(b), 2)] for a, b in out]


def _vad_spans(audio, segments: list[tuple[float, float]]) -> list[tuple[float, float]]:
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        ts = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300, speech_pad_ms=60))
    except Exception:  # noqa: BLE001  (older faster-whisper: fall back to the segment times)
        return segments
    vad = [(t["start"] / 16000, t["end"] / 16000) for t in ts]
    out = []
    for a, b in vad:
        for s0, s1 in segments:
            lo, hi = max(a, s0), min(b, s1)
            if hi > lo:
                out.append((lo, hi))
    return out or segments


EMPTY_TRANSCRIPT = {"language": "", "language_probability": 0.0, "text": "", "segments": [], "words": [],
                    "speech_spans": [], "wpm": 0.0}


def transcribe_server(audio_or_video, model_name: str, *, words: bool, cpu_threads: int = 2,
                      workers: int = 1) -> dict:
    """Server transcription (6.2): CPU int8, VAD on, beam 1, word timestamps only when `words`.

    `audio_or_video` is a 16 kHz mono float32 array or a video path. Returns the 6.2 JSON:
    {"language", "language_probability", "text", "segments": [{"start","end","text"}], "words": [[s, e, "w"]],
     "speech_spans": [[s, e]], "wpm"}."""
    import numpy as np

    audio = audio_or_video if isinstance(audio_or_video, np.ndarray) else load_audio(Path(audio_or_video))
    if audio is None or audio.size == 0:
        return dict(EMPTY_TRANSCRIPT)
    model = server_whisper(model_name, cpu_threads=cpu_threads, workers=workers)
    segs, info = model.transcribe(audio, vad_filter=True, beam_size=1, word_timestamps=words,
                                  condition_on_previous_text=False)
    seg_out, word_out = [], []
    for s in segs:
        seg_out.append({"start": round(float(s.start), 2), "end": round(float(s.end), 2), "text": s.text.strip()})
        for w in (s.words or []) if words else []:
            word_out.append([round(float(w.start), 2), round(float(w.end), 2), w.word.strip()])
    if word_out:
        spans = speech_spans([(w[0], w[1]) for w in word_out])
    else:
        # Without word times, whisper's segments run into each other (one can stretch over 30 s of music), so the
        # speech spans come from the VAD itself (silero, shipped with faster-whisper), clipped to the segments.
        spans = speech_spans(_vad_spans(audio, [(s["start"], s["end"]) for s in seg_out]))
    text = " ".join(s["text"] for s in seg_out).strip()
    n_words = len(word_out) if word_out else len(text.split())
    talk = sum(b - a for a, b in spans)
    return {"language": info.language or "", "language_probability": round(float(info.language_probability), 3),
            "text": text, "segments": seg_out, "words": word_out, "speech_spans": spans,
            "wpm": round(n_words / (talk / 60), 1) if talk > 1 else 0.0}
