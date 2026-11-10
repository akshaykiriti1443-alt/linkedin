"""Hard-cut detection with ffmpeg (DESIGN-SAAS.md 6.3).

    detect_cuts(video, fps) -> [seconds]   # sorted, each the time of the FIRST frame of the new shot

Primary: the `scdet` filter; every frame's score is read, and a frame is a cut when its score is >= 8 (the design's
threshold) or when it stands far above the frames around it (adaptive; catches zoom jump cuts and cuts between flat
graphics, which score 1.5-6). `gt(scene,0.3)` is never used: it misses same-background jump cuts.
Fallback when this ffmpeg has no scdet: `select='gt(scene,0.15)'` + `showinfo`. Cuts closer than 0.25 s to the previous one are dropped (flashes, dissolves).
"""
from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from ..media import NO_WINDOW, _need

SCDET_THRESHOLD = 8
FALLBACK_SCENE = 0.15
MIN_GAP_S = 0.25
REL_MIN = 1.2          # adaptive pick: smallest score that can be a cut
REL_FACTOR = 4.0       # ... and how far above the busiest nearby frames it must stand
REL_FLOOR = 0.3
FROZEN_MIN = 0.4       # between frozen frames (stills), a change this small is already a cut
FROZEN_REF = 0.02
_SCD_RE = re.compile(r"lavfi\.scd\.time=([0-9.]+)")
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


@lru_cache(maxsize=1)
def has_scdet() -> bool:
    out = subprocess.run([_need("ffmpeg"), "-hide_banner", "-filters"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    return bool(re.search(r"\sscdet\s", out))


def _thin(times: list[float], min_gap: float = MIN_GAP_S) -> list[float]:
    out: list[float] = []
    for t in sorted(times):
        if t <= 0.05:          # a "cut" at the very first frame is the start of the video
            continue
        if out and t - out[-1] < min_gap:
            continue
        out.append(t)
    return out


def frame_scores(video: Path, *, timeout: float = 300) -> list[tuple[float, float]]:
    """[(pts_time, scdet score)] for every frame (score 0-100, ffmpeg's scene-change measure)."""
    vf = f"scale=270:-2,scdet=threshold={SCDET_THRESHOLD},metadata=print:file=-"
    p = subprocess.run([_need("ffmpeg"), "-hide_banner", "-nostats", "-i", str(video), "-an", "-vf", vf,
                        "-f", "null", "-"], capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    out: list[tuple[float, float]] = []
    t = None
    for line in p.stdout.splitlines():
        m = _PTS_RE.search(line)
        if m:
            t = float(m.group(1))
            continue
        if t is not None and line.startswith("lavfi.scd.score="):
            try:
                out.append((t, float(line.split("=", 1)[1])))
            except ValueError:
                pass
            t = None
    return out


def pick_cuts(scores: list[tuple[float, float]], *, threshold: float = SCDET_THRESHOLD) -> list[float]:
    """Cuts from per-frame scores: the design's absolute threshold (score >= 8), plus quieter cuts that stand far
    above their neighbourhood (zoom jump cuts and flat graphics score 1.5-6 while motion around them scores < 0.5).

    A frame is a cut when score >= threshold, or when it is the largest score within +-0.25 s, is >= REL_MIN, and is
    >= REL_FACTOR x the 90th percentile of the other scores within +-1 s; or, between frozen frames (stills, the
    90th percentile <= 0.02), any local peak >= 0.4.
    """
    import numpy as np

    if not scores:
        return []
    ts = np.array([s[0] for s in scores])
    sc = np.array([s[1] for s in scores])
    picked = []
    for i, (t, s) in enumerate(zip(ts, sc)):
        if s >= threshold:
            picked.append(float(t))
            continue
        if s < FROZEN_MIN:
            continue
        near = (np.abs(ts - t) <= 0.25)
        if s < sc[near].max():
            continue
        win = (np.abs(ts - t) <= 1.0)
        win[i] = False
        others = sc[win]
        # the other half of a 2-frame transition must not count as "neighbourhood"
        others = others[np.abs(ts[win] - t) > 0.05] if len(others) else others
        ref = float(np.percentile(others, 90)) if len(others) else 0.0
        if s >= REL_MIN and s >= REL_FACTOR * max(ref, REL_FLOOR):
            picked.append(float(t))
        elif ref <= FROZEN_REF:      # slides/stills: any clear change between frozen frames is a cut
            picked.append(float(t))
    return picked


def detect_cuts(video: Path, fps: float | None = None, *, threshold: float = SCDET_THRESHOLD,
                timeout: float = 300) -> list[float]:
    """Cut times in seconds (the first frame of each new shot), rounded to 3 decimals.

    ffmpeg 6.x scdet stamps the score on the first frame of the new shot (measured on the generated fixtures), so
    no frame is subtracted (DESIGN-SAAS.md 6.3 said "subtract one frame"; see docs/wp-notes/WP3.md)."""
    if has_scdet():
        times = pick_cuts(frame_scores(video, timeout=timeout), threshold=threshold)
    else:
        vf = f"scale=270:-2,select='gt(scene,{FALLBACK_SCENE})',showinfo"
        p = subprocess.run([_need("ffmpeg"), "-hide_banner", "-nostats", "-i", str(video), "-an", "-vf", vf,
                            "-f", "null", "-"], capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW)
        times = [float(m) for m in _PTS_RE.findall(p.stderr)]
    return [round(t, 3) for t in _thin(times)]


def shots(duration: float, cuts: list[float]) -> list[tuple[float, float]]:
    """[(start, end)] for every shot."""
    edges = [0.0] + [c for c in cuts if 0 < c < duration] + [duration]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def shot_index(t: float, cuts: list[float]) -> int:
    return sum(1 for c in cuts if t >= c)
