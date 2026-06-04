#!/usr/bin/env python3
"""
render.py — Lossless video rendering pipeline (learned from video-use).

Key improvements over naive FFmpeg approach:
  1. Per-segment extraction with -c copy (no re-encode)
  2. Concat demuxer (lossless join, no quality loss)
  3. 30ms audio fades at every cut point (prevents clicking)
  4. Two-pass loudness normalization (-14 LUFS for social media)
  5. Quality ladder: draft / preview / final

Usage:
  from engine.render import build_rough_cut
  build_rough_cut(video, keep_segments, output, quality="draft")
"""

import os, json, subprocess, shutil, tempfile
from pathlib import Path

QUALITY = {
    "draft":   {"scale": "1280:-2", "crf": "28", "preset": "veryfast"},
    "preview": {"scale": "1920:-2", "crf": "22", "preset": "fast"},
    "final":   {"scale": "1920:-2", "crf": "20", "preset": "slow"},
}

FADE_MS = 30  # milliseconds of audio fade at each cut point

def _run(cmd: str, label: str = ""):
    print(f"  ffmpeg: {label or cmd[:60]}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed ({label}):\n{result.stderr[-800:]}")
    return result

def extract_segment(video: str, start: float, end: float, out: str):
    """Extract a single segment losslessly (no re-encode)."""
    duration = end - start
    _run(
        f'ffmpeg -y -ss {start:.3f} -i "{video}" -t {duration:.3f} '
        f'-c copy -avoid_negative_ts make_zero "{out}"',
        f"extract {start:.1f}s-{end:.1f}s"
    )

def concat_lossless(segment_files: list, out: str):
    """Join segments via concat demuxer — no re-encode, no quality loss."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        for seg in segment_files:
            f.write(f"file '{os.path.abspath(seg)}'\n")
        list_file = f.name
    try:
        _run(
            f'ffmpeg -y -f concat -safe 0 -i "{list_file}" -c copy "{out}"',
            "concat (lossless)"
        )
    finally:
        os.unlink(list_file)

def add_audio_fades(video: str, cut_points_sec: list, out: str):
    """
    Add 30ms audio fade-out before each cut + fade-in after.
    Prevents the clicking/popping that happens at hard cuts.
    """
    if not cut_points_sec:
        shutil.copy(video, out)
        return

    fade_s = FADE_MS / 1000.0
    # Build afade chain: fade out just before each cut, fade in just after
    filters = []
    for t in cut_points_sec:
        filters.append(f"afade=t=out:st={t - fade_s:.4f}:d={fade_s}")
        filters.append(f"afade=t=in:st={t:.4f}:d={fade_s}")

    filter_str = ",".join(filters)
    _run(
        f'ffmpeg -y -i "{video}" -af "{filter_str}" -c:v copy "{out}"',
        f"audio fades ({len(cut_points_sec)} cut points)"
    )

def normalize_loudness(video: str, out: str, target_lufs: float = -14.0):
    """
    Two-pass loudness normalization to -14 LUFS (Instagram/YouTube standard).
    Pass 1: measure. Pass 2: apply.
    """
    # Pass 1 — measure
    result = subprocess.run(
        f'ffmpeg -i "{video}" -af loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json -f null -',
        shell=True, capture_output=True, text=True
    )
    # Extract JSON from stderr
    import re
    m = re.search(r'\{[\s\S]*"input_i"[\s\S]*?\}', result.stderr)
    if not m:
        print("  WARNING: loudnorm measurement failed, copying as-is")
        shutil.copy(video, out)
        return

    stats = json.loads(m.group(0))
    il  = stats["input_i"]
    lra = stats["input_lra"]
    tp  = stats["input_tp"]
    off = stats["target_offset"]

    # Pass 2 — apply with measured stats
    _run(
        f'ffmpeg -y -i "{video}" '
        f'-af "loudnorm=I={target_lufs}:TP=-1.5:LRA=11:'
        f'measured_I={il}:measured_LRA={lra}:measured_TP={tp}:'
        f'measured_thresh={stats["input_thresh"]}:offset={off}:linear=true" '
        f'-c:v copy "{out}"',
        f"loudnorm → {target_lufs} LUFS"
    )

def quality_encode(video: str, out: str, mode: str = "draft"):
    """Re-encode to final quality (draft/preview/final)."""
    q = QUALITY.get(mode, QUALITY["draft"])
    _run(
        f'ffmpeg -y -i "{video}" '
        f'-vf "scale={q["scale"]}" '
        f'-c:v libx264 -crf {q["crf"]} -preset {q["preset"]} '
        f'-c:a aac -b:a 192k -movflags +faststart "{out}"',
        f"encode ({mode})"
    )

def build_rough_cut(
    video: str,
    keep_segments: list,   # [{start, end}]
    output: str,
    quality: str = "draft",
    tmp_dir: str = None,
):
    """
    Full pipeline: extract segments → lossless concat → audio fades → loudnorm → encode.
    Returns path to output file.
    """
    own_tmp = tmp_dir is None
    if own_tmp:
        tmp_dir = tempfile.mkdtemp(prefix="videobot_")

    try:
        print(f"\n🎬 Building rough cut ({len(keep_segments)} segments, quality={quality})")

        # Step 1: extract each segment losslessly
        seg_files = []
        for i, seg in enumerate(keep_segments):
            seg_out = os.path.join(tmp_dir, f"seg_{i:04d}.mp4")
            extract_segment(video, seg["start"], seg["end"], seg_out)
            seg_files.append(seg_out)

        # Step 2: concat losslessly
        concat_out = os.path.join(tmp_dir, "concat.mp4")
        concat_lossless(seg_files, concat_out)

        # Step 3: audio fades at cut points
        # Cut points are cumulative timeline positions after concat
        cut_points = []
        tl = 0.0
        for seg in keep_segments[:-1]:
            tl += seg["end"] - seg["start"]
            cut_points.append(tl)

        faded_out = os.path.join(tmp_dir, "faded.mp4")
        add_audio_fades(concat_out, cut_points, faded_out)

        # Step 4: loudness normalization
        normed_out = os.path.join(tmp_dir, "normed.mp4")
        normalize_loudness(faded_out, normed_out)

        # Step 5: quality encode
        os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
        quality_encode(normed_out, output, quality)

        print(f"  ✅ Rough cut → {output}")
        return output

    finally:
        if own_tmp and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--video",    required=True)
    p.add_argument("--segments", required=True, help="JSON file: [{start,end}]")
    p.add_argument("--output",   default="workspace/rough_cut.mp4")
    p.add_argument("--quality",  choices=["draft","preview","final"], default="draft")
    a = p.parse_args()
    with open(a.segments) as f:
        segs = json.load(f)
    build_rough_cut(a.video, segs, a.output, a.quality)
