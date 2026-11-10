"""Measure this host and print the media settings to use (DESIGN-SAAS.md 12.5 "Sizing"). WP1.

    python -m reelvault.server.bench                    # ~2-4 minutes: synthetic 40 s reel, whisper small + base
    python -m reelvault.server.bench --video reel.mp4   # use a real reel (best: one with speech)
    python -m reelvault.server.bench --quick            # ~20 s smoke run (tiny model, short clip), numbers are rough
    python -m reelvault.server.bench --json             # machine-readable

In the Docker image:  docker compose exec app python -m reelvault.server.bench

What it times, per reel, the way the media pool does it (6.1-6.4):
  * the full `reelvault.signals.process()` pipeline when WP3's code is installed, else its parts:
    probe, cut detection (ffmpeg scene filter), frame grabs, audio decode, and faster-whisper
    (`small` + word times for priority reels, `base` without word times for sample reels; int8, beam 1, 2 threads);
  * then the same work on all media lanes at once (WHISPER_CONCURRENCY), because lanes share the CPU.
It prints seconds per reel, reels per day, and the suggested WHISPER_CONCURRENCY and SERVER_MEDIA_PER_DAY
(half of the measured capacity, like the default). Nothing is written to the database.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PRIORITY_SHARE = 15 / 40   # one pull: ~15 priority + 25 sample reels (12.5)


def _run(cmd: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def make_clip(path: Path, seconds: int) -> Path:
    """Synthetic 1080x1920 reel: 4 scenes (hard cuts), tone + noise audio with a silent gap."""
    seg = max(1, seconds // 4)
    srcs = ["testsrc2", "smptebars", "rgbtestsrc", "mandelbrot"]
    args = ["ffmpeg", "-v", "error", "-y"]
    for s in srcs:
        args += ["-f", "lavfi", "-t", str(seg), "-i", f"{s}=size=1080x1920:rate=30"]
    args += ["-f", "lavfi", "-t", str(seg * 4), "-i",
             "aevalsrc='0.3*sin(2*PI*220*t)*gt(mod(t,7),1)+0.05*(random(0)-0.5)':s=16000"]
    args += ["-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]", "-map", "[v]", "-map", "4:a",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    res = _run(args)
    if res.returncode != 0 or not path.exists():
        raise SystemExit("ffmpeg could not make the test clip: " + res.stderr[-400:])
    return path


def probe_seconds(video: Path) -> float:
    res = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)])
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


class Whisper:
    """Loads each model once (like the media pool) and shares it across lanes."""

    def __init__(self) -> None:
        self.models: dict[str, object] = {}
        self.lock = threading.Lock()
        self.load_s: dict[str, float] = {}

    def get(self, name: str):
        with self.lock:
            if name not in self.models:
                from faster_whisper import WhisperModel

                t = time.perf_counter()
                self.models[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=2)
                self.load_s[name] = round(time.perf_counter() - t, 1)
            return self.models[name]

    def transcribe(self, name: str, pcm: Path, words: bool) -> int:
        import numpy as np

        model = self.get(name)
        # raw 16 kHz mono s16le from ffmpeg -> float32; avoids PyAV version quirks in faster_whisper.decode_audio
        audio = np.frombuffer(pcm.read_bytes(), dtype=np.int16).astype(np.float32) / 32768.0
        segs, _info = model.transcribe(audio, beam_size=1, vad_filter=False,  # type: ignore[attr-defined]
                                       word_timestamps=words)
        return sum(1 for _ in segs)


def parts_pipeline(video: Path, work: Path, whisper: Whisper | None, model: str | None, words: bool) -> dict:
    """The media work of one reel, step by step. Returns seconds per step."""
    t: dict[str, float] = {}
    t0 = time.perf_counter()
    _run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(video)])
    t["probe"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    _run(["ffmpeg", "-v", "error", "-i", str(video), "-an", "-vf", "scale=270:-2,select='gt(scene,0.3)',showinfo",
          "-f", "null", "-"])
    t["cuts"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    frames = work / "frames"
    frames.mkdir(exist_ok=True)
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", "fps=1/3,scale=768:-2", "-q:v", "4",
          str(frames / "f%03d.jpg")])
    t["frames"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    wav = work / "audio.s16le"
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-ac", "1", "-ar", "16000", "-f", "s16le", str(wav)])
    t["audio"] = time.perf_counter() - t0

    if whisper is not None and model and wav.exists():
        t0 = time.perf_counter()
        whisper.transcribe(model, wav, words)
        t["whisper"] = time.perf_counter() - t0
    return {k: round(v, 2) for k, v in t.items()}


_FULL: dict = {}


def signals_pipeline():
    """WP3's full pipeline `signals.process(video, out_dir, *, ig_audio, words, whisper_model)`, or None while it is
    not built (a stub raising NotImplementedError) or not importable."""
    if "fn" not in _FULL:
        try:
            from reelvault import signals

            _FULL["fn"] = getattr(signals, "process", None)
        except Exception:  # noqa: BLE001
            _FULL["fn"] = None
    return _FULL["fn"]


def one_reel(kind: str, video: Path, base: Path, whisper: Whisper | None, models: dict, n: int) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"{kind}{n}-", dir=base))
    model = models["priority"] if kind == "priority" else models["sample"]
    full = signals_pipeline()
    if full is not None:
        t0 = time.perf_counter()
        try:
            # --no-whisper (or no model): the pipeline runs with an empty transcript instead of speech-to-text
            kw = {} if whisper is not None else {"transcribe": lambda audio, name, words: {}}
            sig = full(video, work, ig_audio=None, words=(kind == "priority"), whisper_model=model, **kw)
            steps = dict(getattr(sig, "timings", None) or {})
            steps.setdefault("audio", steps.get("sound", 0.0))   # same step names as the parts timing
            return {"seconds": round(time.perf_counter() - t0, 2), "via": "signals.process", "steps": steps}
        except NotImplementedError:
            _FULL["fn"] = None
        except Exception as e:  # noqa: BLE001
            print(f"  (signals.process failed: {type(e).__name__}: {e}; timing the parts instead)", file=sys.stderr)
            _FULL["fn"] = None
    t0 = time.perf_counter()
    steps = parts_pipeline(video, work, whisper, model, words=(kind == "priority"))
    return {"seconds": round(time.perf_counter() - t0, 2), "via": "parts", "steps": steps}


def measure(video: Path, base: Path, *, lanes: int, models: dict, use_whisper: bool) -> dict:
    whisper = Whisper() if use_whisper else None
    if whisper is not None:
        for m in {models["priority"], models["sample"]}:
            try:
                whisper.get(m)
            except Exception as e:  # noqa: BLE001
                print(f"  (whisper model '{m}' could not load: {e}; skipping speech-to-text)", file=sys.stderr)
                whisper = None
                break
    one_reel("sample", video, base, whisper, models, 0)  # warm-up (disk cache, model first call)
    single = {k: one_reel(k, video, base, whisper, models, 1) for k in ("priority", "sample")}

    results: list[dict] = []
    lock = threading.Lock()

    def lane(i: int) -> None:
        for kind in ("priority", "sample"):
            r = one_reel(kind, video, base, whisper, models, 10 + i)
            with lock:
                results.append({"kind": kind, **r})

    t0 = time.perf_counter()
    threads = [threading.Thread(target=lane, args=(i,)) for i in range(lanes)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wall = time.perf_counter() - t0
    par = {k: round(sum(r["seconds"] for r in results if r["kind"] == k) / max(1, lanes), 2)
           for k in ("priority", "sample")}
    return {"single_lane": single, "all_lanes": {"lanes": lanes, "wall_s": round(wall, 1), "per_reel_s": par},
            "model_load_s": whisper.load_s if whisper else {}, "whisper": whisper is not None}


def suggest(m: dict, *, cpus: int, lanes: int, clip_s: float, ram_gb: float | None, synthetic: bool = False) -> dict:
    per = m["all_lanes"]["per_reel_s"]
    mix = PRIORITY_SHARE * per["priority"] + (1 - PRIORITY_SHARE) * per["sample"]
    mix = max(0.5, mix)
    per_day = int(lanes * 86400 / mix)
    out = {"cpus": cpus, "lanes": lanes, "clip_seconds": round(clip_s, 1),
           "seconds_per_priority_reel": per["priority"], "seconds_per_sample_reel": per["sample"],
           "seconds_per_reel_mixed": round(mix, 1), "reels_per_day_capacity": per_day,
           "suggest": {"WHISPER_CONCURRENCY": lanes, "SERVER_MEDIA_PER_DAY": max(100, per_day // 2)},
           "synthetic": synthetic,
           "one_pull_minutes": round(40 * mix / lanes / 60, 1)}
    if synthetic or not m.get("whisper"):
        # The synthetic clip has no speech (whisper runs much faster on it) and the sound/visual steps may not be
        # timed yet, so never suggest more than the design default measured on real reels (cpus x 1,200).
        out["suggest"]["SERVER_MEDIA_PER_DAY"] = min(out["suggest"]["SERVER_MEDIA_PER_DAY"], cpus * 1200)
        per_day = min(per_day, cpus * 2400)
    # ~5 pulls a week per member x ~40 media reels per pull, at half capacity
    out["members_estimate"] = max(1, int((per_day / 2) * 7 / (5 * 40)))
    if ram_gb is not None:
        need = 0.3 + 1.2 + 0.3 * lanes
        out["ram_gb"] = round(ram_gb, 1)
        out["ram_needed_gb"] = round(need, 1)
        out["ram_ok"] = ram_gb >= need
    return out


def ram_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        return None
    return None


def report(s: dict, m: dict) -> str:
    lines = [
        "ReelVault host check",
        f"  CPUs: {s['cpus']}   media lanes: {s['lanes']}   test reel: {s['clip_seconds']} s",
        f"  Priority reel: {s['seconds_per_priority_reel']} s   sample reel: {s['seconds_per_sample_reel']} s"
        f"   (all lanes busy)",
        f"  One pull (about 15 priority + 25 sample reels): about {s['one_pull_minutes']} minutes",
        f"  Capacity: about {s['reels_per_day_capacity']:,} reels a day",
        f"  Fits about {s['members_estimate']} members at ~5 pulls a week each (half capacity, like the default)",
    ]
    if not m.get("whisper"):
        lines.append("  Note: speech-to-text was not timed (no whisper model). Real reels with speech take longer.")
    if s.get("synthetic"):
        lines.append("  Note: the test reel has no speech, so these times are too low. For real numbers run again")
        lines.append("  with a real reel: --video reel.mp4. The suggestion below is capped at the safe default.")
    if "ram_ok" in s:
        lines.append(f"  RAM: {s['ram_gb']} GB (needs about {s['ram_needed_gb']} GB)"
                     + ("" if s["ram_ok"] else "  <- too little: use fewer lanes or a bigger server"))
    lines += ["", "Put these in your .env (then restart):",
              f"  WHISPER_CONCURRENCY={s['suggest']['WHISPER_CONCURRENCY']}",
              f"  SERVER_MEDIA_PER_DAY={s['suggest']['SERVER_MEDIA_PER_DAY']}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure this host for ReelVault media work.")
    ap.add_argument("--video", help="a real reel to time (default: a synthetic 40 s clip)")
    ap.add_argument("--seconds", type=int, default=40, help="length of the synthetic clip")
    ap.add_argument("--lanes", type=int, default=0, help="media lanes to test (default: CPUs // 2)")
    ap.add_argument("--quick", action="store_true", help="short clip + tiny whisper model (rough smoke run)")
    ap.add_argument("--no-whisper", action="store_true", help="skip speech-to-text")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"{tool} is not installed. Install it first.", file=sys.stderr)
            return 1
    cpus = os.cpu_count() or 2
    lanes = a.lanes or max(1, cpus // 2)
    if a.quick:
        a.seconds = min(a.seconds, 8)
        lanes = min(lanes, 2)
    models = {"priority": "tiny" if a.quick else os.environ.get("WHISPER_MODEL", "small"),
              "sample": "tiny" if a.quick else os.environ.get("WHISPER_SAMPLE_MODEL", "base")}
    base = Path(tempfile.mkdtemp(prefix="rv-bench-"))
    try:
        video = Path(a.video).resolve() if a.video else make_clip(base / "clip.mp4", a.seconds)
        if not video.is_file():
            print(f"Video not found: {video}", file=sys.stderr)
            return 1
        if not a.json:
            print(f"Timing {lanes} lane(s) on {cpus} CPUs. This takes a few minutes...", file=sys.stderr)
        m = measure(video, base, lanes=lanes, models=models, use_whisper=not a.no_whisper)
        s = suggest(m, cpus=cpus, lanes=lanes, clip_s=probe_seconds(video), ram_gb=ram_gb(),
                    synthetic=not a.video)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if a.json:
        print(json.dumps({"summary": s, "detail": m}, indent=2))
    else:
        print(report(s, m))
    return 0


if __name__ == "__main__":
    from .storage import drop_root

    drop_root("reelvault.server.bench", sys.argv[1:])   # never write root-owned files in /data (fix round 2)
    sys.exit(main())
