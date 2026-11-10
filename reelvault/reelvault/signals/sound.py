"""Local sound detector: sherpa-onnx CED-tiny int8 tagger + a numpy DSP layer (DESIGN-SAAS.md 6.4, steps 1-4).

Ported from the research prototype `saas/sound/scripts/sfx_timeline.py` (CED backend only). No torch, no librosa.

    raw = analyze(video_or_audio)            # -> SoundRaw: tagger matrix (or None), DSP events, loudness, lufs
    raw.speech_likely()                      # ASR gate: speech group >= 0.3 in >= 10% of windows (6.1)
    events = fuse(raw)                       # ML events confirmed by a DSP onset + DSP whoosh/riser/hit, 6.4 step 4

The CED model is read from `<models>/ced-tiny/{model.int8.onnx,class_labels_indices.csv}`; `models` is
`RV_MODELS_DIR` (default /opt/reelvault/models). Without the model, everything still runs on DSP alone
(`detector = "dsp"`; no labels such as music/ding/typing, and no speech gate).
"""
from __future__ import annotations

import csv
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..media import NO_WINDOW, _need

log = logging.getLogger("reelvault.signals.sound")

SR = 16000
WIN_S, HOP_S = 1.0, 0.25
DSP_HOP_S, N_FFT = 0.01, 1024

# AudioSet display names -> sound-design groups (score = max over members). 6.4 step 2.
GROUPS: dict[str, list[str]] = {
    "whoosh": ["Whoosh, swoosh, swish", "Whip", "Zing"],
    "impact": ["Thump, thud", "Thunk", "Bang", "Boom", "Slam", "Smash, crash", "Whack, thwack", "Clang",
               "Explosion"],
    "pop": ["Burst, pop"],
    "click": ["Clicking", "Tick", "Clickety-clack", "Finger snapping"],
    "ding": ["Ding", "Ping", "Bell", "Chime", "Ding-dong", "Glockenspiel"],
    "camera_shutter": ["Camera", "Single-lens reflex camera"],
    "typing": ["Typing", "Computer keyboard", "Typewriter"],
    "beep": ["Beep, bleep", "Sine wave", "Chirp tone"],
    "cash_register": ["Cash register", "Coin (dropping)"],
    "laughter": ["Laughter", "Giggle", "Chuckle, chortle", "Belly laugh", "Snicker"],
    "applause": ["Applause", "Cheering", "Clapping"],
    "record_scratch": ["Scratching (performance technique)", "Scratch"],
    "sound_effect": ["Sound effect"],
    "speech": ["Speech", "Male speech, man speaking", "Female speech, woman speaking", "Child speech, kid speaking",
               "Narration, monologue", "Conversation", "Speech synthesizer"],
    "speech_synth": ["Speech synthesizer"],
    "music": ["Music", "Pop music", "Hip hop music", "Electronic music", "Rock music", "Ambient music",
              "Background music", "Theme music", "Soundtrack music", "Dance music", "Electronic dance music",
              "House music", "Techno", "Dubstep", "Trap music", "Piano", "Keyboard (musical)", "Synthesizer",
              "Drum machine", "Drum", "Bass drum", "Guitar", "Musical instrument", "Song", "Funk", "Jazz",
              "Classical music", "Lo-fi music" ],
    "crowd": ["Crowd", "Chatter", "Hubbub, speech noise, speech babble", "Children shouting"],
    "ambient": ["Traffic noise, roadway noise", "Wind", "Wind noise (microphone)", "Inside, small room",
                "Outside, urban or manmade", "Outside, rural or natural", "Vehicle", "Rain"],
}
SPAN_GROUPS = ("speech", "speech_synth", "music", "crowd", "ambient", "laughter", "applause")  # long, not SFX
SFX_GROUPS = tuple(g for g in GROUPS if g not in SPAN_GROUPS)
MUSIC_ON = 0.35
SPEECH_ON = 0.3
SPEECH_GATE_SHARE = 0.10
SFX_CAP = 0.15                                           # adaptive threshold cap (6.4 step 2)


def sfx_floor() -> float:
    return float(os.environ.get("SFX_FLOOR", "0.03"))


def sfx_rel() -> float:
    return float(os.environ.get("SFX_REL", "6"))


# ============================================================================================== audio in

def load_audio(path: Path, sr: int = SR, *, timeout: float = 300) -> np.ndarray:
    """Decode the audio track to mono float32 at `sr` (empty array when there is none)."""
    p = subprocess.run([_need("ffmpeg"), "-v", "error", "-i", str(path), "-vn", "-f", "f32le", "-ac", "1",
                        "-ar", str(sr), "-"], capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    return np.frombuffer(p.stdout, np.float32).copy()


_EBUR_I = re.compile(r"I:\s*(-?[0-9.]+|-inf)\s*LUFS")


def integrated_lufs(path: Path, *, timeout: float = 300) -> float | None:
    """Integrated loudness from ffmpeg's ebur128 summary (None when silent or no audio)."""
    p = subprocess.run([_need("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
                        "ebur128=framelog=quiet", "-f", "null", "-"], capture_output=True, text=True,
                       timeout=timeout, creationflags=NO_WINDOW)
    found = _EBUR_I.findall(p.stderr)
    if not found or found[-1] == "-inf":
        return None
    v = float(found[-1])
    return round(v, 1) if v > -70 else None


# ============================================================================================== CED tagger

def model_dir() -> Path:
    return Path(os.environ.get("RV_MODELS_DIR", "/opt/reelvault/models")) / "ced-tiny"


def model_available() -> bool:
    d = model_dir()
    return (d / "model.int8.onnx").is_file() and (d / "class_labels_indices.csv").is_file()


class CED:
    """sherpa-onnx CED-tiny audio tagger (527 AudioSet classes). One instance per thread (see tagger())."""

    def __init__(self, directory: Path, threads: int = 1):
        import sherpa_onnx

        cfg = sherpa_onnx.AudioTaggingConfig(
            model=sherpa_onnx.AudioTaggingModelConfig(ced=str(directory / "model.int8.onnx"), num_threads=threads,
                                                      provider="cpu"),
            labels=str(directory / "class_labels_indices.csv"), top_k=527)
        self.tagger = sherpa_onnx.AudioTagging(cfg)
        with open(directory / "class_labels_indices.csv", newline="") as f:
            self.labels = [r["display_name"] for r in csv.DictReader(f)]
        idx = {n: i for i, n in enumerate(self.labels)}
        self.groups = {g: [idx[n] for n in names if n in idx] for g, names in GROUPS.items()}

    def probs(self, chunk: np.ndarray) -> np.ndarray:
        st = self.tagger.create_stream()
        st.accept_waveform(SR, chunk)
        p = np.zeros(len(self.labels), np.float32)
        for ev in self.tagger.compute(st, top_k=len(self.labels)):
            p[ev.index] = ev.prob
        return p


_local = threading.local()


def tagger() -> CED | None:
    """The calling thread's CED instance (the media pool shares nothing mutable between threads), or None."""
    if not model_available():
        return None
    key = str(model_dir())
    got = getattr(_local, "ced", None)
    if got is None or got[0] != key:
        try:
            got = (key, CED(model_dir()))
        except Exception:  # noqa: BLE001
            log.exception("could not load the CED sound model; using DSP only")
            return None
        _local.ced = got
    return got[1]


def windows(x: np.ndarray, win_s: float = WIN_S, hop_s: float = HOP_S) -> tuple[np.ndarray, list[np.ndarray]]:
    win, hop = int(win_s * SR), int(hop_s * SR)
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    starts = list(range(0, len(x) - win + 1, hop))
    return np.array([s / SR for s in starts]), [x[s:s + win] for s in starts]


def tag_windows(model: CED, x: np.ndarray) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Window start times and, per group, the max class probability per window."""
    t0, chunks = windows(x)
    P = np.stack([model.probs(c) for c in chunks]) if chunks else np.zeros((0, len(model.labels)), np.float32)
    groups = {g: (P[:, idx].max(1) if idx else np.zeros(len(t0), np.float32)) for g, idx in model.groups.items()}
    return t0, groups


# ============================================================================================== DSP layer

def stft_feats(x: np.ndarray, hop_s: float = DSP_HOP_S, n_fft: int = N_FFT) -> dict:
    hop = int(hop_s * SR)
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    n = 1 + (len(x) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    raw = x[idx]
    frames = raw * np.hanning(n_fft)[None, :]
    mag = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1 / SR)
    rms = np.sqrt((frames ** 2).mean(1) + 1e-12)
    p2 = mag ** 2
    logm = np.log1p(mag * 10)
    return {
        "t": (np.arange(n) * hop + n_fft / 2) / SR,
        "db": 20 * np.log10(rms + 1e-9),
        "pk": 20 * np.log10(np.abs(raw).max(1) + 1e-9),
        "centroid": (mag * freqs).sum(1) / (mag.sum(1) + 1e-9),
        "flux": np.maximum(0, np.diff(logm, axis=0, prepend=logm[:1])).sum(1),
        "hf": 10 * np.log10(p2[:, (freqs >= 3000) & (freqs <= 12000)].sum(1) + 1e-10),
    }


def segments(mask: np.ndarray, t: np.ndarray, min_len: float, gap: float = 0.0) -> list[list[float]]:
    segs, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        if (not m or i == len(mask) - 1) and start is not None:
            end = i if not m else i + 1
            segs.append([float(t[start]), float(t[min(end, len(t) - 1)])])
            start = None
    merged: list[list[float]] = []
    for s in segs:
        if merged and s[0] - merged[-1][1] <= gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    return [s for s in merged if s[1] - s[0] >= min_len]


def _moving(x: np.ndarray, k: int) -> np.ndarray:
    return np.convolve(x, np.ones(k) / k, mode="same")


def _rolling_median(x: np.ndarray, half: int, step: int = 10) -> np.ndarray:
    """Median over +-half samples, evaluated every `step` samples and interpolated (fast enough for 5 min)."""
    if len(x) == 0:
        return x
    pts = np.arange(0, len(x), step)
    med = np.array([np.median(x[max(0, i - half):i + half + 1]) for i in pts])
    return np.interp(np.arange(len(x)), pts, med)


def dsp_events(F: dict) -> list[dict]:
    """silence, hit, whoosh, riser (6.4 step 3). Times in seconds."""
    t, db, pk, cen, flux, hf = F["t"], F["db"], F["pk"], F["centroid"], F["flux"], F["hf"]
    out: list[dict] = []
    if len(t) < 3:
        return out
    # silence: peak level < -39 dBFS for >= 0.4 s
    for a, b in segments(pk < -39.0, t, min_len=0.4, gap=0.05):
        out.append({"label": "silence", "t": a, "t_end": b, "source": "dsp", "strength": 1.0})
    # hit: spectral-flux peak > 2.5 x local median (+ a little spread), level > -25 dBFS, 80 ms refractory
    med = _rolling_median(flux, 50)
    thr = med * 2.5 + flux.std() * 0.5
    last = -1e9
    for i in range(1, len(flux) - 1):
        if flux[i] > thr[i] and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1] and pk[i] > -25:
            if t[i] - last >= 0.08:
                out.append({"label": "hit", "t": float(t[i]), "t_end": float(t[i] + 0.05), "source": "dsp",
                            "strength": float(min(1.0, flux[i] / (thr[i] * 2 + 1e-9))),
                            "ratio": float(flux[i] / (thr[i] + 1e-9)), "level": float(pk[i])})
                last = t[i]
    # whoosh: 3-12 kHz band energy rises >= 12 dB over >= 100 ms; total 0.25-1.6 s
    shf = _moving(hf, 5)
    base = _rolling_median(shf, 150)
    for a, b in segments(shf > base + 12, t, min_len=0.25, gap=0.05):
        if b - a > 1.6:
            continue
        ia, ib = int(np.searchsorted(t, a)), int(np.searchsorted(t, b))
        if ib <= ia:
            continue
        ipk = ia + int(np.argmax(shf[ia:ib]))
        # a whoosh swells: the raw band energy needs >= 80 ms to climb from 12 dB below its peak to 1 dB below it;
        # a hit's bright crack jumps there in one or two 10 ms frames
        jpk = ia + int(np.argmax(hf[ia:ib]))
        lo = jpk
        while lo > max(0, ia - 30) and hf[lo] > hf[jpk] - 12:
            lo -= 1
        hi_ = lo
        while hi_ < jpk and hf[hi_] < hf[jpk] - 1:
            hi_ += 1
        if t[hi_] - t[lo] < 0.08:
            continue
        if t[ipk] - a >= 0.1 and (b - t[ipk]) >= 0.08:
            out.append({"label": "whoosh", "t": a, "t_end": b, "t_peak": float(t[ipk]), "source": "dsp",
                        "strength": float(min(0.9, (shf[ipk] - base[ipk]) / 30))})
    # riser: 1.5-3.5 s where smoothed loudness AND centroid climb (corr > 0.9 and > 0.8), rise >= 10 dB
    sdb, scen = _moving(db, 25), _moving(cen, 25)
    rising = np.zeros(len(t), bool)
    for win in (150, 250, 350):
        tt = np.arange(win)
        for i in range(0, len(t) - win, 10):
            seg_db, seg_c = sdb[i:i + win], scen[i:i + win]
            if seg_db[-1] - seg_db[0] < 10 or seg_db[-1] < -30:
                continue
            if np.std(seg_c) < 1e-6:
                continue
            if np.corrcoef(tt, seg_db)[0, 1] > 0.9 and np.corrcoef(tt, seg_c)[0, 1] > 0.8:
                rising[i:i + win] = True
    for a, b in segments(rising, t, min_len=1.5, gap=0.1):
        # trim to where the climb really ends: the loudest point of the span
        ia, ib = int(np.searchsorted(t, a)), int(np.searchsorted(t, b))
        b2 = float(t[ia + int(np.argmax(sdb[ia:ib]))]) if ib > ia else b
        b = b2 if b2 - a >= 1.5 else b
        # and where it really starts: a hinge fit (flat floor, then a straight climb in dB) on lightly smoothed
        # loudness; the breakpoint is the start. The 25-frame smoothing used for detection blurs it by ~0.15 s.
        a = _hinge_start(t, _moving(db, 5), a, b)
        if b - a <= 4.0:
            out.append({"label": "riser", "t": a, "t_end": b, "source": "dsp", "strength": 0.6})
    return out


def _hinge_start(t: np.ndarray, db: np.ndarray, a: float, b: float) -> float:
    i0, i1 = int(np.searchsorted(t, a - 0.6)), int(np.searchsorted(t, b))
    if i1 - i0 < 40:
        return a
    tt, yy = t[i0:i1], db[i0:i1]
    # a riser that enters with an audible step: the biggest 50 ms jump (>= 3 dB) near the detected start
    j0, j1 = int(np.searchsorted(tt, a - 0.3)), int(np.searchsorted(tt, a + 0.5))
    if j1 - j0 > 6:
        jumps = yy[j0 + 5:j1] - yy[j0:j1 - 5]
        k = int(np.argmax(jumps))
        if jumps[k] >= 3.0:
            seg = yy[j0 + k:j0 + k + 6]
            return float(tt[j0 + k + int(np.argmax(np.diff(seg)))]) if len(seg) > 1 else float(tt[j0 + k])
    best, best_err = a, np.inf
    for ts in np.arange(a - 0.3, min(a + 0.8, b - 1.2), 0.01):
        pre, post = tt < ts, tt >= ts
        if pre.sum() < 10 or post.sum() < 50:
            continue
        floor = float(np.median(yy[pre]))
        x = tt[post] - ts
        # climb from the floor at ts: least squares slope through (0, floor)
        slope = float(((yy[post] - floor) * x).sum() / max(1e-9, (x * x).sum()))
        err = float(np.abs(yy[pre] - floor).sum() + np.abs(yy[post] - (floor + slope * x)).sum())
        if slope > 0 and err < best_err:
            best, best_err = float(ts), err
    return best


def loudness_windows(x: np.ndarray, win_s: float = 0.5) -> list[float]:
    """RMS dBFS per 0.5 s window."""
    n = int(win_s * SR)
    if len(x) == 0:
        return []
    out = []
    for s in range(0, len(x), n):
        seg = x[s:s + n]
        out.append(round(float(20 * np.log10(np.sqrt(np.mean(seg ** 2) + 1e-12) + 1e-9)), 1))
    return out


# ============================================================================================== analysis result

@dataclass
class SoundRaw:
    duration: float
    has_audio: bool
    detector: str                                    # "ced-tiny-int8+dsp" | "dsp" | "none"
    t0: np.ndarray = field(default_factory=lambda: np.zeros(0))       # tagger window starts
    groups: dict = field(default_factory=dict)       # group -> scores per window
    dsp: list = field(default_factory=list)
    feats: dict = field(default_factory=dict)
    loudness: list = field(default_factory=list)
    lufs: float | None = None
    audio: np.ndarray | None = None

    def share_over(self, group: str, thr: float) -> float:
        s = self.groups.get(group)
        if s is None or len(s) == 0:
            return 0.0
        return float((s >= thr).mean())

    def speech_likely(self) -> bool | None:
        """ASR gate (6.1): speech group >= 0.3 in >= 10% of windows. None = unknown (no tagger)."""
        if not self.has_audio:
            return False
        if not self.groups:
            return None
        return self.share_over("speech", SPEECH_ON) >= SPEECH_GATE_SHARE

    def music_spans(self) -> list[list[float]]:
        s = self.groups.get("music")
        if s is None or len(s) == 0:
            return []
        on = s >= MUSIC_ON
        spans: list[list[float]] = []
        for i, m in enumerate(on):
            if not m:
                continue
            a, b = float(self.t0[i]), float(min(self.duration, self.t0[i] + WIN_S))
            if spans and a <= spans[-1][1] + 0.01:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        return [[round(a, 2), round(b, 2)] for a, b in spans if b - a >= 1.0]

    def level_db(self, a: float, b: float, pct: float = 50) -> float | None:
        """Percentile of the 10 ms RMS level between a and b (dBFS)."""
        t, db = self.feats.get("t"), self.feats.get("db")
        if t is None or len(t) == 0:
            return None
        m = (t >= a) & (t < b)
        if not m.any():
            return None
        return float(np.percentile(db[m], pct))


def analyze(path: Path, *, use_model: bool = True, keep_audio: bool = False) -> SoundRaw:
    """Decode + tag + DSP. Never raises for a missing or silent audio track."""
    x = load_audio(path)
    dur = len(x) / SR
    if len(x) < int(0.2 * SR) or float(np.abs(x).max(initial=0)) < 1e-5:
        return SoundRaw(duration=dur, has_audio=len(x) > 0, detector="none", audio=x if keep_audio else None,
                        loudness=loudness_windows(x))
    F = stft_feats(x)
    raw = SoundRaw(duration=dur, has_audio=True, detector="dsp", dsp=dsp_events(F), feats=F,
                   loudness=loudness_windows(x), audio=x if keep_audio else None)
    model = tagger() if use_model else None
    if model is not None:
        raw.t0, raw.groups = tag_windows(model, x)
        raw.detector = "ced-tiny-int8+dsp"
    try:
        raw.lufs = integrated_lufs(path)
    except Exception:  # noqa: BLE001
        raw.lufs = None
    return raw


# ============================================================================================== fusion (6.4 step 4)

def adaptive_threshold(scores: np.ndarray) -> float:
    if len(scores) == 0:
        return SFX_CAP
    return min(SFX_CAP, max(sfx_floor(), sfx_rel() * float(np.median(scores))))


def ml_events(raw: SoundRaw) -> list[dict]:
    """Runs of windows above each SFX group's adaptive threshold, plus laughter/applause/crowd spans."""
    out: list[dict] = []
    if not raw.groups:
        return out
    t0 = raw.t0
    for g in SFX_GROUPS + ("laughter", "applause", "crowd"):
        s = raw.groups.get(g)
        if s is None or len(s) == 0:
            continue
        thr = adaptive_threshold(s) if g in SFX_GROUPS else 0.3
        mask = s >= thr
        i = 0
        while i < len(mask):
            if not mask[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(mask) and mask[j + 1]:
                j += 1
            if g in SFX_GROUPS:   # a short SFX lies in the overlap of the windows that heard it
                a, b = float(t0[j]), float(t0[i] + WIN_S)
                if b <= a:
                    mid = float(t0[i] + (t0[j] - t0[i]) / 2)
                    a, b = mid, mid + WIN_S
            else:
                a, b = float(t0[i]), float(t0[j] + WIN_S)
            peak = float(s[i:j + 1].max())
            out.append({"label": g, "t": a, "t_end": min(b, raw.duration), "source": "ml", "score": peak,
                        "ratio": peak / thr})
            i = j + 1
    return out


def isolated(onsets: list[float], gap: float = 0.15) -> list[float]:
    """Transients with no other transient within +-gap (drum hits; not key clicks in a typing run)."""
    ts = sorted(onsets)
    out = []
    for i, t in enumerate(ts):
        if (i == 0 or t - ts[i - 1] > gap) and (i == len(ts) - 1 or ts[i + 1] - t > gap):
            out.append(t)
    return out


def beat_period(onsets: list[float]) -> float | None:
    """The music's beat (or bar) period: the most common pairwise gap in 0.25-4.2 s (10 ms bins) between isolated
    transients, or None when no gap repeats often enough. Bars count too: a bright pad changes chord every bar."""
    ts = np.array(isolated(onsets))
    if len(ts) < 6:
        return None
    d = (ts[None, :] - ts[:, None]).ravel()
    d = d[(d >= 0.25) & (d <= 4.2)]
    if not len(d):
        return None
    hist, edges = np.histogram(d, bins=np.arange(0.25, 4.21, 0.01))
    sm = np.convolve(hist, [1, 2, 1], mode="same")
    k = int(np.argmax(sm))
    lo, hi = max(0, k - 1), min(len(hist), k + 2)
    w = hist[lo:hi]
    if w.sum() < max(4, 0.3 * len(ts)):
        return None
    centers = (edges[lo:hi] + edges[lo + 1:hi + 1]) / 2
    return float((w * centers).sum() / max(1, w.sum()))


def grid_phase(onsets: list[float], period: float | None) -> float | None:
    """Phase (0-1) of the beat grid: where most isolated transients fall within the period, or None."""
    if not period:
        return None
    ts = np.array(isolated(onsets))
    if len(ts) < 6:
        return None
    ph = (ts / period) % 1.0
    hist, edges = np.histogram(ph, bins=20, range=(0, 1))
    k = int(np.argmax(hist + np.roll(hist, 1) + np.roll(hist, -1)))
    if (hist[k] + hist[(k - 1) % 20] + hist[(k + 1) % 20]) < 0.4 * len(ts):
        return None
    sel = np.abs(((ph - (k + 0.5) / 20) + 0.5) % 1.0 - 0.5) <= 0.075
    return float(np.angle(np.exp(2j * np.pi * ph[sel]).mean()) / (2 * np.pi) % 1.0)


def on_beat_grid(t: float, onsets: list[float], period: float | None, tol: float = 0.04,
                 phase: float | None = None) -> bool:
    """True when t sits on the beat grid: one beat (or two) away from other isolated transients, or (with `phase`)
    on the grid's beat or half-beat phase."""
    if not period:
        return False
    if phase is not None:
        d = ((t / period - phase) * 2) % 1.0          # half-beat grid (off-beat hats count)
        if min(d, 1 - d) * period / 2 <= tol:
            return True
    ts = np.array(isolated(onsets))
    if not len(ts):
        return False

    def near(u: float) -> bool:
        return bool(np.any(np.abs(ts - u) <= tol))

    p = period
    return (near(t - p) and near(t + p)) or (near(t - p) and near(t - 2 * p)) or (near(t + p) and near(t + 2 * p))


RELIABLE = {"ding", "typing", "camera_shutter", "cash_register", "click", "laughter", "applause"}
ONSET_TOL = 0.12


def _in_spans(t: float, spans: list) -> bool:
    return any(a <= t <= b for a, b in spans)


def fuse(raw: SoundRaw, speech_spans: list | None = None) -> list[dict]:
    """Final event list: {"t", "t_end", "label", "source", "confidence", "score", (+ "t_peak")}.

    - ML SFX events must agree with a DSP onset within 120 ms; the time is the DSP onset. Every DSP onset inside a
      confirmed ML event is explained by it (key clicks in a typing run). Unconfirmed ML SFX are kept only when
      strong (score >= the 0.15 cap) and their label is reliable.
    - Weak events on the music's beat grid (+-40 ms) are its drums: dropped. A transient on the grid is kept when it
      is clearly stronger (flux >= 2x) than the grid's typical drum hit.
    - Within any 0.2 s, keep the best score/threshold ratio.
    - DSP whoosh and riser are kept (ml+dsp when the tagger agrees; ML whoosh/"sound effect" overlapping them merge).
    - DSP-only transients become `hit` with low confidence, but never inside speech (plosives) or inside a sweep.
    """
    dsp = raw.dsp
    hits = [d for d in dsp if d["label"] == "hit"]
    hit_times = [h["t"] for h in hits]
    lone = set(isolated(hit_times))
    period = beat_period(hit_times)
    phase = grid_phase(hit_times, period)
    grid = [h for h in hits if h["t"] in lone and on_beat_grid(h["t"], hit_times, period, phase=phase)]
    drum_ratio = float(np.median([h["ratio"] for h in grid])) if grid else 0.0

    def weak_on_beat(h: dict) -> bool:
        """Part of the music: an isolated transient on the beat grid no louder than its drums, or one of a run of
        similar-strength transients repeating every 1, 2, 4 or 8 beats (bar-start chord changes and accents).
        Transients inside a dense run (typing, foley) are never 'music'."""
        if not period or h["t"] not in lone:
            return False
        if on_beat_grid(h["t"], hit_times, period, phase=phase) and h["ratio"] < 2.0 * drum_ratio:
            return True
        similar = [x["t"] for x in hits if 0.5 * h["ratio"] <= x["ratio"] <= 2.0 * h["ratio"]]
        return any(on_beat_grid(h["t"], similar, period * m, tol=0.04 + 0.01 * m) for m in (1, 2, 4, 8))

    speech = list(speech_spans or [])
    sg = raw.groups.get("speech")
    if sg is not None and len(sg):
        speech += [[float(raw.t0[i]) + 0.25, float(raw.t0[i]) + 0.75] for i in np.flatnonzero(sg >= SPEECH_ON)]
    sweeps = [d for d in dsp if d["label"] in ("whoosh", "riser")]
    ml = ml_events(raw)
    spans = [e for e in ml if e["label"] in ("laughter", "applause", "crowd")]
    cand: list[dict] = []
    explained: set[int] = set()
    for e in (x for x in ml if x["label"] in SFX_GROUPS):
        if e["label"] in ("whoosh", "sound_effect"):
            match = [s for s in sweeps if s["t"] - ONSET_TOL <= e["t_end"] and s["t_end"] + ONSET_TOL >= e["t"]]
            if match:
                for s in match:
                    if e["label"] == "whoosh" or s["label"] == "whoosh":
                        s["ml"] = max(s.get("ml", 0.0), e["score"])
                continue
        inside = [(k, h) for k, h in enumerate(hits) if e["t"] - ONSET_TOL <= h["t"] <= e["t_end"] + ONSET_TOL]
        if inside:
            k, h = max(inside, key=lambda kh: kh[1]["ratio"])
            if e["score"] < SFX_CAP and weak_on_beat(h):
                continue
            explained.update(kk for kk, _ in inside)
            label = "hit" if e["label"] == "impact" else e["label"]
            cand.append({"t": h["t"], "t_end": max(h["t"] + 0.05, min(e["t_end"], h["t"] + 1.0)), "label": label,
                         "source": "ml+dsp", "score": e["score"], "ratio": e["ratio"]})
        elif e["score"] >= SFX_CAP and e["label"] in RELIABLE:
            cand.append({"t": e["t"], "t_end": e["t_end"], "label": e["label"], "source": "ml", "score": e["score"],
                         "ratio": e["ratio"]})
    cand.sort(key=lambda c: -c["ratio"])
    chosen: list[dict] = []
    for c in cand:
        if not any(abs(c["t"] - o["t"]) < 0.2 for o in chosen):
            chosen.append(c)
    out = []
    for c in chosen:
        conf = "high" if (c["source"] == "ml+dsp" and (c["label"] in RELIABLE or c["ratio"] >= 1.5)) else "low"
        out.append({"t": c["t"], "t_end": c["t_end"], "label": c["label"], "source": c["source"],
                    "confidence": conf, "score": round(c["score"], 3)})
    for s in sweeps:
        ev = {"t": s["t"], "t_end": s["t_end"], "label": s["label"],
              "source": "ml+dsp" if s.get("ml") else "dsp",
              "confidence": "high" if s.get("ml") or s["label"] == "riser" or s["strength"] >= 0.5 else "low",
              "score": round(float(s.get("ml", s["strength"])), 3)}
        if "t_peak" in s:
            ev["t_peak"] = s["t_peak"]
        out.append(ev)
    sweep_spans = [(s["t"], s["t_end"]) for s in sweeps]
    for k, h in enumerate(hits):
        if k in explained or weak_on_beat(h):
            continue
        if any(abs(h["t"] - o["t"]) < 0.2 for o in out):
            continue
        if any(a - 0.05 <= h["t"] <= b - 0.05 for a, b in sweep_spans):
            continue   # a transient inside a whoosh/riser is part of that sound (a hit AT a riser's end is kept)
        if _in_spans(h["t"], speech):
            continue   # plosives and syllable onsets
        out.append({"t": h["t"], "t_end": h["t_end"], "label": "hit", "source": "dsp", "confidence": "low",
                    "score": round(h["strength"], 3)})
    for s in spans:
        out.append({"t": s["t"], "t_end": s["t_end"], "label": s["label"], "source": "ml",
                    "confidence": "high" if s["score"] >= 0.5 else "low", "score": round(s["score"], 3)})
    for e in out:
        e["t"], e["t_end"] = round(float(e["t"]), 3), round(float(e["t_end"]), 3)
    return sorted(out, key=lambda e: (e["t"], e["label"]))
