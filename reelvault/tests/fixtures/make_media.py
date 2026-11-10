"""Generate the synthetic test reels for the signals tests (DESIGN-SAAS.md 12.1, WP3).

Every clip is made from code (numpy + OpenCV drawing + ffmpeg encode), so no real person, brand or song is in the
repo. Each clip has a ground-truth JSON next to it (cuts, sound events, silences, the layout it shows).

    python tests/fixtures/make_media.py [out_dir]          # default: tests/fixtures/media
    python tests/fixtures/make_media.py --force            # rebuild even when the version stamp matches

From tests:   from make_media import ensure ; paths = ensure(out_dir)   -> {"sfx_cuts": Path(.../sfx_cuts.mp4), ...}

Clips (portrait 540x960, 25 fps, AAC 48 kHz mono):
    sfx_cuts      colour scenes with 9 known cuts (one same-background jump cut); music bed with real sound effects:
                  whoosh sweeps peaking on cuts, hits on cuts, a riser that lands on a cut, a beep and a ding off the
                  cuts, and a 0.8 s silence beat
    talking_head  a drawn face that YuNet detects, mouth moving, zoom jump cuts, a fixed headline box at the top and
                  word-by-word captions in the lower middle; steady music bed, hit on the hook
    screen        a software UI (bars, sidebar, cards, many small text lines, cursor, scrolling) with a small face
                  bubble in the bottom-right corner; typing and mouse clicks over quiet music
    split         two moving panes (face on top, other footage below) split by a horizontal line
    greenscreen   a cut-out person with a white halo over still "post" screenshots that change on the cuts
    slideshow     6 still slides held 2.5 s each; music only
    noaudio       6 s of moving shapes and NO audio stream
    speech        (only when a speech source is available) a face clip with real narration over a ducked music bed.
                  Source: env RV_FIXTURE_SPEECH (any audio file ffmpeg reads) or gTTS when the network allows it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

VERSION = 6                      # bump when a clip or its truth changes
W, H, FPS = 540, 960, 25
SR = 48000
HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "media"
CLIPS = ("sfx_cuts", "talking_head", "screen", "split", "greenscreen", "slideshow", "noaudio", "speech",
         "speech_ducked")


# ============================================================================================== sound synthesis

def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _t(d: float) -> np.ndarray:
    return np.arange(int(round(d * SR))) / SR


def _band_noise(n: int, lo: float, hi: float, rng) -> np.ndarray:
    """White noise band-limited with an FFT mask (numpy only)."""
    x = rng.standard_normal(n)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / SR)
    X[(f < lo) | (f > hi)] = 0
    y = np.fft.irfft(X, n)
    return y / (np.abs(y).max() + 1e-9)


def whoosh(d: float = 0.6, seed: int = 1) -> np.ndarray:
    """Noise swept upward through a band (500 Hz -> 7 kHz) with a sin^2 swell: peaks at d/2."""
    rng = _rng(seed)
    n = int(d * SR)
    hop, win = 256, 1024
    noise = rng.standard_normal(n + win)
    out = np.zeros(n + win)
    wnd = np.hanning(win)
    freqs = np.fft.rfftfreq(win, 1 / SR)
    for s in range(0, n, hop):
        p = s / n
        fc = 500 * (14 ** p)                       # 500 Hz -> 7 kHz
        bw = 0.6 * fc + 300
        mask = np.exp(-0.5 * ((freqs - fc) / bw) ** 2)
        seg = np.fft.irfft(np.fft.rfft(noise[s:s + win] * wnd) * mask, win)
        out[s:s + win] += seg * wnd
    out = out[:n]
    env = np.sin(np.pi * np.arange(n) / n) ** 2
    y = out * env
    return (0.9 * y / (np.abs(y).max() + 1e-9)).astype(np.float32)


def hit(d: float = 0.45, seed: int = 2) -> np.ndarray:
    """Low thump with a pitch drop plus a short bright crack: a typical 'impact' SFX. Onset at sample 0."""
    t = _t(d)
    f = 45 + 110 * np.exp(-t * 25)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 9)
    crack = _band_noise(len(t), 1500, 9000, _rng(seed)) * np.exp(-t * 60)
    y = 0.85 * body + 0.6 * crack
    return (0.95 * y / np.abs(y).max()).astype(np.float32)


def riser(d: float = 2.0, seed: int = 3) -> np.ndarray:
    """Upward sine sweep + brightening noise; loudness and brightness both climb to the end."""
    t = _t(d)
    p = t / d
    f = 200 * (12 ** p)
    tone = np.sin(2 * np.pi * np.cumsum(f) / SR)
    noise = _band_noise(len(t), 2000, 9000, _rng(seed)) * p
    env = 10 ** ((-16 + 16 * p) / 20)              # -16 dB -> 0 dB: audible over the bed from its start
    y = env * (0.6 * tone + 0.5 * noise)
    return (0.9 * y / np.abs(y).max()).astype(np.float32)


def beep(d: float = 0.18, f: float = 1000.0) -> np.ndarray:
    t = _t(d)
    env = np.minimum(1, np.minimum(t, d - t) / 0.005)
    return (0.6 * np.sin(2 * np.pi * f * t) * env).astype(np.float32)


def ding(d: float = 1.0) -> np.ndarray:
    t = _t(d)
    s = sum(a * np.sin(2 * np.pi * f * t) for f, a in [(1568, 1.0), (3136, 0.4), (4704, 0.2), (2349, 0.3)])
    return (0.45 * s * np.exp(-t * 4)).astype(np.float32)


def click(seed: int = 4) -> np.ndarray:
    t = _t(0.02)
    return (0.8 * _rng(seed).standard_normal(len(t)) * np.exp(-t * 400)).astype(np.float32)


def typing(d: float, seed: int = 5) -> tuple[np.ndarray, list[float]]:
    """Irregular key clicks (60-140 ms apart). Returns the signal and the key times."""
    rng = _rng(seed)
    out = np.zeros(int(d * SR), np.float32)
    t, times = 0.05, []
    while t < d - 0.05:
        c = click(int(rng.integers(0, 1 << 30))) * float(rng.uniform(0.35, 0.7))
        i = int(t * SR)
        out[i:i + len(c)] += c[:len(out) - i]
        times.append(round(t, 3))
        t += float(rng.uniform(0.06, 0.14))
    return out, times


def music(d: float, bpm: float = 100.0, seed: int = 6, hats: bool = True) -> np.ndarray:
    """A simple loop: 4 chords (sines with harmonics), a bass, a kick on every beat, soft hats off-beat."""
    rng = _rng(seed)
    n = int(d * SR)
    t = np.arange(n) / SR
    beat = 60 / bpm
    s = np.zeros(n)
    chords = [[220, 277, 330], [196, 247, 294], [175, 220, 262], [196, 247, 294]]
    bar = 4 * beat
    for k in range(int(d / bar) + 1):
        a, b = int(k * bar * SR), min(n, int((k + 1) * bar * SR))
        if a >= n:
            break
        tt = t[a:b] - t[a]
        c = chords[k % 4]
        # bright, detuned saw-like pads (pure sines read as "beep / sine wave" to the tagger)
        pad = sum(sum(np.sin(2 * np.pi * f * h * (1 + det) * tt) / h for h in range(1, 9))
                  for f in c for det in (-0.004, 0.004)) * 0.035
        bass = 0.18 * np.sin(2 * np.pi * (c[0] / 2) * tt)
        s[a:b] += pad + bass
    kt = _t(0.22)
    kick = np.sin(2 * np.pi * (48 + 90 * np.exp(-kt * 30)) * kt) * np.exp(-kt * 14) * 0.45
    hat = _band_noise(int(0.04 * SR), 5000, 9000, rng) * np.exp(-_t(0.04) * 90) * 0.05
    for k in range(int(d / beat) + 1):
        a = int(k * beat * SR)
        if a >= n:
            break
        s[a:a + len(kick)] += kick[:n - a]
        if hats:
            h = a + int(beat / 2 * SR)
            if h < n:
                s[h:h + len(hat)] += hat[:n - h]
    # gentle fade in/out so the loop edges are not transients
    fade = int(0.05 * SR)
    s[:fade] *= np.linspace(0, 1, fade)
    s[-fade:] *= np.linspace(1, 0, fade)
    return s.astype(np.float32)


def _db(x: float) -> float:
    return 10 ** (x / 20)


class Track:
    """Mono mix with ground-truth bookkeeping."""

    def __init__(self, d: float):
        self.d = d
        self.x = np.zeros(int(d * SR), np.float32)
        self.truth: list[dict] = []

    def add(self, sig: np.ndarray, t: float, label: str | None = None, gain_db: float = 0.0, **extra) -> None:
        i = int(round(t * SR))
        seg = sig[:max(0, len(self.x) - i)] * _db(gain_db)
        self.x[i:i + len(seg)] += seg
        if label:
            self.truth.append({"label": label, "t": round(t, 3), "t_end": round(t + len(sig) / SR, 3), **extra})

    def mute(self, a: float, b: float) -> None:
        self.x[int(a * SR):int(b * SR)] = 0
        self.truth.append({"label": "silence", "t": a, "t_end": b})

    def done(self) -> np.ndarray:
        peak = float(np.abs(self.x).max())
        return self.x / max(1.0, peak / 0.95)


def write_wav(path: Path, x: np.ndarray) -> None:
    import wave

    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


# ============================================================================================== drawing

def _cv2():
    import cv2

    return cv2


def draw_face(img, cx: int, cy: int, s: float = 1.0, mouth: float = 0.3, skin=(135, 170, 225), body=True) -> tuple:
    """A drawn head (and shoulders) that YuNet detects at ~0.9. Returns the head box (x, y, w, h)."""
    cv2 = _cv2()
    if body:
        cv2.ellipse(img, (cx, int(cy + 330 * s)), (int(230 * s), int(150 * s)), 0, 180, 360, (90, 60, 40), -1)
        cv2.rectangle(img, (int(cx - 45 * s), int(cy + 80 * s)), (int(cx + 45 * s), int(cy + 200 * s)),
                      (120, 150, 200), -1)
    cv2.ellipse(img, (cx, int(cy - 20 * s)), (int(118 * s), int(150 * s)), 0, 0, 360, (30, 35, 45), -1)
    cv2.ellipse(img, (cx, cy), (int(100 * s), int(135 * s)), 0, 0, 360, skin, -1)
    cv2.ellipse(img, (cx, int(cy - 95 * s)), (int(108 * s), int(60 * s)), 0, 180, 360, (30, 35, 45), -1)
    for dx in (-38, 38):
        ex, ey = int(cx + dx * s), int(cy - 20 * s)
        cv2.ellipse(img, (ex, ey), (max(1, int(20 * s)), max(1, int(10 * s))), 0, 0, 360, (245, 245, 245), -1)
        cv2.circle(img, (ex, ey), max(1, int(8 * s)), (60, 40, 30), -1)
        cv2.circle(img, (ex, ey), max(1, int(4 * s)), (10, 10, 10), -1)
        cv2.line(img, (int(ex - 22 * s), int(ey - 22 * s)), (int(ex + 22 * s), int(ey - 26 * s)), (30, 35, 45),
                 max(1, int(6 * s)))
    pts = np.array([[cx, int(cy - 10 * s)], [int(cx - 14 * s), int(cy + 35 * s)], [int(cx + 14 * s), int(cy + 35 * s)]],
                   np.int32)
    cv2.fillPoly(img, [pts], (115, 150, 205))
    cv2.ellipse(img, (cx, int(cy + 70 * s)), (max(1, int(32 * s)), max(1, int((6 + 18 * mouth) * s))), 0, 0, 360,
                (80, 80, 170), -1)
    return (int(cx - 118 * s), int(cy - 170 * s), int(236 * s), int(305 * s))


def put_text(img, text: str, x: int, y: int, scale: float, color=(255, 255, 255), thick: int = 2, center=False):
    cv2 = _cv2()
    font = cv2.FONT_HERSHEY_DUPLEX
    if center:
        (tw, _), _ = cv2.getTextSize(text, font, scale, thick)
        x = int(x - tw / 2)
    cv2.putText(img, text, (x, y), font, scale, color, thick, cv2.LINE_AA)


def bg(color) -> np.ndarray:
    img = np.empty((H, W, 3), np.uint8)
    img[:] = color
    return img


def gradient_bg(c1, c2) -> np.ndarray:
    a = np.linspace(0, 1, H)[:, None, None]
    img = (np.array(c1)[None, None, :] * (1 - a) + np.array(c2)[None, None, :] * a)
    return np.repeat(img, W, axis=1).astype(np.uint8)


# ============================================================================================== encode

def encode(path: Path, frames, n_frames: int, audio: np.ndarray | None) -> None:
    """frames: iterator of HxWx3 BGR uint8. Writes H.264 + AAC (or video only)."""
    ff = shutil.which("ffmpeg")
    if not ff:
        raise SystemExit("ffmpeg is needed to build the test clips.")
    with tempfile.TemporaryDirectory() as td:
        cmd = [ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
               "-r", str(FPS), "-i", "-"]
        if audio is not None:
            wav = Path(td) / "a.wav"
            write_wav(wav, audio)
            cmd += ["-i", str(wav), "-c:a", "aac", "-b:a", "128k", "-ar", str(SR), "-ac", "1"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-g", "50",
                "-movflags", "+faststart", "-frames:v", str(n_frames), str(path)]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        assert p.stdin is not None
        for f in frames:
            p.stdin.write(np.ascontiguousarray(f).tobytes())
        p.stdin.close()
        if p.wait() != 0:
            raise RuntimeError(f"ffmpeg failed for {path.name}")


def scene_index(t: float, cuts: list[float]) -> int:
    return sum(1 for c in cuts if t >= c)


# ============================================================================================== clips

def clip_sfx_cuts(out: Path) -> dict:
    cv2 = _cv2()
    d = 24.0
    cuts = [2.5, 4.6, 6.8, 9.0, 11.6, 13.0, 15.2, 18.0, 20.4]
    jump_cut = 18.0          # same background before and after; only the subject moves and grows
    colors = [(40, 40, 200), (200, 120, 30), (60, 170, 60), (180, 50, 160), (30, 160, 200), (120, 120, 120),
              (20, 90, 150), (90, 30, 30), (90, 30, 30), (200, 200, 60)]

    def frames():
        for i in range(int(d * FPS)):
            t = i / FPS
            k = scene_index(t, cuts)
            img = bg(colors[k])
            # a slowly moving subject so shots are not frozen
            ph = t - (cuts[k - 1] if k else 0)
            if k == 8:   # after the jump cut: same background, subject jumps right and grows
                cx, r = 360 + int(10 * np.sin(ph * 2)), 150
            elif k == 7:
                cx, r = 180 + int(10 * np.sin(ph * 2)), 95
            else:
                cx, r = 270 + int(60 * np.sin(ph * 1.5)), 110
            cv2.circle(img, (cx, 480 + int(20 * np.cos(ph))), r, (240, 240, 240), -1)
            cv2.rectangle(img, (60, 760), (480, 820), (20, 20, 20), -1)
            put_text(img, f"SCENE {k + 1}", 270, 805, 1.3, center=True)
            yield img

    tr = Track(d)
    tr.add(music(d), 0.0, None, gain_db=-14)
    tr.mute(16.0, 16.8)
    # whooshes PEAK on their cut (the sweep is 0.6 s long, so it starts 0.3 s before)
    for c in (2.5, 6.8, 13.0, 20.4):
        tr.add(whoosh(0.6, seed=int(c * 10)), c - 0.3, "whoosh", gain_db=-2, peak=c, on_cut=c)
    for c in (4.6, 9.0, 18.0):
        tr.add(hit(seed=int(c * 10)), c, "hit", gain_db=-1, on_cut=c)
    tr.add(riser(2.0), 9.6, "riser", gain_db=-3, on_cut=11.6)
    tr.add(hit(seed=116), 11.6, "hit", gain_db=-1, on_cut=11.6)
    tr.add(beep(), 7.9, "beep", gain_db=-4)
    tr.add(ding(), 14.2, "ding", gain_db=-4)
    audio = tr.done()
    encode(out, frames(), int(d * FPS), audio)
    return {"duration": d, "cuts": cuts, "jump_cut": jump_cut, "events": tr.truth,
            "cuts_with_sfx": [2.5, 4.6, 6.8, 9.0, 11.6, 13.0, 18.0, 20.4], "cuts_without_sfx": [15.2],
            "riser": [9.6, 11.6], "silence": [16.0, 16.8], "off_cut_sfx": [7.9, 14.2],
            "layout": "colour scenes", "expect_vf": None}


def clip_talking_head(out: Path) -> dict:
    cv2 = _cv2()
    d = 20.0
    cuts = [3.1, 6.3, 9.4, 12.6, 15.8]
    words = "STOP RUNNING ADS LIKE THIS DO THIS INSTEAD AND WATCH YOUR COST PER LEAD DROP FAST".split()

    def frames():
        for i in range(int(d * FPS)):
            t = i / FPS
            k = scene_index(t, cuts)
            img = gradient_bg((70, 85, 105), (40, 50, 60))
            cv2.rectangle(img, (0, 640), (W, H), (55, 60, 70), -1)            # desk
            zoom = 1.0 if k % 2 == 0 else 1.18                                 # punch-in on every other shot
            sway = int(8 * np.sin(t * 1.3))
            draw_face(img, 270 + sway, 430 + int(4 * np.sin(t * 2.1)), s=1.0 * zoom,
                      mouth=0.5 + 0.5 * np.sin(t * 13))
            # headline box, fixed from 0 s
            cv2.rectangle(img, (40, 70), (500, 180), (255, 255, 255), -1)
            put_text(img, "3 ADS THAT", 270, 115, 1.25, (0, 0, 0), 3, center=True)
            put_text(img, "PRINT LEADS", 270, 165, 1.25, (0, 0, 0), 3, center=True)
            # word-by-word captions in the lower middle
            w = words[int(t / 0.6) % len(words)]
            put_text(img, w, 270, 720, 1.6, (0, 230, 255), 4, center=True)
            yield img

    tr = Track(d)
    tr.add(hit(seed=9), 0.3, "hit", gain_db=-2)
    tr.add(music(d, bpm=92, hats=False), 0.0, None, gain_db=-16)
    encode(out, frames(), int(d * FPS), tr.done())
    return {"duration": d, "cuts": cuts, "events": tr.truth, "layout": "talking head + headline box + captions",
            "expect_vf": "VF01", "headline_box": True, "caption_band": "bottom"}


def _ui_frame(t: float, scroll: float) -> np.ndarray:
    cv2 = _cv2()
    img = bg((246, 246, 246))
    cv2.rectangle(img, (0, 0), (W, 56), (52, 52, 58), -1)                  # top bar
    for j, label in enumerate(("File", "Edit", "View", "Layer", "Help")):
        put_text(img, label, 14 + j * 70, 36, 0.55, (230, 230, 230), 1)
    cv2.rectangle(img, (0, 56), (120, H), (232, 233, 238), -1)             # sidebar
    cv2.line(img, (120, 56), (120, H), (190, 190, 196), 2)
    for j in range(16):
        y = 90 + j * 34
        cv2.rectangle(img, (12, y - 12), (26, y + 2), (150, 150, 160), -1)
        put_text(img, f"Frame {j + 1}", 32, y, 0.42, (70, 70, 80), 1)
    off = int(scroll) % 220
    for row in range(-1, 5):
        y0 = 80 + row * 220 - off
        for col in range(2):
            x0 = 140 + col * 195
            cv2.rectangle(img, (x0, y0), (x0 + 180, y0 + 200), (255, 255, 255), -1)
            cv2.rectangle(img, (x0, y0), (x0 + 180, y0 + 200), (200, 200, 205), 1)
            cv2.rectangle(img, (x0 + 10, y0 + 10), (x0 + 170, y0 + 90), (120, 170, 230), -1)
            for ln in range(4):
                put_text(img, f"Item {row * 2 + col} text {ln}", x0 + 10, y0 + 115 + ln * 22, 0.42, (60, 60, 70), 1)
    cv2.rectangle(img, (120, H - 60), (W, H), (236, 236, 240), -1)        # bottom toolbar
    cv2.line(img, (120, H - 60), (W, H - 60), (190, 190, 196), 2)
    for j in range(5):
        put_text(img, f"Tool{j}", 135 + j * 78, H - 25, 0.45, (80, 80, 90), 1)
    cx, cy = int(200 + 150 * np.sin(t * 0.7)), int(400 + 200 * np.sin(t * 0.43))   # cursor
    pts = np.array([[cx, cy], [cx, cy + 22], [cx + 6, cy + 16], [cx + 15, cy + 16]], np.int32)
    cv2.fillPoly(img, [pts], (0, 0, 0))
    return img


def clip_screen(out: Path) -> dict:
    cv2 = _cv2()
    d = 20.0
    cuts = [6.5, 13.4]

    def frames():
        for i in range(int(d * FPS)):
            t = i / FPS
            k = scene_index(t, cuts)
            img = _ui_frame(t, scroll=t * 30 + k * 400)
            # face bubble: a small round face cam in the bottom-right corner (~2.5% of the frame)
            bub = np.zeros((150, 150, 3), np.uint8)
            bub[:] = (70, 85, 105)
            draw_face(bub, 75, 70, s=0.4, mouth=0.5 + 0.5 * np.sin(t * 12), body=True)
            mask = np.zeros((150, 150), np.uint8)
            cv2.circle(mask, (75, 75), 72, 255, -1)
            x0, y0 = W - 165, H - 230
            roi = img[y0:y0 + 150, x0:x0 + 150]
            roi[mask > 0] = bub[mask > 0]
            cv2.circle(img, (x0 + 75, y0 + 75), 73, (255, 255, 255), 3)
            yield img

    tr = Track(d)
    tr.add(music(d, bpm=110, hats=False), 0.0, None, gain_db=-24)
    ty, times = typing(4.0)
    tr.add(ty, 2.0, "typing", gain_db=-3)
    tr.add(ty, 9.0, "typing", gain_db=-3)
    for c in (5.0, 8.2, 12.0, 15.5, 17.1):
        tr.add(click(int(c * 10)), c, "click", gain_db=-2)
    encode(out, frames(), int(d * FPS), tr.done())
    return {"duration": d, "cuts": cuts, "events": tr.truth, "layout": "screen recording + face bubble",
            "expect_vf": "VF05", "face_bubble": True}


def clip_split(out: Path) -> dict:
    cv2 = _cv2()
    d = 16.0
    cuts = [8.0]

    def frames():
        for i in range(int(d * FPS)):
            t = i / FPS
            top = gradient_bg((90, 70, 60), (60, 50, 40))[:H // 2].copy()
            draw_face(top, 270 + int(40 * np.sin(t * 1.1)), 230, s=0.85, mouth=0.5 + 0.5 * np.sin(t * 11))
            bot = bg((30, 110, 60))[:H // 2].copy()
            for j in range(3):   # other footage: moving blocks, independent of the top
                x = int((t * (90 + 40 * j) + j * 170) % (W + 120)) - 60
                cv2.rectangle(bot, (x, 60 + j * 130), (x + 110, 150 + j * 130), (40 + 70 * j, 200, 240 - 60 * j), -1)
            if t >= cuts[0]:
                bot = cv2.flip(bot, 1)
            img = np.vstack([top, bot])
            cv2.line(img, (0, H // 2), (W, H // 2), (255, 255, 255), 4)
            yield img

    tr = Track(d)
    tr.add(music(d, bpm=120), 0.0, None, gain_db=-16)
    encode(out, frames(), int(d * FPS), tr.done())
    return {"duration": d, "cuts": cuts, "events": tr.truth, "layout": "two panes", "expect_vf": "VF06"}


def _post_card(k: int) -> np.ndarray:
    cv2 = _cv2()
    img = bg([(225, 232, 240), (238, 230, 222), (230, 238, 228)][k % 3])
    cv2.rectangle(img, (30, 60), (510, 560), (255, 255, 255), -1)
    cv2.circle(img, (80, 115), 30, (180, 130, 60), -1)
    put_text(img, f"Founder {k + 1}", 125, 110, 0.75, (20, 20, 20), 2)
    put_text(img, "@founder_posts", 125, 138, 0.55, (120, 120, 120), 1)
    lines = [["Nobody tells you this", "about your first", "10 clients:"], ["I spent $40k on ads", "before I learned", "this one rule."],
             ["Hot take:", "your offer is the", "problem, not your ads."]][k % 3]
    for j, ln in enumerate(lines):
        put_text(img, ln, 55, 220 + j * 60, 1.05, (15, 15, 15), 2)
    put_text(img, "12:41 PM - 2.1M views", 55, 480, 0.6, (120, 120, 120), 1)
    return img


def clip_greenscreen(out: Path) -> dict:
    cv2 = _cv2()
    d = 18.0
    cuts = [6.0, 12.0]
    cards = [_post_card(k) for k in range(3)]

    def frames():
        for i in range(int(d * FPS)):
            t = i / FPS
            k = scene_index(t, cuts)
            img = cards[k].copy()
            # the cut-out person: drawn on its own layer, pasted with a white halo (keyer edge)
            layer = np.zeros((H, W, 3), np.uint8)
            cx, cy = 270 + int(45 * np.sin(t * 1.4)), 760 + int(12 * np.sin(t * 2.3))
            draw_face(layer, cx, cy, s=0.75, mouth=0.5 + 0.5 * np.sin(t * 12))
            mask = (layer.sum(axis=2) > 0).astype(np.uint8) * 255
            halo = cv2.dilate(mask, np.ones((9, 9), np.uint8))
            img[halo > 0] = (250, 250, 250)
            img[mask > 0] = layer[mask > 0]
            yield img

    tr = Track(d)
    tr.add(music(d, bpm=96, hats=False), 0.0, None, gain_db=-18)
    tr.add(whoosh(0.6, seed=60), 5.7, "whoosh", gain_db=-3, peak=6.0, on_cut=6.0)
    tr.add(whoosh(0.6, seed=120), 11.7, "whoosh", gain_db=-3, peak=12.0, on_cut=12.0)
    encode(out, frames(), int(d * FPS), tr.done())
    return {"duration": d, "cuts": cuts, "events": tr.truth, "layout": "cut-out speaker over still posts",
            "expect_vf": "VF04"}


def clip_slideshow(out: Path) -> dict:
    cv2 = _cv2()
    d = 15.0
    cuts = [2.5, 5.0, 7.5, 10.0, 12.5]
    slides = []
    for k in range(6):
        img = gradient_bg([(30, 30, 30), (90, 40, 20), (20, 60, 90), (60, 20, 70), (20, 80, 40), (80, 80, 20)][k],
                          (10, 10, 10))
        put_text(img, f"STEP {k + 1}", 270, 360, 2.0, (255, 255, 255), 4, center=True)
        put_text(img, ["Pick one offer", "Find the pain", "Write the hook", "Show the proof", "Ask once",
                       "Repeat weekly"][k], 270, 470, 1.15, (230, 230, 230), 2, center=True)
        cv2.rectangle(img, (120, 560), (420, 580), (200, 200, 200), -1)
        slides.append(img)

    def frames():
        for i in range(int(d * FPS)):
            yield slides[scene_index(i / FPS, cuts)]

    tr = Track(d)
    tr.add(music(d, bpm=120), 0.0, None, gain_db=-10)
    encode(out, frames(), int(d * FPS), tr.done())
    return {"duration": d, "cuts": cuts, "events": tr.truth, "layout": "slideshow", "expect_vf": "VF09"}


def clip_noaudio(out: Path) -> dict:
    cv2 = _cv2()
    d = 6.0

    def frames():
        for i in range(int(d * FPS)):
            t = i / FPS
            img = bg((60, 60, 60))
            cv2.circle(img, (int(100 + 60 * t), 480), 80, (0, 200, 255), -1)
            yield img

    encode(out, frames(), int(d * FPS), None)
    return {"duration": d, "cuts": [], "events": [], "layout": "no audio", "expect_vf": None}


def _speech_source(td: Path) -> Path | None:
    src = os.environ.get("RV_FIXTURE_SPEECH", "").strip()
    if src and Path(src).exists():
        return Path(src)
    try:  # optional, needs the network
        from gtts import gTTS  # type: ignore

        p = td / "speech.mp3"
        gTTS("Most agency owners run ads the wrong way. They test ten audiences and one offer. Do the opposite. "
             "Test one audience and ten hooks. Here is why that works. The hook decides who stops scrolling. "
             "The audience only decides who sees it. So write ten hooks this week and keep the best two.",
             lang="en").save(str(p))
        return p
    except Exception:  # noqa: BLE001
        return None


def clip_speech(out: Path) -> dict | None:
    with tempfile.TemporaryDirectory() as td:
        src = _speech_source(Path(td))
        if src is None:
            return None
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                             capture_output=True, check=True).stdout
    sp = np.frombuffer(raw, np.float32).copy()
    sp = sp[:int(40 * SR)]
    sp /= np.abs(sp).max() + 1e-9
    d = round(len(sp) / SR + 2.0, 2)
    cuts = [round(x, 2) for x in np.arange(3.0, d - 1.0, 3.4)]

    def frames():
        for i in range(int(d * FPS)):
            t = i / FPS
            k = scene_index(t, cuts)
            img = gradient_bg((80, 90, 100), (40, 45, 50))
            draw_face(img, 270 + int(6 * np.sin(t)), 430, s=1.0 if k % 2 == 0 else 1.15,
                      mouth=0.5 + 0.5 * np.sin(t * 13))
            yield img

    tr = Track(d)
    tr.add(music(d, bpm=96, hats=False), 0.0, None, gain_db=-24)      # quiet bed under the voice
    tr.add(sp, 1.0, "speech", gain_db=-2)
    encode(out, frames(), int(d * FPS), tr.done())
    return {"duration": d, "cuts": cuts, "events": tr.truth, "layout": "talking head with narration",
            "expect_vf": "VF01", "speech": [1.0, round(1.0 + len(sp) / SR, 2)]}


def clip_speech_ducked(out: Path) -> dict | None:
    """Fix round 1: the same voice over a music bed that is loud in the intro and the pauses and ducked 14 dB under
    the voice (kicks every beat). True SFX: none. Sibilants, plosives and the bed's intro kicks must not read as
    whooshes, clicks or hits."""
    with tempfile.TemporaryDirectory() as td:
        src = _speech_source(Path(td))
        if src is None:
            return None
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                             capture_output=True, check=True).stdout
    sp = np.frombuffer(raw, np.float32).copy()[:int(30 * SR)]
    sp /= np.abs(sp).max() + 1e-9
    lead = 2.0
    d = round(len(sp) / SR + lead + 1.5, 2)
    cuts = [round(x, 2) for x in np.arange(3.3, d - 1.0, 3.3)]
    # duck envelope: -14 dB while the voice is on (50 ms attack, 300 ms release)
    env = np.zeros(int(d * SR), np.float32)
    a = int(lead * SR)
    lvl = np.convolve(np.abs(sp), np.ones(int(0.05 * SR)) / int(0.05 * SR), mode="same")
    env[a:a + len(sp)] = (lvl[:len(env) - a] > 0.02).astype(np.float32)
    held = np.convolve(env, np.ones(int(0.3 * SR)), mode="full")[:len(env)] > 0      # 300 ms release
    gain = np.where(held, _db(-14), 1.0).astype(np.float32)
    gain = np.convolve(gain, np.ones(int(0.05 * SR)) / int(0.05 * SR), mode="same")  # 50 ms ramps
    bed = music(d, bpm=96, hats=False)
    n = min(len(bed), len(gain))
    bed = bed[:n] * gain[:n] * _db(-8)

    def frames():
        for i in range(int(d * FPS)):
            t = i / FPS
            k = scene_index(t, cuts)
            img = gradient_bg((70, 80, 95), (35, 40, 50))
            draw_face(img, 270, 430, s=1.0 if k % 2 == 0 else 1.12, mouth=0.5 + 0.5 * np.sin(t * 13))
            yield img

    tr = Track(d)
    tr.add(bed, 0.0, None)
    tr.add(sp, lead, "speech", gain_db=-2)
    encode(out, frames(), int(d * FPS), tr.done())
    return {"duration": d, "cuts": cuts, "events": tr.truth, "layout": "talking head, ducked music bed",
            "expect_vf": "VF01", "expect_sd": "SD03", "sfx": 0, "speech": [lead, round(lead + len(sp) / SR, 2)]}


BUILDERS = {"sfx_cuts": clip_sfx_cuts, "talking_head": clip_talking_head, "screen": clip_screen,
            "split": clip_split, "greenscreen": clip_greenscreen, "slideshow": clip_slideshow,
            "noaudio": clip_noaudio, "speech": clip_speech, "speech_ducked": clip_speech_ducked}


# ============================================================================================== entry points

def ensure(out_dir: Path | str = DEFAULT_OUT, *, force: bool = False, only: tuple[str, ...] | None = None) -> dict:
    """Build missing (or outdated) clips. Returns {name: Path} for every clip that exists (speech may be absent)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = out / "VERSION"
    if force or not stamp.exists() or stamp.read_text().strip() != str(VERSION):
        for p in out.glob("*.mp4"):
            p.unlink()
        for p in out.glob("*.truth.json"):
            p.unlink()
        stamp.write_text(str(VERSION))
    got = {}
    for name in only or CLIPS:
        mp4, truth = out / f"{name}.mp4", out / f"{name}.truth.json"
        if not (mp4.exists() and truth.exists()):
            tmp = out / f".{name}.tmp.mp4"
            info = BUILDERS[name](tmp)
            if info is None:
                tmp.unlink(missing_ok=True)
                continue
            tmp.replace(mp4)
            truth.write_text(json.dumps(info, indent=1))
        got[name] = mp4
    return got


def truth(path: Path) -> dict:
    return json.loads(Path(str(path)[:-4] + ".truth.json").read_text())


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    res = ensure(args[0] if args else DEFAULT_OUT, force="--force" in sys.argv)
    for k, v in res.items():
        print(f"{k:13s} {v}  {v.stat().st_size // 1024} KB")
