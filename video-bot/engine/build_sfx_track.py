#!/usr/bin/env python3
"""
build_sfx_track.py
Reads workspace/sfx_events.json and mixes SFX onto a silent timeline.

Input  : workspace/sfx_events.json  [{sfx, at_ms}]
         workspace/meta.json         {duration_s}
Output : workspace/sfx_track.wav

Usage:
  pip install pydub
  python engine/build_sfx_track.py

The sfx_events.json is written by Claude during /soundeffects or /build-timeline.
Shape:
  [
    {"sfx": "digital_readout", "at_ms": 1200},
    {"sfx": "whoosh",          "at_ms": 4800},
    ...
  ]
"""

import json, os, sys
from pathlib import Path

ROOT      = Path(__file__).parent.parent
SFX_DIR   = ROOT / 'sfx'
WORKSPACE = ROOT / 'workspace'
EVENTS_FILE = WORKSPACE / 'sfx_events.json'
META_FILE   = WORKSPACE / 'meta.json'
OUTPUT_FILE = WORKSPACE / 'sfx_track.wav'

# Spacing rules (ms)
MIN_GAP_ANY  = 1500   # 1.5s between any two SFX
MIN_GAP_SAME = 7000   # 7s between same SFX type

# Gain per SFX type (0.0–1.0 multiplier on normalised audio)
GAIN = {
    'whoosh':          0.50,
    'swoosh_down':     0.50,
    'digital_readout': 0.65,
    'impact':          0.70,
    'ding':            0.65,
    'keyboard':        0.55,
    'mouse_click':     0.60,
    'double_click':    0.60,
    'notification':    0.60,
    'camera_shutter':  0.55,
    'riser':           0.45,
    'air_hit':         0.65,
}
DEFAULT_GAIN = 0.60
MAX_SFX_MS   = 1000  # trim all SFX to 1 second max

def load_events(path):
    if not path.exists():
        print(f"ERROR: {path} not found. Run /soundeffects first.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)

def get_duration_ms():
    if META_FILE.exists():
        with open(META_FILE) as f:
            meta = json.load(f)
        dur = meta.get('duration_s')
        if dur:
            return int(float(dur) * 1000)
    # Fallback: estimate from last event + 5s
    return None

def enforce_spacing(events):
    """Filter events to respect min gap rules."""
    kept = []
    last_any_ms = -MIN_GAP_ANY
    last_per_type = {}
    for ev in sorted(events, key=lambda e: e['at_ms']):
        sfx = ev['sfx']
        t   = ev['at_ms']
        gap_any  = t - last_any_ms
        gap_same = t - last_per_type.get(sfx, -MIN_GAP_SAME)
        if gap_any >= MIN_GAP_ANY and gap_same >= MIN_GAP_SAME:
            kept.append(ev)
            last_any_ms = t
            last_per_type[sfx] = t
        else:
            print(f"  skipped {sfx} @ {t}ms (too close)")
    return kept

def build_track():
    try:
        from pydub import AudioSegment
    except ImportError:
        print("ERROR: pydub not installed. Run: pip install pydub")
        sys.exit(1)

    events = load_events(EVENTS_FILE)
    print(f"Loaded {len(events)} SFX events")

    events = enforce_spacing(events)
    print(f"After spacing rules: {len(events)} events kept")

    # Determine track duration
    dur_ms = get_duration_ms()
    if dur_ms is None:
        last_t = max(e['at_ms'] for e in events) if events else 0
        dur_ms = last_t + 5000
    print(f"Track duration: {dur_ms/1000:.1f}s")

    # Build silent canvas
    track = AudioSegment.silent(duration=dur_ms, frame_rate=48000)
    track = track.set_channels(2)

    # Load and cache SFX files
    sfx_cache = {}
    for sfx_path in SFX_DIR.glob('*.wav'):
        name = sfx_path.stem
        try:
            seg = AudioSegment.from_wav(str(sfx_path))
            seg = seg.set_frame_rate(48000).set_channels(2)
            # Normalise to 0 dBFS then apply gain
            peak_db = seg.max_dBFS
            if peak_db < -0.1:
                seg = seg.apply_gain(-peak_db)
            gain_factor = GAIN.get(name, DEFAULT_GAIN)
            seg = seg.apply_gain(20 * (gain_factor - 1))  # dB headroom
            # Trim to max 1 second
            seg = seg[:MAX_SFX_MS]
            sfx_cache[name] = seg
        except Exception as e:
            print(f"  WARNING: could not load {sfx_path.name}: {e}")

    # Overlay events
    placed = 0
    for ev in events:
        sfx  = ev['sfx']
        t_ms = ev['at_ms']
        seg  = sfx_cache.get(sfx)
        if seg is None:
            print(f"  WARNING: {sfx}.wav not found in sfx/, skipping")
            continue
        if t_ms + len(seg) > dur_ms:
            seg = seg[:dur_ms - t_ms]
        track = track.overlay(seg, position=t_ms)
        placed += 1

    WORKSPACE.mkdir(exist_ok=True)
    track.export(str(OUTPUT_FILE), format='wav')
    print(f"\n✅ SFX track written → workspace/sfx_track.wav")
    print(f"   Events placed : {placed}")
    print(f"   Duration      : {dur_ms/1000:.1f}s")
    print(f"\n▶  Premiere: import workspace/sfx_track.wav onto A2 track")

if __name__ == '__main__':
    build_track()
