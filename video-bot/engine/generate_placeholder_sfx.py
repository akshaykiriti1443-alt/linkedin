#!/usr/bin/env python3
"""
generate_placeholder_sfx.py
Generates 12 distinct placeholder WAV files in sfx/ using only Python stdlib.
Run once: python engine/generate_placeholder_sfx.py
Replace with real audio from freesound.org for production.
"""

import wave, struct, math, os

OUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'sfx')
os.makedirs(OUT_DIR, exist_ok=True)

SAMPLE_RATE = 48000
CHANNELS    = 2

def write_wav(filename, frames):
    path = os.path.join(OUT_DIR, filename)
    with wave.open(path, 'w') as f:
        f.setnchannels(CHANNELS)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(frames)
    print(f"  wrote {filename}  ({len(frames)//4} samples)")

def samples(duration_s, gen_fn):
    """gen_fn(t) → float in [-1, 1]"""
    n = int(SAMPLE_RATE * duration_s)
    raw = bytearray()
    for i in range(n):
        t = i / SAMPLE_RATE
        v = max(-1.0, min(1.0, gen_fn(t)))
        s = int(v * 32767)
        packed = struct.pack('<h', s)
        raw += packed + packed  # stereo
    return bytes(raw)

def sine(freq, amp=0.7):
    return lambda t: amp * math.sin(2 * math.pi * freq * t)

def envelope(gen_fn, attack=0.01, decay=0.0, sustain_level=1.0, release=0.1, total=0.5):
    def fn(t):
        if t < attack:
            env = t / attack
        elif t < attack + decay:
            env = 1.0 - (1.0 - sustain_level) * ((t - attack) / decay) if decay > 0 else sustain_level
        elif t < total - release:
            env = sustain_level
        else:
            env = sustain_level * max(0, (total - t) / release)
        return gen_fn(t) * env
    return fn

def sweep(f0, f1, amp=0.7):
    def fn(t, dur=0.4):
        freq = f0 + (f1 - f0) * (t / dur)
        return amp * math.sin(2 * math.pi * freq * t)
    return fn

def noise(amp=0.3):
    import random
    rng = random.Random(42)
    return lambda t: amp * (rng.random() * 2 - 1)

def mix(*fns):
    return lambda t: sum(f(t) for f in fns) / len(fns)

print("Generating placeholder SFX...")

# 1. whoosh — rising sweep (transitions)
write_wav('whoosh.wav', samples(0.5, envelope(sweep(200, 1800), attack=0.05, release=0.2, total=0.5)))

# 2. impact — low thud (dramatic reveals)
write_wav('impact.wav', samples(0.6, envelope(mix(sine(60, 0.8), sine(80, 0.5), noise(0.3)), attack=0.002, release=0.4, total=0.6)))

# 3. ding — bright bell (success, key points)
write_wav('ding.wav', samples(0.8, envelope(mix(sine(880, 0.6), sine(1760, 0.3)), attack=0.005, release=0.6, total=0.8)))

# 4. keyboard — rapid mid taps (typing/code)
def keyboard_fn(t):
    clicks = [0.0, 0.08, 0.16, 0.22, 0.30]
    v = 0.0
    for c in clicks:
        dt = t - c
        if 0 <= dt < 0.04:
            v += 0.5 * math.sin(2 * math.pi * 1200 * dt) * (1 - dt / 0.04)
    return v
write_wav('keyboard.wav', samples(0.4, keyboard_fn))

# 5. mouse_click — single sharp click (UI clicks)
def click_fn(t):
    if t < 0.03:
        return 0.7 * math.sin(2 * math.pi * 900 * t) * (1 - t / 0.03)
    return 0.0
write_wav('mouse_click.wav', samples(0.1, click_fn))

# 6. double_click — two clicks (section starts)
def dbl_click_fn(t):
    v = 0.0
    for offset in [0.0, 0.12]:
        dt = t - offset
        if 0 <= dt < 0.03:
            v += 0.7 * math.sin(2 * math.pi * 900 * dt) * (1 - dt / 0.03)
    return v
write_wav('double_click.wav', samples(0.25, dbl_click_fn))

# 7. notification — gentle two-tone chime (tool names / alerts)
def notif_fn(t):
    if t < 0.4:
        return 0.5 * math.sin(2 * math.pi * 660 * t) * math.exp(-3 * t)
    else:
        dt = t - 0.45
        return 0.5 * math.sin(2 * math.pi * 880 * dt) * math.exp(-3 * dt)
write_wav('notification.wav', samples(0.9, notif_fn))

# 8. camera_shutter — snap + mechanical noise (screenshots / results)
def shutter_fn(t):
    if t < 0.05:
        return 0.6 * math.sin(2 * math.pi * 400 * t) * (1 - t / 0.05)
    elif t < 0.12:
        import random
        rng = random.Random(int(t * 10000))
        return 0.2 * (rng.random() * 2 - 1) * (1 - (t - 0.05) / 0.07)
    return 0.0
write_wav('camera_shutter.wav', samples(0.2, shutter_fn))

# 9. riser — slow building sweep (build-up before reveals)
write_wav('riser.wav', samples(1.0, envelope(sweep(100, 600, 0.5), attack=0.3, release=0.3, total=1.0)))

# 10. air_hit — punchy mid burst (action verbs / intros)
write_wav('air_hit.wav', samples(0.35, envelope(mix(sine(300, 0.6), noise(0.4)), attack=0.003, release=0.25, total=0.35)))

# 11. digital_readout — sci-fi blip sequence (stats / numbers / tech terms)
def digital_fn(t):
    blips = [0.0, 0.07, 0.14, 0.21, 0.30]
    freqs = [1400, 1600, 1800, 1400, 2000]
    v = 0.0
    for bt, bf in zip(blips, freqs):
        dt = t - bt
        if 0 <= dt < 0.05:
            v += 0.45 * math.sin(2 * math.pi * bf * dt) * (1 - dt / 0.05)
    return v
write_wav('digital_readout.wav', samples(0.45, digital_fn))

# 12. swoosh_down — falling sweep (dismissals / endings)
write_wav('swoosh_down.wav', samples(0.5, envelope(sweep(1800, 200), attack=0.05, release=0.2, total=0.5)))

print(f"\nDone — 12 WAV files written to sfx/")
print("Replace with real audio from freesound.org for production quality.")
