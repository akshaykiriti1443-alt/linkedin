"""SoundMap: SFX aligned to cuts, music and speech coverage, ducking, summary numbers and SD-pattern hints
(DESIGN-SAAS.md 6.4 step 5 and the SoundMap JSON).

    build(raw, events, cuts, *, transcript, ig_audio) -> dict   # the SoundMap stored in reel_signals.sound

Speech coverage comes from the transcript's speech spans (whisper), not from the tagger. Music comes from the
tagger (score >= 0.35). The pattern hints are rules over these numbers (section 10.2 cues); the AI confirms or
overrides them.
"""
from __future__ import annotations

import numpy as np

from .sound import SFX_GROUPS, SoundRaw

VERSION = 1
SFX_LABELS = set(SFX_GROUPS) | {"hit", "riser", "whoosh"}
SFX_LABELS.discard("impact")
ACCENT_LABELS = {"ding", "pop", "cash_register", "camera_shutter", "typing", "click", "beep"}   # SD07
MEME_LABELS = {"record_scratch", "laughter", "crowd"}                                           # SD16
FOLEY_LABELS = {"typing", "click"}                                                              # SD11
CUT_BEFORE, CUT_AFTER = 0.25, 0.15


def anchor(e: dict) -> float:
    """The moment an SFX lands: onset for hits and accents, peak for a whoosh, end for a riser."""
    if e["label"] == "riser":
        return float(e["t_end"])
    if e["label"] == "whoosh":
        return float(e.get("t_peak", (e["t"] + e["t_end"]) / 2))
    return float(e["t"])


def align(events: list[dict], cuts: list[float]) -> dict[float, dict | None]:
    """{cut: the SFX event that sits on it, or None}. A cut has SFX when an anchor is in [cut - 0.25, cut + 0.15]
    or a whoosh/riser span covers it."""
    sfx = [e for e in events if e["label"] in SFX_LABELS]
    out: dict[float, dict | None] = {}
    for c in cuts:
        best = None
        for e in sfx:
            d = anchor(e) - c
            if -CUT_BEFORE <= d <= CUT_AFTER and (best is None or abs(d) < abs(anchor(best) - c)):
                best = e
        if best is None:
            best = next((e for e in sfx if e["label"] in ("whoosh", "riser") and e["t"] <= c <= e["t_end"]), None)
        out[c] = best
    return out


def _merge(spans: list[list[float]], gap: float = 0.0) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted([list(map(float, s)) for s in spans]):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _total(spans: list[list[float]]) -> float:
    return sum(max(0.0, b - a) for a, b in _merge(spans))


def _intersect(a_spans: list[list[float]], b_spans: list[list[float]]) -> list[list[float]]:
    out = []
    for a0, a1 in _merge(a_spans):
        for b0, b1 in _merge(b_spans):
            lo, hi = max(a0, b0), min(a1, b1)
            if hi > lo:
                out.append([lo, hi])
    return out


def _minus(a_spans: list[list[float]], b_spans: list[list[float]]) -> list[list[float]]:
    out = []
    bs = _merge(b_spans)
    for a0, a1 in _merge(a_spans):
        cur = a0
        for b0, b1 in bs:
            if b1 <= cur or b0 >= a1:
                continue
            if b0 > cur:
                out.append([cur, b0])
            cur = max(cur, b1)
        if cur < a1:
            out.append([cur, a1])
    return out


def music_levels(raw: SoundRaw, music: list[list[float]], speech: list[list[float]]) -> dict:
    """Music level under speech vs in the gaps (dBFS) and the ducking between them.

    Under speech the bed is estimated from the quietest 10 ms frames inside speech (the pauses between syllables,
    10th percentile); in the gaps it is the median level of music outside speech. Spans shorter than 0.5 s are
    ignored."""
    t, db = raw.feats.get("t"), raw.feats.get("db")
    res = {"under_speech_db": None, "in_gaps_db": None, "duck_db": None, "steady": None}
    if t is None or len(t) == 0 or not music:
        return res
    under = [s for s in _intersect(music, speech) if s[1] - s[0] >= 0.5]
    gaps = [s for s in _minus(music, speech) if s[1] - s[0] >= 0.5]

    def frames(spans):
        m = np.zeros(len(t), bool)
        for a, b in spans:
            m |= (t >= a) & (t < b)
        return db[m]

    fu, fg = frames(under), frames(gaps)
    if len(fu) >= 50:
        res["under_speech_db"] = round(float(np.percentile(fu, 10)), 1)
    if len(fg) >= 50:
        res["in_gaps_db"] = round(float(np.median(fg)), 1)
    if res["under_speech_db"] is not None and res["in_gaps_db"] is not None:
        res["duck_db"] = round(res["in_gaps_db"] - res["under_speech_db"], 1)
    # steady: the bed's level per 0.5 s (10th percentile inside each window) moves within +-3 dB
    lv = []
    for a, b in (under or gaps):
        x = a
        while x + 0.5 <= b:
            seg = db[(t >= x) & (t < x + 0.5)]
            if len(seg):
                lv.append(np.percentile(seg, 10 if under else 50))
            x += 0.5
    if len(lv) >= 4:
        spread = float(np.percentile(lv, 90) - np.percentile(lv, 10))
        res["steady"] = bool(spread <= 6.0 and (res["duck_db"] is None or res["duck_db"] < 3))
    return res


def tts_likely(raw: SoundRaw, speech: list[list[float]]) -> bool:
    """SD14: speech-synthesizer score >= 0.3 in >= 30% of the windows that overlap speech."""
    s = raw.groups.get("speech_synth")
    if s is None or not len(s) or not speech:
        return False
    idx = [i for i, t0 in enumerate(raw.t0) if any(a < t0 + 1.0 and b > t0 for a, b in speech)]
    if not idx:
        return False
    return bool((s[idx] >= 0.3).mean() >= 0.3)


def pattern_hints(m: dict) -> list[str]:
    """Rules from the section 10.2 cues. Order = how sure the rule is (strongest evidence first)."""
    s = m["summary"]
    speech = m["speech_coverage_pct"] or 0
    music_cov = (m["music"] or {}).get("coverage_pct") or 0
    music_under = (m["music"] or {}).get("under_speech_pct") or 0
    duck = (m["music"] or {}).get("duck_db")
    ig = m.get("ig_audio") or {}
    licensed = ig.get("type") == "licensed" or bool(ig.get("is_reused"))
    counts = s["sfx_counts"]
    sfx10 = s["sfx_per_10s"]
    hints = []
    if s["cuts_with_sfx_pct"] is not None and s["cuts_with_sfx_pct"] >= 40 and sfx10 >= 2:
        hints.append("SD06")
    if speech >= 70 and music_cov < 20 and sfx10 <= 1:
        hints.append("SD01")
    if speech >= 30 and duck is not None and duck >= 4:
        hints.append("SD03")
    elif speech >= 30 and music_under >= 60 and (duck is None or duck < 3):
        hints.append("SD02")
    if speech < 15 and (music_cov >= 70 or licensed):
        hints.append("SD04")
    if licensed and speech >= 50:
        hints.append("SD05")
    if s.get("accents_off_cut", 0) >= 2:
        hints.append("SD07")
    if s.get("riser_into_hit"):
        hints.append("SD08")
    if s.get("silence_beat"):
        hints.append("SD09")
    if sum(counts.get(k, 0) for k in FOLEY_LABELS) >= 3 and speech < 30:
        hints.append("SD11")
    if m.get("ambient_pct", 0) >= 30 and music_cov < 20 and sfx10 <= 1:
        hints.append("SD12")
    if m.get("tts_likely"):
        hints.append("SD14")
    if (s["first_sfx_s"] is not None and s["first_sfx_s"] <= 1.5) or s.get("music_starts_s") is not None:
        hints.append("SD15")
    if s.get("meme_events", sum(counts.get(k, 0) for k in MEME_LABELS)) >= 1:
        hints.append("SD16")
    return hints


def build(raw: SoundRaw, events: list[dict], cuts: list[float], *, duration: float, transcript: dict | None = None,
          ig_audio: dict | None = None) -> dict:
    """The SoundMap JSON (6.4)."""
    dur = max(0.01, float(duration or raw.duration or 0.01))
    speech = [list(map(float, s)) for s in (transcript or {}).get("speech_spans") or []]
    aligned = align(events, cuts)
    sfx = [e for e in events if e["label"] in SFX_LABELS]
    out_events = []
    on_cut_ids = {}
    for c, e in aligned.items():
        if e is not None:
            on_cut_ids.setdefault(id(e), c)
    for e in events:
        ev = {k: e[k] for k in ("t", "t_end", "label", "source", "confidence", "score")}
        if "t_peak" in e:
            ev["t_peak"] = e["t_peak"]
        ev["on_cut"] = on_cut_ids.get(id(e))
        out_events.append(ev)
    music = raw.music_spans()
    music_cov = _total(music) / dur
    speech_cov = _total(speech) / dur
    levels = music_levels(raw, music, speech)
    under_pct = (_total(_intersect(music, speech)) / _total(speech) * 100) if speech else None
    silences = [[round(d["t"], 2), round(d["t_end"], 2)] for d in raw.dsp if d["label"] == "silence"]
    counts: dict[str, int] = {}
    for e in sfx:
        counts[e["label"]] = counts.get(e["label"], 0) + 1
    # laughter / crowd are span groups, not SFX: count them from all events for the SD16 meme rule
    meme = sum(1 for e in events if e["label"] in MEME_LABELS)
    first = min((anchor(e) for e in sfx), default=None)
    accents_off = sum(1 for e in sfx if e["label"] in ACCENT_LABELS and on_cut_ids.get(id(e)) is None)
    risers = [e for e in sfx if e["label"] == "riser"]
    lands = [e["t"] for e in sfx if e["label"] in ("hit", "whoosh", "pop", "sound_effect") and e["label"] != "riser"]
    riser_into_hit = any(any(0 <= x - r["t_end"] <= 0.3 for x in lands + list(cuts)) or
                         any(abs(c - r["t_end"]) <= 0.3 for c in cuts) for r in risers)
    # A silence is a "beat" (SD09) only with a reason: a hit lands right after it, or the music drops out for it
    # (music on both sides). Plain pauses between sentences of a voice-only reel are not one (fix round 1).
    lands_after = [anchor(e) for e in sfx] + [a for a, _ in music]

    def _is_beat(a: float, b: float) -> bool:
        if not (0.3 <= b - a <= 1.5 and a >= 1.0 and b <= dur - 1.0):
            return False
        if any(b - 0.1 <= x <= b + 0.3 for x in lands_after):
            return True
        return any(ma <= a - 0.2 for ma, mb in music if mb >= a - 0.05) and \
            any(ma <= b + 0.05 and mb >= b + 0.2 for ma, mb in music)

    silence_beat = any(_is_beat(a, b) for a, b in silences)
    music_starts = next((a for a, b in music if 0.25 < a <= 1.5), None)
    amb = raw.groups.get("ambient")
    crowd = raw.groups.get("crowd")
    ambient_pct = 0
    if amb is not None and len(amb):
        ambient_pct = round(100 * float((np.maximum(amb, crowd if crowd is not None else 0) >= 0.3).mean()))
    m = {
        "version": VERSION,
        "detector": raw.detector,
        "duration": round(dur, 2),
        "lufs": raw.lufs,
        "events": out_events,
        "music": {"coverage_pct": round(100 * music_cov), "spans": music,
                  "under_speech_pct": None if under_pct is None else round(under_pct),
                  **levels} if raw.groups else None,
        "speech_coverage_pct": round(100 * speech_cov),
        "tts_likely": tts_likely(raw, speech),
        "silences": silences,
        "loudness": raw.loudness,
        "ambient_pct": ambient_pct,
        "summary": {
            "sfx_per_10s": round(10 * len(sfx) / dur, 2),
            "sfx_counts": counts,
            "cuts_with_sfx_pct": round(100 * sum(1 for v in aligned.values() if v is not None) / len(cuts))
            if cuts else None,
            "first_sfx_s": None if first is None else round(first, 2),
            "hook_hit": first is not None and first <= 1.5,
            "accents_off_cut": accents_off,
            "riser_into_hit": riser_into_hit,
            "silence_beat": silence_beat,
            "meme_events": meme,
            "music_starts_s": music_starts,
            "cuts_on_beat_pct": None,
            "beat_bpm": None,
        },
        "ig_audio": _ig_audio(ig_audio),
    }
    m["pattern_hints"] = pattern_hints(m)
    return m


def _ig_audio(a: dict | None) -> dict | None:
    if not a:
        return None
    return {"type": a.get("type") or "unknown", "title": a.get("title"), "artist": a.get("artist"),
            "is_reused": bool(a.get("is_reused"))}


def empty(duration: float, *, has_audio: bool, ig_audio: dict | None = None) -> dict:
    """SoundMap for a reel with no audio track (or a silent one)."""
    return {"version": VERSION, "detector": "none", "duration": round(float(duration or 0), 2), "lufs": None,
            "events": [], "music": None, "speech_coverage_pct": 0, "tts_likely": False,
            "silences": [[0.0, round(float(duration or 0), 2)]] if has_audio and duration else [],
            "loudness": [], "ambient_pct": 0,
            "summary": {"sfx_per_10s": 0.0, "sfx_counts": {}, "cuts_with_sfx_pct": None, "first_sfx_s": None,
                        "hook_hit": False, "accents_off_cut": 0, "riser_into_hit": False, "silence_beat": False,
                        "music_starts_s": None, "cuts_on_beat_pct": None, "beat_bpm": None},
            "ig_audio": _ig_audio(ig_audio), "pattern_hints": [], "no_audio": not has_audio}
