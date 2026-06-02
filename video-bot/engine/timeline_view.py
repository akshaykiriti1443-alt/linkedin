#!/usr/bin/env python3
"""
timeline_view.py — Filmstrip + waveform preview PNG (learned from video-use).

Generates workspace/timeline_preview.png so you can SEE your edit
before committing — shows frames, silences, word labels, audio envelope.

Usage:
  python engine/timeline_view.py --video workspace/raw.mp4 --transcript workspace/transcript.json
"""

import os, json, subprocess, tempfile, math
from pathlib import Path

def _run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True)

def extract_frames(video: str, n: int = 10, tmp: str = None) -> list:
    """Extract N evenly-spaced frames as PNG paths."""
    out_dir = tmp or tempfile.mkdtemp()
    _run(
        f'ffmpeg -y -i "{video}" '
        f'-vf "select=not(mod(n\\,{max(1, 30*n)}))" '
        f'-frames:v {n} -vsync vfr "{out_dir}/frame_%02d.png"'
    )
    return sorted(Path(out_dir).glob("frame_*.png"))

def extract_waveform(video: str, tmp: str) -> list:
    """Extract audio as 16kHz mono, return RMS envelope (100 buckets)."""
    wav = os.path.join(tmp, "audio.wav")
    _run(f'ffmpeg -y -i "{video}" -vn -acodec pcm_s16le -ar 16000 -ac 1 "{wav}"')
    if not os.path.exists(wav):
        return [0.0] * 100

    import wave, struct
    with wave.open(wav, 'r') as wf:
        frames = wf.readframes(wf.getnframes())
        samples = struct.unpack(f'<{len(frames)//2}h', frames)

    bucket_size = max(1, len(samples) // 100)
    rms = []
    for i in range(100):
        chunk = samples[i*bucket_size:(i+1)*bucket_size]
        if chunk:
            rms.append(math.sqrt(sum(s*s for s in chunk) / len(chunk)) / 32768.0)
        else:
            rms.append(0.0)
    return rms

def get_video_duration(video: str) -> float:
    r = subprocess.run(
        f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{video}"',
        shell=True, capture_output=True, text=True
    )
    try:
        return float(r.stdout.strip())
    except Exception:
        return 60.0

def generate_preview(video: str, transcript_path: str, output_png: str):
    """Generate filmstrip + waveform + word labels PNG."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  WARNING: Pillow not installed — skipping timeline preview.")
        print("  Install: pip install Pillow")
        return

    duration = get_video_duration(video)
    N_FRAMES  = 10
    FRAME_W   = 160
    FRAME_H   = 90
    WAVEFORM_H = 60
    LABEL_H    = 28
    PADDING    = 12
    TOTAL_W    = N_FRAMES * FRAME_W + 2 * PADDING
    TOTAL_H    = FRAME_H + WAVEFORM_H + LABEL_H + 3 * PADDING

    img = Image.new("RGB", (TOTAL_W, TOTAL_H), (18, 18, 24))
    draw = ImageDraw.Draw(img)

    # Try to load a monospace font
    font_sm = ImageFont.load_default()
    try:
        font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 11)
    except Exception:
        pass

    with tempfile.TemporaryDirectory() as tmp:
        # ── Filmstrip ─────────────────────────────────────────────────────
        frames = extract_frames(video, N_FRAMES, tmp)
        for i, fp in enumerate(frames[:N_FRAMES]):
            try:
                frame_img = Image.open(fp).resize((FRAME_W, FRAME_H))
                img.paste(frame_img, (PADDING + i * FRAME_W, PADDING))
            except Exception:
                pass

        # ── Waveform ──────────────────────────────────────────────────────
        rms = extract_waveform(video, tmp)
        wave_y_base = PADDING + FRAME_H + PADDING
        for i, val in enumerate(rms):
            x = PADDING + int(i / 100 * (TOTAL_W - 2 * PADDING))
            bar_h = int(val * WAVEFORM_H)
            color = (80, 200, 120) if val > 0.02 else (60, 60, 70)
            draw.rectangle([x, wave_y_base + WAVEFORM_H - bar_h, x + 3, wave_y_base + WAVEFORM_H], fill=color)

    # ── Silence shading ───────────────────────────────────────────────────
    segments = []
    if os.path.exists(transcript_path):
        with open(transcript_path) as f:
            data = json.load(f)
        for seg in data.get("segments", []):
            words = seg.get("words", [])
            for j in range(1, len(words)):
                gap_start = words[j-1]["end"]
                gap_end   = words[j]["start"]
                if gap_end - gap_start > 0.3:
                    segments.append((gap_start, gap_end))

    for gs, ge in segments:
        x1 = PADDING + int(gs / duration * (TOTAL_W - 2 * PADDING))
        x2 = PADDING + int(ge / duration * (TOTAL_W - 2 * PADDING))
        draw.rectangle([x1, PADDING, x2, PADDING + FRAME_H + PADDING + WAVEFORM_H],
                       fill=(180, 40, 40, 80))

    # ── Time ruler ────────────────────────────────────────────────────────
    label_y = PADDING + FRAME_H + PADDING + WAVEFORM_H + PADDING // 2
    n_ticks = 10
    for i in range(n_ticks + 1):
        t = duration * i / n_ticks
        x = PADDING + int(i / n_ticks * (TOTAL_W - 2 * PADDING))
        draw.line([(x, label_y), (x, label_y + 6)], fill=(120, 120, 140))
        draw.text((x - 10, label_y + 8), f"{t:.0f}s", fill=(180, 180, 200), font=font_sm)

    # ── Word labels on first row ──────────────────────────────────────────
    if os.path.exists(transcript_path):
        with open(transcript_path) as f:
            data = json.load(f)
        words_flat = [w for seg in data.get("segments", []) for w in seg.get("words", [])]
        # Sample every ~3s to avoid clutter
        last_x = -100
        for w in words_flat:
            t = w.get("start", 0)
            x = PADDING + int(t / duration * (TOTAL_W - 2 * PADDING))
            if x - last_x > 80:
                draw.text((x, PADDING + FRAME_H - 18), w["word"].strip()[:10],
                          fill=(255, 220, 80), font=font_sm)
                last_x = x

    # ── Title bar ─────────────────────────────────────────────────────────
    draw.text((PADDING, 2), f"Timeline Preview  |  {duration:.1f}s  |  Red = silence gaps",
              fill=(160, 160, 180), font=font_sm)

    os.makedirs(os.path.dirname(output_png) or ".", exist_ok=True)
    img.save(output_png)
    print(f"  ✅ Timeline preview → {output_png}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--video",      required=True)
    p.add_argument("--transcript", default="workspace/transcript.json")
    p.add_argument("--output",     default="workspace/timeline_preview.png")
    a = p.parse_args()
    generate_preview(a.video, a.transcript, a.output)
