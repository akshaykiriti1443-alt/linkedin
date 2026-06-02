#!/usr/bin/env python3
"""
grade.py — Auto color grading (learned from video-use/helpers/grade.py).

Samples frames, measures brightness/saturation/contrast,
then applies a bounded ffmpeg eq+curves+colorbalance filter.

Usage:
  python engine/grade.py --video workspace/raw.mp4 --output workspace/graded.mp4
  python engine/grade.py --video workspace/raw.mp4 --preset warm_cinematic --output workspace/graded.mp4
"""

import subprocess, os, json, tempfile, shutil
from pathlib import Path

# ── Presets ────────────────────────────────────────────────────────────────
PRESETS = {
    "warm_cinematic": {
        "eq":           "brightness=0.02:contrast=1.08:saturation=1.12:gamma=0.97",
        "colorbalance": "rs=0.03:gs=0.0:bs=-0.04:rm=0.02:gm=0.0:bm=-0.02:rh=0.01:gh=0.0:bh=-0.01",
        "curves":       "r='0/0 0.5/0.54 1/1':g='0/0 0.5/0.50 1/1':b='0/0 0.5/0.47 1/0.97'",
    },
    "neutral_punch": {
        "eq":           "brightness=0.0:contrast=1.12:saturation=1.15:gamma=1.0",
        "colorbalance": "rs=0.0:gs=0.0:bs=0.0:rm=0.0:gm=0.01:bm=0.0:rh=0.0:gh=0.0:bh=0.0",
        "curves":       "r='0/0 0.5/0.5 1/1':g='0/0 0.5/0.51 1/1':b='0/0 0.5/0.49 1/1'",
    },
    "subtle": {
        "eq":           "brightness=0.01:contrast=1.04:saturation=1.05:gamma=0.99",
        "colorbalance": "rs=0.01:gs=0.0:bs=-0.01:rm=0.0:gm=0.0:bm=0.0:rh=0.0:gh=0.0:bh=0.0",
        "curves":       "default",
    },
    "none": None,
}

def _run(cmd, label=""):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({label}):\n{r.stderr[-600:]}")
    return r

def sample_frames(video: str, n: int = 8) -> dict:
    """Sample N evenly-spaced frames and measure avg brightness/saturation."""
    with tempfile.TemporaryDirectory() as tmp:
        # Extract frames
        _run(
            f'ffmpeg -y -i "{video}" -vf "select=not(mod(n\\,{max(1, 30*n)}))" '
            f'-frames:v {n} -vsync vfr "{tmp}/frame_%02d.png"',
            "sample frames"
        )
        frames = sorted(Path(tmp).glob("frame_*.png"))
        if not frames:
            return {"brightness": 0.5, "saturation": 0.5, "contrast": 0.5}

        # Measure via ffprobe signalstats
        stats_list = []
        for frame in frames[:n]:
            r = subprocess.run(
                f'ffprobe -v error -show_frames -select_streams v '
                f'-read_intervals "%+#1" '
                f'-f lavfi -i "movie={frame},signalstats" '
                f'-show_entries frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.SATAVG '
                f'-of json',
                shell=True, capture_output=True, text=True
            )
            try:
                data = json.loads(r.stdout)
                tags = data["frames"][0]["tags"]
                stats_list.append({
                    "y":   float(tags.get("lavfi.signalstats.YAVG", 128)),
                    "sat": float(tags.get("lavfi.signalstats.SATAVG", 64)),
                })
            except Exception:
                pass

        if not stats_list:
            return {"brightness": 0.5, "saturation": 0.5}

        avg_y   = sum(s["y"]   for s in stats_list) / len(stats_list)
        avg_sat = sum(s["sat"] for s in stats_list) / len(stats_list)
        return {
            "brightness": avg_y / 255.0,
            "saturation": avg_sat / 255.0,
        }

def auto_grade_filter(stats: dict) -> str:
    """
    Derive bounded eq filter from measured frame stats.
    Adjustments capped at ±8% to avoid overcorrection.
    """
    brightness = stats.get("brightness", 0.5)
    saturation = stats.get("saturation", 0.5)

    # Target: brightness ~0.45 (slight underexpose looks cinematic)
    b_adj = max(-0.08, min(0.08, 0.45 - brightness))
    # Target: saturation ~0.40 (slightly punchy)
    s_adj = max(0.85,  min(1.15, 1.0 + (0.40 - saturation) * 0.5))
    # Contrast: boost slightly if flat
    c_adj = 1.06 if saturation < 0.30 else 1.03

    return f"eq=brightness={b_adj:.3f}:contrast={c_adj:.2f}:saturation={s_adj:.2f}"

def build_filter(preset: str, video: str = None) -> str | None:
    """Build complete ffmpeg filter string for the chosen preset or auto mode."""
    if preset == "none":
        return None

    if preset == "auto":
        if video is None:
            raise ValueError("video path required for auto preset")
        stats = sample_frames(video)
        eq_filter = auto_grade_filter(stats)
        print(f"  Auto grade: brightness={stats['brightness']:.2f} sat={stats['saturation']:.2f}")
        print(f"  Filter: {eq_filter}")
        return eq_filter

    p = PRESETS.get(preset)
    if p is None:
        return None

    parts = [p["eq"]]
    if p.get("colorbalance"):
        parts.append(f"colorbalance={p['colorbalance']}")
    if p.get("curves") and p["curves"] != "default":
        parts.append(f"curves={p['curves']}")
    return ",".join(parts)

def apply_grade(video: str, output: str, preset: str = "warm_cinematic"):
    """Apply color grade and write output file."""
    filt = build_filter(preset, video)
    if filt is None:
        print(f"  Grade: none — copying as-is")
        shutil.copy(video, output)
        return

    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    _run(
        f'ffmpeg -y -i "{video}" -vf "{filt}" -c:v libx264 -crf 18 -preset fast '
        f'-c:a copy "{output}"',
        f"grade ({preset})"
    )
    print(f"  ✅ Graded → {output}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--video",   required=True)
    p.add_argument("--output",  default="workspace/graded.mp4")
    p.add_argument("--preset",  choices=list(PRESETS.keys()) + ["auto"], default="warm_cinematic")
    a = p.parse_args()
    apply_grade(a.video, a.output, a.preset)
