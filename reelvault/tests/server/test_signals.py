"""WP3: media, sound and visual signals (DESIGN-SAAS.md 6.1-6.5.1, 12.1 test_signals.py).

The clips come from tests/fixtures/make_media.py (synthetic, made from code; cached in tests/fixtures/media).
Model-dependent checks (CED-tiny sound labels, YuNet faces, whisper) are skipped when the models are absent.
Point the tests at a model folder with RV_TEST_MODELS_DIR (same layout as /opt/reelvault/models,
deploy/fetch_models.sh); without it they look at RV_MODELS_DIR, then /opt/reelvault/models.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import make_media  # noqa: E402

from reelvault import media  # noqa: E402
from reelvault import signals  # noqa: E402
from reelvault.signals import cuts as cuts_mod  # noqa: E402
from reelvault.signals import editstats, frames, sound, soundmap, visual  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


def _models_dir() -> Path | None:
    for cand in (os.environ.get("RV_TEST_MODELS_DIR"), os.environ.get("RV_MODELS_DIR"), "/opt/reelvault/models"):
        if cand and Path(cand).is_dir():
            return Path(cand)
    return None


MODELS = _models_dir()
HAS_CED = bool(MODELS and (MODELS / "ced-tiny" / "model.int8.onnx").is_file())
HAS_YUNET = bool(MODELS and (MODELS / "yunet" / "face_detection_yunet_2023mar.onnx").is_file())
HAS_WHISPER = bool(MODELS and media.whisper_path("base", MODELS) != "base")
needs_ced = pytest.mark.skipif(not HAS_CED, reason="CED-tiny model not found (set RV_TEST_MODELS_DIR)")
needs_yunet = pytest.mark.skipif(not HAS_YUNET, reason="YuNet model not found (set RV_TEST_MODELS_DIR)")


# ============================================================================================== fixtures

@pytest.fixture(scope="session")
def clips() -> dict[str, Path]:
    return make_media.ensure(ROOT / "tests" / "fixtures" / "media")


@pytest.fixture(scope="session")
def processed(clips, tmp_path_factory) -> dict[str, tuple[signals.Signals, Path]]:
    """Every clip through the full pipeline once, with whatever models exist (shared by many tests)."""
    old = os.environ.get("RV_MODELS_DIR")
    if MODELS:
        os.environ["RV_MODELS_DIR"] = str(MODELS)
    else:
        os.environ["RV_MODELS_DIR"] = str(tmp_path_factory.mktemp("no_models"))
    base = tmp_path_factory.mktemp("reels")
    out = {}

    def fake_asr(audio, model, words):     # whisper only for the speech clip, and only when present
        return dict(media.EMPTY_TRANSCRIPT)

    try:
        for name, path in clips.items():
            d = base / name
            asr = None if (name.startswith("speech") and HAS_WHISPER) else fake_asr
            out[name] = (signals.process(path, d, ig_audio={"type": "original"}, words=name.startswith("speech"),
                                         whisper_model="base", transcribe=asr), d)
    finally:
        if old is None:
            os.environ.pop("RV_MODELS_DIR", None)
        else:
            os.environ["RV_MODELS_DIR"] = old
    return out


def truth(clips, name) -> dict:
    return make_media.truth(clips[name])


# ============================================================================================== cuts (6.3)

@pytest.mark.parametrize("name", ["sfx_cuts", "talking_head", "screen", "split", "greenscreen", "slideshow",
                                  "noaudio"])
def test_cuts_within_50ms(clips, name):
    t = truth(clips, name)
    info = signals.probe(clips[name])
    got = cuts_mod.detect_cuts(clips[name], info.fps)
    assert len(got) == len(t["cuts"]), (got, t["cuts"])
    for want, have in zip(t["cuts"], got):
        assert abs(have - want) <= 0.05, (want, have)


def test_same_background_jump_cut_found(clips):
    got = cuts_mod.detect_cuts(clips["sfx_cuts"], 25)
    assert any(abs(c - truth(clips, "sfx_cuts")["jump_cut"]) <= 0.05 for c in got)


def test_pick_cuts_rules():
    # flat motion around a 2.3 score (zoom jump cut) -> cut; the same score inside busy motion -> no cut
    quiet = [(i / 25, 0.05) for i in range(100)]
    quiet[50] = (2.0, 2.3)
    assert cuts_mod.pick_cuts(quiet) == [2.0]
    busy = [(i / 25, 1.5 if i % 2 else 0.9) for i in range(100)]
    busy[50] = (2.0, 2.3)
    assert cuts_mod.pick_cuts(busy) == []
    # the design threshold always wins; two cuts 0.2 s apart keep the first
    hard = [(i / 25, 0.0) for i in range(100)]
    hard[30] = (1.2, 9.0)
    hard[35] = (1.4, 9.0)
    assert cuts_mod._thin(cuts_mod.pick_cuts(hard)) == [1.2]
    # stills: a small change between frozen frames is a slide change
    still = [(i / 25, 0.0) for i in range(100)]
    still[60] = (2.4, 0.53)
    assert cuts_mod.pick_cuts(still) == [2.4]


# ============================================================================================== frames (6.1)

def test_cut_aware_frame_times_many_cuts():
    cuts = [1.5, 4.0, 6.0, 9.0, 12.0, 15.0, 18.0, 21.0, 24.0, 27.0]
    fr = media.cut_frame_times(30.0, cuts, 12)
    assert [f["t"] for f in fr if f["kind"] == "hook"] == [0.2, 1.0, 2.0, 3.0]
    cut_ts = [f["t"] for f in fr if f["kind"] == "cut"]
    assert len(cut_ts) == 7 and all(round(t - 0.2, 2) in cuts for t in cut_ts)
    assert cut_ts[0] == 4.2 and cut_ts[-1] == 27.2              # spread evenly by index, ends kept
    assert fr[-1] == {"n": 11, "t": 29.5, "shot": 10, "kind": "end"}
    assert not [f for f in fr if f["kind"] == "body"]
    assert [f["n"] for f in fr] == list(range(12))
    for f in fr:
        assert f["shot"] == sum(1 for c in cuts if f["t"] >= c)


def test_cut_aware_frame_times_few_cuts_fill_body():
    fr = media.cut_frame_times(20.0, [3.1, 10.0], 12)
    kinds = [f["kind"] for f in fr]
    assert kinds.count("cut") == 1 and kinds.count("body") == 6 and kinds[-1] == "end"
    ts = [f["t"] for f in fr]
    assert ts == sorted(ts) and len(fr) == 12
    assert all(b - a >= 0.4 for a, b in zip(ts, ts[1:]))
    assert all(3.5 <= f["t"] <= 19.4 for f in fr if f["kind"] == "body")


def test_cut_aware_frame_times_short_clip():
    fr = media.cut_frame_times(1.6, [], 12)
    assert [(f["t"], f["kind"]) for f in fr] == [(0.2, "hook"), (1.0, "hook"), (1.1, "end")]


def test_masters_and_index(processed):
    sig, d = processed["sfx_cuts"]
    assert len(sig.frames) == 12
    for f in sig.frames:
        p = d / f["path"]
        assert p.exists() and p.name == f"f{f['n']:02d}_{f['t']:06.2f}.jpg"
    import cv2

    img = cv2.imread(str(d / sig.frames[0]["path"]))
    assert img.shape[1] == 768
    idx = json.loads((d / "frames" / "index.json").read_text())
    assert [f["t"] for f in idx["frames"]] == [f["t"] for f in sig.frames]


def test_key_frames_pick_rules():
    fr = media.cut_frame_times(30.0, [4, 8, 12, 16, 20, 24], 12)
    vis = {"per_frame": [{"n": f["n"], "face": (0.2 if f["t"] == 16.2 else 0.05), "text": 0} for f in fr]}
    ks = frames.key_frames(fr, vis)
    ts = [f["t"] for f in ks]
    assert len(ks) == 6 and ts == sorted(ts)
    assert 0.2 in ts and 1.0 in ts and 29.5 in ts       # hook pair and end
    assert 16.2 in ts                                    # the biggest face in the body
    assert 8.2 in ts or 12.2 in ts                       # cut nearest 1/3 (10 s)
    assert 20.2 in ts                                    # cut nearest 2/3 (20 s)


# ============================================================================================== image budget (6.1.1)

def test_every_destination_within_its_cap(processed):
    """Measured on every clip: sheets and single frames for paid_api / small / mcp, plus the MCP deep sheet."""
    sizes = {}
    for name, (sig, d) in processed.items():
        for dest in frames.DESTS:
            b = frames.sheet_for(d, dest)
            assert 0 < len(b) <= frames.SHEETS[dest].cap, (name, dest, len(b))
            assert b[:3] == b"\xff\xd8\xff"
            sizes[(name, "sheet", dest)] = len(b)
            for f in sig.frames[:3]:
                j = frames.jpeg_for(d / f["path"], dest)
                assert 0 < len(j) <= frames.FRAMES[dest].cap, (name, dest, len(j))
        deep = frames.sheet_for(d, "mcp", cap_bytes=frames.MCP_DEEP_SHEET_CAP)
        assert len(deep) <= frames.MCP_DEEP_SHEET_CAP
    print("sheet bytes:", {f"{k[0]}/{k[2]}": v for k, v in sizes.items()})


def test_sheet_sizes_and_tiles(processed):
    import cv2
    import numpy as np

    sig, d = processed["screen"]
    expect = {"paid_api": (768, 1023), "small": (768, 912), "mcp": (480, 568)}
    for dest, (w, h) in expect.items():
        img = cv2.imdecode(np.frombuffer(frames.sheet_for(d, dest), np.uint8), cv2.IMREAD_COLOR)
        assert abs(img.shape[1] - w) <= 2 and abs(img.shape[0] - h) <= 4, (dest, img.shape)
    tiles = frames.sheet_tiles(d, "paid_api")
    assert [t["tile"] for t in tiles] == list(range(1, 13)) and tiles[0]["t"] == 0.2
    small = frames.sheet_tiles(d, "small")
    assert len(small) == 6 and small[0]["t"] == 0.2 and small[1]["t"] == 1.0 and small[-1]["kind"] == "end"


def test_step_down_never_returns_nothing(processed, tmp_path):
    sig, d = processed["screen"]
    shutil.copytree(d, tmp_path / "r")
    tight = frames.sheet_for(tmp_path / "r", "paid_api", cap_bytes=20 * 1024, refresh=True)
    assert 0 < len(tight) <= 20 * 1024                   # quality, then tile size, then fewer tiles
    tiny = frames.jpeg_for(tmp_path / "r" / sig.frames[5]["path"], "mcp", cap_bytes=3000)
    assert 0 < len(tiny) <= 3000
    impossible = frames.jpeg_for(tmp_path / "r" / sig.frames[5]["path"], "mcp", cap_bytes=50)
    assert len(impossible) > 0                           # smallest attempt, logged, never empty


def test_missing_master_leaves_black_tile(processed, tmp_path):
    sig, d = processed["talking_head"]
    r = tmp_path / "r"
    shutil.copytree(d, r)
    (r / sig.frames[4]["path"]).unlink()
    b = frames.sheet_for(r, "paid_api", refresh=True)
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_GRAYSCALE)
    th, tw = img.shape[0] // 3, img.shape[1] // 4
    tile5 = img[th:2 * th, 0:tw]                         # tile 5 = row 2, col 1
    assert tile5.mean() < 8 and img[0:th, 0:tw].mean() > 30


def _tag_text(sig: signals.Signals, caption: str) -> str:
    """The per-reel text an MCP tag batch carries (6.5): numbers + untrusted caption/transcript, <= 3,500 chars."""
    v = {k: x for k, x in sig.visual.items() if k not in ("per_frame", "sheet_bytes")}
    s = sig.sound
    body = {"reel_id": "reel_xxxxxxxxxxxxxxxx", "visual": v, "edit": sig.edit,
            "sound": {"summary": s["summary"], "pattern_hints": s["pattern_hints"], "ig_audio": s.get("ig_audio"),
                      "music": {k: x for k, x in (s.get("music") or {}).items() if k != "spans"},
                      "speech_coverage_pct": s["speech_coverage_pct"]},
            "caption": caption[:600], "transcript": (sig.transcript.get("text") or "")[:1200]}
    return json.dumps(body, separators=(",", ":"))


def test_mcp_tag_batch_and_deep_payload_under_90k(processed):
    caption = "Nobody tells you this about ads. " * 40
    worst = sorted(processed.values(), key=lambda p: -len(frames.sheet_for(p[1], "mcp")))
    # work.mcp_tag_batch (WP5) adds reels while 3,000 + sum(b64(sheet) + text) < 90,000; max 3, at least 1
    total, batch = 3000, 0
    for sig, d in worst[:3]:
        text = _tag_text(sig, caption)
        assert len(text) <= 3500, len(text)
        add = len(base64.b64encode(frames.sheet_for(d, "mcp"))) + len(text)
        if total + add >= 90000:
            break
        total, batch = total + add, batch + 1
    assert batch >= 2 and total < 90000, (batch, total)       # even the two heaviest clips fit together
    # deep: 1 mcp sheet (28 KB cap) + 2 frames (12 KB cap) + <= 18,000 chars of text
    sig, d = worst[0]
    deep = len(base64.b64encode(frames.sheet_for(d, "mcp", cap_bytes=frames.MCP_DEEP_SHEET_CAP)))
    pics = frames.deep_frames(d, "mcp", 2)
    assert len(pics) == 2 and pics[0][0]["t"] == 1.0
    deep += sum(len(base64.b64encode(b)) for _, b in pics)
    text = json.dumps({"signals": sig.to_row(), "caption": caption[:2000]})[:18000]
    assert deep + len(text) < 90000, deep + len(text)
    print("mcp tag batch:", batch, "reels,", total, "chars; deep chars:", deep + len(text))


def test_deep_frames_counts(processed):
    sig, d = processed["talking_head"]
    got = frames.deep_frames(d, "paid_api", 8)
    assert len(got) == 8 and all(len(b) <= frames.FRAMES["paid_api"].cap for _, b in got)
    assert [f["t"] for f, _ in got] == sorted(f["t"] for f, _ in got)


# ============================================================================================== sound (6.4)

def _near(spans, a, b, tol):
    return any(abs(x - a) <= tol and abs(y - b) <= tol for x, y in spans)


def test_dsp_silence_riser_whoosh(clips):
    raw = sound.analyze(clips["sfx_cuts"], use_model=False)
    t = truth(clips, "sfx_cuts")
    sil = [[d["t"], d["t_end"]] for d in raw.dsp if d["label"] == "silence"]
    assert _near(sil, *t["silence"], 0.1), sil
    ris = [[d["t"], d["t_end"]] for d in raw.dsp if d["label"] == "riser"]
    assert len(ris) == 1 and _near(ris, *t["riser"], 0.1), ris
    wh = [d for d in raw.dsp if d["label"] == "whoosh"]
    peaks = [e["peak"] for e in t["events"] if e["label"] == "whoosh"]
    assert len(wh) == len(peaks)
    for p in peaks:
        assert any(w["t"] <= p <= w["t_end"] and -0.1 <= w["t_peak"] - p <= 0.15 for w in wh), (p, wh)


def test_hits_found_and_music_drums_dropped(clips):
    raw = sound.analyze(clips["sfx_cuts"], use_model=False)
    ev = sound.fuse(raw)
    hits = [e["t"] for e in ev if e["label"] == "hit"]
    for want in (4.6, 9.0, 18.0):
        assert any(abs(h - want) <= 0.05 for h in hits), (want, hits)
    # the slideshow is music only: its drum and bar transients are not SFX
    raw2 = sound.analyze(clips["slideshow"], use_model=False)
    assert [e for e in sound.fuse(raw2) if e["label"] == "hit"] == []


def test_sfx_on_cut_alignment(processed, clips):
    sig, _ = processed["sfx_cuts"]
    t = truth(clips, "sfx_cuts")
    on = {round(c, 1) for c in t["cuts_with_sfx"]}
    aligned = soundmap.align(sig.sound["events"], sig.cuts)
    found = {round(c, 1) for c, e in aligned.items() if e is not None}
    assert len(on & found) >= len(on) - 1, (on, found)        # at most one miss
    assert 15.2 not in found                                     # the bare cut stays bare
    assert sig.sound["summary"]["cuts_with_sfx_pct"] >= 75
    ev_on_cut = [e for e in sig.sound["events"] if e["on_cut"] is not None]
    assert ev_on_cut and all(e["on_cut"] in sig.cuts for e in ev_on_cut)


def test_anchor_rules():
    assert soundmap.anchor({"label": "riser", "t": 9.6, "t_end": 11.6}) == 11.6
    assert soundmap.anchor({"label": "whoosh", "t": 2.2, "t_end": 2.8, "t_peak": 2.55}) == 2.55
    assert soundmap.anchor({"label": "hit", "t": 4.6, "t_end": 4.65}) == 4.6
    ev = [{"label": "whoosh", "t": 2.0, "t_end": 3.0, "t_peak": 2.9}, {"label": "hit", "t": 5.3, "t_end": 5.35}]
    al = soundmap.align(ev, [2.4, 5.0, 5.5])
    assert al[2.4]["label"] == "whoosh"                       # covered by the whoosh span
    assert al[5.0] is None                                     # hit 0.3 s after: outside [-0.25, +0.15]
    assert al[5.5]["label"] == "hit"                           # hit 0.2 s before: inside


def test_soundmap_shape_and_hints(processed):
    sig, _ = processed["sfx_cuts"]
    m = sig.sound
    for k in ("version", "detector", "duration", "lufs", "events", "music", "speech_coverage_pct", "tts_likely",
              "silences", "summary", "ig_audio", "pattern_hints"):
        assert k in m
    assert m["ig_audio"] == {"type": "original", "title": None, "artist": None, "is_reused": False}
    s = m["summary"]
    assert s["sfx_per_10s"] >= 2 and s["riser_into_hit"] and s["silence_beat"]
    assert {"SD06", "SD08", "SD09"} <= set(m["pattern_hints"])
    assert m["lufs"] is not None and -30 < m["lufs"] < -10
    for e in m["events"]:
        assert e["confidence"] in ("high", "low") and e["source"] in ("dsp", "ml", "ml+dsp")


def test_pattern_hint_rules():
    base = {"summary": {"sfx_per_10s": 0.5, "sfx_counts": {}, "cuts_with_sfx_pct": 0, "first_sfx_s": None},
            "speech_coverage_pct": 85, "music": {"coverage_pct": 5, "duck_db": None}, "ig_audio": None}
    assert soundmap.pattern_hints(base) == ["SD01"]
    ducked = dict(base, music={"coverage_pct": 80, "under_speech_pct": 70, "duck_db": 6.5})
    assert "SD03" in soundmap.pattern_hints(ducked) and "SD02" not in soundmap.pattern_hints(ducked)
    steady = dict(base, music={"coverage_pct": 80, "under_speech_pct": 70, "duck_db": 1.0})
    assert "SD02" in soundmap.pattern_hints(steady)
    licensed = dict(base, speech_coverage_pct=5, ig_audio={"type": "licensed"})
    assert "SD04" in soundmap.pattern_hints(licensed)
    vo = dict(base, ig_audio={"type": "licensed"})
    assert "SD05" in soundmap.pattern_hints(vo)
    hook = dict(base, summary=dict(base["summary"], first_sfx_s=0.4))
    assert "SD15" in soundmap.pattern_hints(hook)


@needs_ced
def test_ced_labels_music_beep_ding(processed):
    sig, _ = processed["sfx_cuts"]
    m = sig.sound
    assert m["detector"] == "ced-tiny-int8+dsp"
    assert m["music"]["coverage_pct"] >= 80
    labels = {(e["label"], round(e["t"])) for e in m["events"]}
    assert ("beep", 8) in labels                         # truth 7.9 s
    assert ("ding", 14) in labels                        # truth 14.2 s
    assert {"SD07"} <= set(m["pattern_hints"])           # two accents off the cuts


@needs_ced
def test_asr_skipped_when_no_speech(processed):
    for name in ("sfx_cuts", "talking_head", "screen", "slideshow"):
        sig, _ = processed[name]
        assert sig.skipped_asr and sig.transcript["skipped"] and sig.transcript["text"] == ""
        assert sig.transcript["model"] is None


def test_no_audio_track(processed):
    sig, _ = processed["noaudio"]
    assert sig.has_audio is False and sig.skipped_asr
    assert sig.sound["detector"] == "none" and sig.sound["no_audio"] is True
    assert sig.sound["events"] == [] and sig.sound["pattern_hints"] == []


def test_speech_gate_rule():
    import numpy as np

    raw = sound.SoundRaw(duration=10, has_audio=True, detector="x", t0=np.arange(37) * 0.25,
                         groups={"speech": np.r_[np.full(4, 0.5), np.zeros(33)]})
    assert raw.speech_likely() is True                   # 4/37 = 11% >= 10%
    raw.groups["speech"] = np.r_[np.full(3, 0.5), np.zeros(34)]
    assert raw.speech_likely() is False                  # 8%
    assert sound.SoundRaw(duration=10, has_audio=True, detector="dsp").speech_likely() is None
    assert sound.SoundRaw(duration=10, has_audio=False, detector="none").speech_likely() is False


@pytest.mark.skipif(not (HAS_CED and HAS_WHISPER), reason="needs CED-tiny and whisper base")
def test_transcript_with_words(processed):
    if "speech" not in processed:
        pytest.skip("no speech source for the speech clip (RV_FIXTURE_SPEECH or network for gTTS)")
    sig, _ = processed["speech"]
    tr = sig.transcript
    assert not sig.skipped_asr and tr["model"] == "base"
    assert "audience" in tr["text"].lower() and "hook" in tr["text"].lower()
    assert tr["words"] and all(len(w) == 3 and w[1] >= w[0] for w in tr["words"])
    assert 120 <= tr["wpm"] <= 260
    assert tr["speech_spans"][0][0] == pytest.approx(1.0, abs=0.5)
    assert sig.sound["speech_coverage_pct"] >= 50
    assert sig.edit["words_first_3s"] >= 3 and sig.edit["speech_ratio"] >= 0.5


# ============================================================================================== edit stats (6.4.4)

def test_edit_stats_numbers(processed):
    sig, _ = processed["sfx_cuts"]
    e = sig.edit
    assert e["cuts"] == 9 and e["cuts_per_10s"] == 3.8 and e["cut_pace"] == "fast"
    assert e["hook_cuts_per_10s"] == 3.3                 # one cut (2.5 s) in the first 3 s
    assert e["avg_shot_s"] == 2.4 and e["length_bucket"] == "15-30" and e["frames_used"] == 12
    assert e["silence_gaps"] == 1 and e["speech_ratio"] == 0.0


def test_edit_stats_buckets_and_words():
    assert [editstats.length_bucket(x) for x in (9, 15, 45, 75, 120)] == ["<15", "15-30", "30-60", "60-90", "90+"]
    assert [editstats.cut_pace(x) for x in (1.0, 2.0, 5.0, 7.0)] == ["slow", "medium", "fast", "hyper"]
    tr = {"words": [[0.1, 0.3, "Stop"], [0.4, 0.6, "posting"], [2.9, 3.1, "daily"], [3.2, 3.4, "now"]],
          "speech_spans": [[0.1, 3.4]], "wpm": 168.0}
    e = editstats.compute(34.2, [1.0, 2.0] + [3 + i * 1.5 for i in range(19)], tr, {"silences": [[17.2, 17.9]]}, 12)
    assert e["cuts"] == 21 and e["cuts_per_10s"] == 6.1 and e["cut_pace"] == "hyper"
    assert e["hook_cuts_per_10s"] == 6.7 and e["avg_shot_s"] == 1.55 and e["words_first_3s"] == 3
    assert e["wpm"] == 168 and e["silence_gaps"] == 1 and e["length_bucket"] == "30-60"


# ============================================================================================== visual stats (6.5.1)

def test_ui_clip_is_screen_recording(processed):
    v = processed["screen"][0].visual
    assert v["ui_frames_share"] >= 0.5 and v["ui_score"] >= 0.6
    assert v["small_text_boxes_med"] >= 6
    assert v["local_vf"][0]["id"] == "VF05"
    assert v["local_recipe"]["headline_box"] is False    # the app's own top bar is not a headline box


def test_split_clip_has_two_panes(processed):
    v = processed["split"][0].visual
    assert v["split_share"] >= 0.5 and v["panes_med"] == 2
    assert v["local_vf"][0]["id"] == "VF06"


def test_slideshow_is_static(processed):
    v = processed["slideshow"][0].visual
    assert v["static_frame_share"] >= 0.6 and v["motion_med"] < 0.002
    if HAS_YUNET:
        assert v["local_vf"][0]["id"] == "VF09"


def test_non_split_clips_are_not_split(processed):
    for name in ("talking_head", "screen", "greenscreen", "slideshow", "sfx_cuts"):
        assert processed[name][0].visual["split_share"] < 0.2, name
    for name in ("talking_head", "split", "greenscreen", "slideshow"):
        assert processed[name][0].visual["ui_frames_share"] < 0.2, name


@needs_yunet
def test_talking_head_faces_headline_captions(processed):
    v = processed["talking_head"][0].visual
    assert v["face_detector"] == "yunet"
    assert v["face_share"] >= 0.7 and v["faces_max"] == 1 and v["face_center_share"] >= 0.7
    assert 0.05 <= v["face_area_med"] <= 0.2
    assert v["top_text_box_share"] >= 0.5 and v["local_recipe"]["headline_box"] is True
    assert v["caption_band"] == "bottom" and v["text_frames_share"] >= 0.8
    assert v["local_vf"][0]["id"] == "VF01" and v["cutout_share"] < 0.1


@needs_yunet
def test_face_bubble_on_screen_recording(processed):
    v = processed["screen"][0].visual
    assert v["small_face_corner_share"] >= 0.5 and v["local_recipe"]["face_bubble"] is True
    assert all(x["id"] != "VF01" for x in v["local_vf"])


@needs_yunet
def test_greenscreen_cutout_over_still(processed):
    v = processed["greenscreen"][0].visual
    assert v["cutout_share"] >= 0.4
    assert v["local_vf"][0]["id"] == "VF04"
    assert processed["split"][0].visual["cutout_share"] < 0.1


def test_visual_without_face_model(processed, tmp_path, monkeypatch, clips):
    monkeypatch.setenv("RV_MODELS_DIR", str(tmp_path / "none"))
    sig, d = processed["screen"]
    v = visual.compute(clips["screen"], sig.frames, reel_dir=d, duration=sig.duration, cuts=sig.cuts,
                       speech_ratio=0.0, width=sig.width, height=sig.height)
    assert v["face_detector"] == "none" and v["face_share"] is None and v["cutout_share"] is None
    assert v["local_vf"][0]["id"] == "VF05"              # UI rule needs no faces


def test_local_rules_from_taxonomy_json():
    from reelvault import taxonomy

    data = json.loads(taxonomy.PATH.read_text())
    assert data["local_rules"] == visual.rules_json()    # the WP3 block mirrors the code defaults
    fmts, rec = visual.local_rules()
    assert [r["id"] for r in fmts][:3] == ["VF05", "VF06", "VF04"]
    assert rec["headline_box"] == ["top_text_box_share", ">=", 0.5]
    stats = {"ui_frames_share": 0.7, "split_share": 0.6, "face_share": None}
    assert [g["id"] for g in visual.guess_format(stats, fmts)] == ["VF05", "VF06"]
    assert visual.guess_format({"face_share": None, "speech_ratio": 0.9}, fmts) == []


def test_visual_json_shape(processed):
    v = processed["talking_head"][0].visual
    for k in ("version", "face_share", "face_area_med", "faces_max", "small_face_corner_share", "text_area_med",
              "text_frames_share", "top_text_box_share", "caption_band", "small_text_boxes_med", "ui_score",
              "ui_frames_share", "cutout_share", "split_share", "panes_med", "motion_med", "motion_p90",
              "colorfulness", "static_frame_share", "local_vf", "local_recipe"):
        assert k in v, k
    json.dumps(v)
    assert len(v["per_frame"]) == len(processed["talking_head"][0].frames)


# ============================================================================================== process() limits (6.1)

def test_too_long_video_rejected(tmp_path):
    long = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x112:r=1:d=301",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(long)], check=True)
    with pytest.raises(signals.VideoRejected) as e:
        signals.process(long, tmp_path / "r", words=False, whisper_model="base")
    assert e.value.message == "Video too long. ReelVault reads reels up to 5 minutes."
    assert not (tmp_path / "r" / "frames").exists()


def test_too_big_and_unreadable(tmp_path, monkeypatch, clips):
    monkeypatch.setattr(signals, "MAX_BYTES", 1000)
    with pytest.raises(signals.VideoRejected) as e:
        signals.check_video(clips["noaudio"])
    assert "50 MB" in e.value.message
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all")
    with pytest.raises(signals.VideoRejected) as e:
        signals.check_video(junk)
    assert e.value.code == "unreadable"


def test_signals_row_shape(processed):
    sig, _ = processed["greenscreen"]
    row = sig.to_row()
    assert set(row) == {"version", "duration", "width", "height", "fps", "transcript", "cuts", "frames",
                        "contact_sheet_path", "sound", "edit", "visual", "error"}
    assert row["width"] == 540 and row["height"] == 960 and row["fps"] == 25
    assert row["contact_sheet_path"] == "sheet_paid_api.jpg"
    json.dumps(row)
    assert all(k in sig.timings for k in ("probe", "cuts", "frames", "sound", "visual", "sheets"))


# ============================================================================================== job handlers (jobs_media.py)

def _reel(uid: str, **kw) -> str:
    import random

    from reelvault.server import db

    rid = db.new_id("reel")
    vals = dict(id=rid, user_id=uid, account_id="acc_test", ig_pk=str(random.randint(10 ** 17, 10 ** 18)),
                code="DAbcd12345x", url="https://www.instagram.com/reel/DAbcd12345x/", tier="priority",
                sample_weight=1.0, audio={"type": "original", "title": "Original audio"})
    vals.update(kw)
    with db.tx() as c:
        c.execute(db.reels.insert().values(**vals))
    return rid


def _row(table, rid, col="id"):
    from reelvault.server import db

    with db.read() as c:
        return c.execute(table.select().where(getattr(table.c, col) == rid)).first()


@pytest.fixture
def advanced(monkeypatch):
    from reelvault.server import pipeline

    calls = []
    monkeypatch.setattr(pipeline, "advance", lambda uid, rid: calls.append(rid))
    return calls


@pytest.fixture
def no_asr(monkeypatch):
    monkeypatch.setattr(signals, "transcribe_server", lambda *a, **k: dict(media.EMPTY_TRANSCRIPT))


def test_process_reel_job_writes_signals(rv_env, member, run_jobs, advanced, no_asr, clips):
    from reelvault.server import db, jobs_media, storage

    rid = _reel(member.id, media_state="have", video_path=None)
    dest = storage.member_path(member.id, f"reels/{rid}/video.mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(clips["sfx_cuts"], dest)
    with db.tx() as c:
        c.execute(db.reels.update().where(db.reels.c.id == rid).values(video_path=f"reels/{rid}/video.mp4"))
    jid = jobs_media.queue_process(member.id, rid, words=False)
    assert jobs_media.queue_process(member.id, rid, words=False) == jid        # dedupe while active
    done = run_jobs()
    job = next(j for j in done if j.id == jid)
    assert job.state == "done" and job.result["signals_version"] == signals.SIGNALS_VERSION
    reel = _row(db.reels, rid)
    assert reel.signals_state == "done" and reel.duration == pytest.approx(24.0, abs=0.1) and reel.has_audio
    sig = jobs_media.signals_for(member.id, rid)
    assert sig["version"] == signals.SIGNALS_VERSION and sig["error"] is None
    assert len(sig["cuts"]) == 9 and len(sig["frames"]) == 12 and sig["edit"]["cuts"] == 9
    assert sig["sound"]["detector"] == "dsp"                                   # no models in rv_env
    assert sig["transcript"]["skipped"] is False                              # no tagger: ASR is not gated
    assert (storage.member_dir(member.id) / f"reels/{rid}/sheet_mcp.jpg").exists()
    assert (storage.member_dir(member.id) / f"reels/{rid}/{sig['frames'][0]['path']}").exists()
    assert advanced == [rid]
    with db.read() as c:
        assert c.execute(db.users.select().where(db.users.c.id == member.id)).first().storage_bytes > 50_000
    assert jobs_media.signals_for("usr_aaaaaaaaaaaaaaaa", rid) is None        # never another member's row


def test_process_reel_too_long_marks_failed(rv_env, member, run_jobs, advanced, tmp_path):
    from reelvault.server import db, jobs_media, storage

    rid = _reel(member.id, media_state="have", video_path=None)
    dest = storage.member_path(member.id, f"reels/{rid}/video.mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x112:r=1:d=302",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(dest)], check=True)
    with db.tx() as c:
        c.execute(db.reels.update().where(db.reels.c.id == rid).values(video_path=f"reels/{rid}/video.mp4"))
    jid = jobs_media.queue_process(member.id, rid, words=True)
    job = next(j for j in run_jobs() if j.id == jid)
    assert job.state == "done" and job.result["error"].startswith("Video too long")
    assert _row(db.reels, rid).signals_state == "failed"
    assert _row(db.reel_signals, rid, "reel_id").error == "Video too long. ReelVault reads reels up to 5 minutes."
    assert advanced == [rid]


def test_process_reel_without_file_goes_back_to_fetch(rv_env, member, run_jobs, advanced):
    from reelvault.server import db, jobs_media

    rid = _reel(member.id, media_state="have", video_path="reels/x/video.mp4")
    jid = jobs_media.queue_process(member.id, rid, words=False)
    job = next(j for j in run_jobs() if j.id == jid)
    assert job.state == "done" and job.result["skipped"] == "no_video"
    assert _row(db.reels, rid).media_state == "none" and advanced == [rid]


def _cdn(monkeypatch, handler_fn):
    import httpx

    from reelvault.server import jobs_media

    seen = []

    def h(req):
        seen.append(req)
        return handler_fn(req)

    monkeypatch.setattr(jobs_media, "TRANSPORT", httpx.MockTransport(h))
    monkeypatch.setattr(jobs_media, "GAP_S", (0.0, 0.0))
    return seen


def test_fetch_video_downloads_from_cdn(rv_env, member, run_jobs, advanced, monkeypatch, clips):
    import httpx

    from reelvault.server import db, jobs_media, storage

    body = clips["noaudio"].read_bytes()
    cover = b"\xff\xd8\xff\xe0" + b"0" * 500
    seen = _cdn(monkeypatch, lambda req: httpx.Response(
        200, content=body if req.url.path.endswith(".mp4") else cover,
        headers={"content-type": "video/mp4" if req.url.path.endswith(".mp4") else "image/jpeg"}))
    from datetime import timedelta

    rid = _reel(member.id, video_url="https://scontent-lhr8-1.cdninstagram.com/v/t66/abc.mp4?oe=6740A1B2",
                cover_url="https://scontent-lhr8-1.cdninstagram.com/v/t51/abc.jpg",
                video_url_expires_at=db.utcnow() + timedelta(hours=5))
    jid = jobs_media.queue_fetch(member.id, rid)
    job = next(j for j in run_jobs() if j.id == jid)
    assert job.state == "done" and job.result["ok"] is True
    reel = _row(db.reels, rid)
    assert reel.media_state == "have" and reel.video_path == f"reels/{rid}/video.mp4"
    assert reel.cover_path == f"reels/{rid}/cover.jpg"
    assert storage.member_path(member.id, reel.video_path).read_bytes() == body
    assert seen[0].headers["referer"] == "https://www.instagram.com/" and "cookie" not in seen[0].headers
    with db.read() as c:
        assert c.execute(db.users.select().where(db.users.c.id == member.id)).first().storage_bytes == \
            len(body) + len(cover)
    assert advanced == [rid]


def test_fetch_video_403_hands_over_to_the_extension(rv_env, member, run_jobs, advanced, monkeypatch):
    import httpx

    from reelvault.server import db, jobs_media

    _cdn(monkeypatch, lambda req: httpx.Response(403, content=b"URL signature expired"))
    rid = _reel(member.id, video_url="https://scontent.cdninstagram.com/v/abc.mp4")
    jid = jobs_media.queue_fetch(member.id, rid)
    job = next(j for j in run_jobs() if j.id == jid)
    assert job.state == "done" and job.result["need_extension"] is True
    with db.read() as c:
        vids = c.execute(db.jobs.select().where(db.jobs.c.user_id == member.id, db.jobs.c.kind == "ig_videos")).all()
    assert len(vids) == 1 and vids[0].payload["items"][0]["reel_id"] == rid      # WP2's scheduler took it
    assert vids[0].executor == "extension" and vids[0].state == "waiting"
    assert advanced == [rid]


@pytest.mark.parametrize("case", ["expired", "not_cdn", "flag_off", "no_url"])
def test_fetch_video_needs_extension_without_calling_out(rv_env, member, run_jobs, advanced, monkeypatch, case):
    import httpx

    from reelvault.server import db, jobs_media, settings

    seen = _cdn(monkeypatch, lambda req: httpx.Response(200, content=b"x"))
    from datetime import timedelta

    kw = {"video_url": "https://scontent.cdninstagram.com/v/abc.mp4"}
    if case == "expired":
        kw["video_url_expires_at"] = db.utcnow() - timedelta(minutes=5)
    elif case == "not_cdn":
        kw["video_url"] = "https://example.com/abc.mp4"
    elif case == "no_url":
        kw["video_url"] = None
    else:
        monkeypatch.setenv("SERVER_CDN_FETCH", "0")
        settings.reload()
    rid = _reel(member.id, **kw)
    jid = jobs_media.queue_fetch(member.id, rid)
    job = next(j for j in run_jobs() if j.id == jid)
    assert job.result["need_extension"] is True and seen == []
    assert _row(db.reels, rid).media_state in ("need_extension", "fetching")      # fetching = in an ig_videos job


def test_fetch_video_skips_when_already_downloaded(rv_env, member, run_jobs, advanced, monkeypatch):
    import httpx

    from reelvault.server import jobs_media, storage

    seen = _cdn(monkeypatch, lambda req: httpx.Response(200, content=b"x"))
    rid = _reel(member.id, media_state="have", video_path=None)
    p = storage.member_path(member.id, f"reels/{rid}/video.mp4")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    from reelvault.server import db

    with db.tx() as c:
        c.execute(db.reels.update().where(db.reels.c.id == rid).values(video_path=f"reels/{rid}/video.mp4"))
    jid = jobs_media.queue_fetch(member.id, rid)
    job = next(j for j in run_jobs() if j.id == jid)
    assert job.result == {"ok": True, "already": True} and seen == [] and advanced == [rid]


def test_cdn_host_rules():
    from reelvault.server.jobs_media import cdn_url_ok, looks_like_mp4

    assert cdn_url_ok("https://scontent-lhr8-1.cdninstagram.com/v/a.mp4")
    assert cdn_url_ok("https://video.xx.fbcdn.net/v/a.mp4")
    assert not cdn_url_ok("http://scontent.cdninstagram.com/v/a.mp4")
    assert not cdn_url_ok("https://cdninstagram.com.evil.example/v/a.mp4")
    assert not cdn_url_ok("https://user:pw@scontent.cdninstagram.com/a.mp4")
    assert not cdn_url_ok(None)
    assert looks_like_mp4(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8) and not looks_like_mp4(b"<html>nope</html>")


@pytest.mark.skipif(not (HAS_CED and HAS_WHISPER), reason="needs CED-tiny and whisper base")
@pytest.mark.parametrize("name", ["speech", "speech_ducked"])
def test_voice_and_ducked_bed_give_no_sfx(processed, name):
    """Fix round 1: sibilants ("S" of "Step"), plosives and a ducked bed's intro kicks are not SFX. A voice-only
    reel must keep its SD01 hint and give the deep read no made-up cue."""
    if name not in processed:
        pytest.skip("no speech source (RV_FIXTURE_SPEECH or network for gTTS)")
    sig, _ = processed[name]
    sfx = [e for e in sig.sound["events"] if e["label"] in soundmap.SFX_LABELS]
    assert sfx == [], sfx
    assert sig.sound["summary"]["sfx_per_10s"] == 0.0
    assert "SD15" not in sig.sound["pattern_hints"]


def test_silence_beat_needs_a_reason_and_laughter_gives_sd16():
    """Fix round 1: plain pauses between sentences are not SD09; laughter / crowd events give SD16."""
    import numpy as np

    def raw_with(silences, music_on=False):
        t0 = np.arange(0, 19, 0.25)
        groups = {"music": np.full(len(t0), 0.9 if music_on else 0.0), "speech": np.full(len(t0), 0.6)}
        dsp = [{"label": "silence", "t": a, "t_end": b, "source": "dsp", "strength": 1.0} for a, b in silences]
        return sound.SoundRaw(duration=20.0, has_audio=True, detector="ced-tiny-int8+dsp", t0=t0, groups=groups,
                              dsp=dsp)

    tr = {"speech_spans": [[0.4, 7.3], [7.8, 10.9], [11.4, 19.5]]}
    pauses = [[7.37, 7.8], [10.97, 11.44]]
    m = soundmap.build(raw_with(pauses), [], [], duration=20.0, transcript=tr)
    assert not m["summary"]["silence_beat"] and "SD09" not in m["pattern_hints"]
    hit = [{"t": 7.85, "t_end": 7.9, "label": "hit", "source": "ml+dsp", "confidence": "high", "score": 0.5}]
    m = soundmap.build(raw_with(pauses), hit, [], duration=20.0, transcript=tr)
    assert m["summary"]["silence_beat"] and "SD09" in m["pattern_hints"]
    m = soundmap.build(raw_with(pauses, music_on=True), [], [], duration=20.0, transcript=tr)
    assert m["summary"]["silence_beat"]                 # the music drops out for the gap
    laughs = [{"t": 4.0, "t_end": 5.5, "label": "laughter", "source": "ml", "confidence": "high", "score": 0.7},
              {"t": 14.0, "t_end": 15.0, "label": "crowd", "source": "ml", "confidence": "high", "score": 0.6}]
    m = soundmap.build(raw_with([]), laughs, [], duration=20.0, transcript=tr)
    assert "SD16" in m["pattern_hints"] and m["summary"]["sfx_counts"] == {}


@needs_ced
def test_dings_under_a_beat_bed_keep_their_onset_and_count(tmp_path, monkeypatch):
    """Fix round 2: a ringing ding under a -20 dB beat bed is timed at its onset (not at a music kick under its
    decaying tail) and is counted once (its tail is not a second 'ml' ding)."""
    import numpy as np

    monkeypatch.setenv("RV_MODELS_DIR", str(MODELS))
    times = [1.5, 4.5, 7.5, 10.5]
    for ding_len in (0.8, 1.2):
        tr = make_media.Track(12.0)
        tr.add(make_media.music(12.0, bpm=100, hats=False), 0.0, None, gain_db=-20)
        for t in times:
            tr.add(make_media.ding(ding_len), t, "ding", gain_db=-4)
        p = tmp_path / f"dings_{ding_len}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(make_media.SR), "-ac", "1", "-i", "-",
                        str(p)], input=np.asarray(tr.done(), np.float32).tobytes(), check=True)
        ev = [e for e in sound.fuse(sound.analyze(p)) if e["label"] == "ding"]
        assert len(ev) == len(times), ev
        for e, t in zip(sorted(ev, key=lambda e: e["t"]), times, strict=True):
            assert abs(e["t"] - t) <= 0.12, (e, t)
