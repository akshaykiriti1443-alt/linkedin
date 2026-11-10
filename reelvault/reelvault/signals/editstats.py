"""EditStats: the numeric edit-pattern fields, computed locally (DESIGN-SAAS.md 6.4.4, section 10.3 EP01/EP14).

    compute(duration, cuts, transcript, sound, frames_used) -> dict
"""
from __future__ import annotations


def length_bucket(d: float) -> str:
    """EP14: <15 | 15-30 | 30-60 | 60-90 | 90+."""
    if d < 15:
        return "<15"
    if d < 30:
        return "15-30"
    if d < 60:
        return "30-60"
    if d < 90:
        return "60-90"
    return "90+"


def cut_pace(cuts_per_10s: float) -> str:
    """EP01: slow < 1.5, medium 1.5-3.5, fast 3.5-6, hyper > 6 cuts per 10 s."""
    if cuts_per_10s < 1.5:
        return "slow"
    if cuts_per_10s <= 3.5:
        return "medium"
    if cuts_per_10s <= 6:
        return "fast"
    return "hyper"


def compute(duration: float, cuts: list[float], transcript: dict | None, sound: dict | None,
            frames_used: int) -> dict:
    d = max(0.01, float(duration or 0))
    cuts = [c for c in cuts if 0 < c < d]
    per10 = 10 * len(cuts) / d
    hook_len = min(3.0, d)
    hook_cuts = sum(1 for c in cuts if c < hook_len)
    tr = transcript or {}
    spans = tr.get("speech_spans") or []
    talk = sum(max(0.0, b - a) for a, b in spans)
    words = tr.get("words") or []
    if words:
        first3 = sum(1 for w in words if w[0] < 3.0)
    else:   # segments only: count words of segments that start in the first 3 s, pro rata
        first3 = 0
        for s in tr.get("segments") or []:
            if s["start"] < 3.0 and s["end"] > s["start"]:
                share = min(1.0, (3.0 - s["start"]) / (s["end"] - s["start"]))
                first3 += int(round(len(s["text"].split()) * share))
    silences = (sound or {}).get("silences") or []
    gaps = [s for s in silences if 0.3 <= s[1] - s[0] and s[0] > 0.2 and s[1] < d - 0.2]
    return {
        "duration": round(d, 2),
        "length_bucket": length_bucket(d),
        "cuts": len(cuts),
        "cuts_per_10s": round(per10, 1),
        "hook_cuts_per_10s": round(10 * hook_cuts / hook_len, 1) if hook_len > 0 else 0.0,
        "avg_shot_s": round(d / (len(cuts) + 1), 2),
        "cut_pace": cut_pace(per10),
        "speech_ratio": round(float(min(1.0, talk / d)), 2),
        "wpm": round(float(tr.get("wpm") or 0.0)),
        "words_first_3s": first3,
        "silence_gaps": len(gaps),
        "frames_used": frames_used,
    }
