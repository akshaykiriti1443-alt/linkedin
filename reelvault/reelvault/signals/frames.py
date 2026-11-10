"""Frames and contact sheets for every destination (DESIGN-SAAS.md 6.1, 6.1.1).

Masters (768 px wide JPEG, q=4) are written once by `signals.process` into `<reel_dir>/frames/` together with
`frames/index.json` (the frame list + per-frame face/text numbers). Every image sent anywhere is derived from them:

    jpeg_for(master, dest, *, cap_bytes=None) -> bytes                      # one frame for a destination
    sheet_for(reel_dir, dest, *, cap_bytes=None) -> bytes                   # cached as sheet_<dest>.jpg
    sheet_tiles(reel_dir, dest) -> [{"tile": 1, "n": 0, "t": 0.2, "kind": "hook"}]   # tile number -> time, for prompts
    key_frames(frames, visual) -> [frame]                                   # the 6 tiles of small/mcp sheets
    deep_frames(reel_dir, dest, n) -> [(frame, bytes)]                      # frames for a deep read (6.1.1)

Destinations ("sheet" / "frame" specs from the 6.1.1 table):
    paid_api  sheet 4x3 = 12 tiles 192x341, q4, 160 KB | frame 540x960, q5, 60 KB
    small     sheet 3x2 = 6 key frames 256x456, q5, 120 KB | frame 384x683, q5, 30 KB
    mcp       sheet 3x2 = 6 key frames 160x284 (480x568), q12, 32 KB (28 KB in a deep payload) | frame 384 px long
              edge, q8, 12 KB
When an image is over its cap the code steps down: JPEG quality first, then pixel size, then (sheets) fewer tiles.
It never returns nothing: the smallest attempt is returned and the overshoot is logged.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..media import NO_WINDOW, _need, contact_sheet, cut_frame_times  # noqa: F401  (re-exported)

log = logging.getLogger("reelvault.signals.frames")

INDEX = "index.json"
DESTS = ("paid_api", "small", "mcp")


@dataclass(frozen=True)
class SheetSpec:
    cols: int
    rows: int
    tile_w: int
    tile_h: int
    q: int
    cap: int
    key_only: bool          # True: the 6 key frames; False: every master (up to cols*rows)


@dataclass(frozen=True)
class FrameSpec:
    box_w: int              # fit inside box_w x box_h (portrait box); landscape frames use the same long edge
    box_h: int
    q: int
    cap: int


SHEETS = {
    "paid_api": SheetSpec(4, 3, 192, 341, 4, 160 * 1024, False),
    "small": SheetSpec(3, 2, 256, 456, 5, 120 * 1024, True),
    "mcp": SheetSpec(3, 2, 160, 284, 12, 32 * 1024, True),
}
FRAMES = {
    "paid_api": FrameSpec(540, 960, 5, 60 * 1024),
    "small": FrameSpec(384, 683, 5, 30 * 1024),
    "mcp": FrameSpec(216, 384, 8, 12 * 1024),
}
MCP_DEEP_SHEET_CAP = 28 * 1024
MAX_Q = 31


def _check_dest(dest: str) -> None:
    if dest not in DESTS:
        raise ValueError(f"unknown image destination {dest!r}; use one of {DESTS}")


# ============================================================================================== index

def write_index(reel_dir: Path, frames: list[dict], *, duration: float, per_frame: list[dict] | None = None,
                width: int = 0, height: int = 0) -> None:
    data = {"version": 1, "duration": duration, "width": width, "height": height, "frames": frames,
            "per_frame": per_frame or []}
    p = Path(reel_dir) / "frames" / INDEX
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)
    for old in Path(reel_dir).glob("sheet_*.jpg"):     # new masters: cached sheets are stale
        old.unlink()
    for old in Path(reel_dir).glob("sheet_*.json"):
        old.unlink()


def read_index(reel_dir: Path) -> dict:
    p = Path(reel_dir) / "frames" / INDEX
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            pass
    # no index (older layout): rebuild a minimal one from the file names f00_000.20.jpg
    frames = []
    for f in sorted((Path(reel_dir) / "frames").glob("f*_*.jpg")):
        try:
            n, t = f.stem[1:].split("_", 1)
            frames.append({"n": int(n), "t": float(t), "shot": 0, "kind": "body", "path": f"frames/{f.name}"})
        except ValueError:
            continue
    if frames:
        frames[0]["kind"] = "hook"
        frames[-1]["kind"] = "end"
    dur = (frames[-1]["t"] + 0.5) if frames else 0.0
    return {"version": 0, "duration": dur, "frames": frames, "per_frame": []}


# ============================================================================================== key frames

def _per_frame_map(visual: dict | None) -> dict[int, dict]:
    pf = (visual or {}).get("per_frame") or []
    return {int(x["n"]): x for x in pf if "n" in x}


def best_body_frame(frames: list[dict], visual: dict | None) -> dict | None:
    """The body frame with the largest face, else the most text, else the middle body frame."""
    body = [f for f in frames if f.get("kind") in ("body", "cut")] or frames[1:-1] or frames
    if not body:
        return None
    pf = _per_frame_map(visual)
    if pf:
        def score(f):
            x = pf.get(int(f["n"]), {})
            return (float(x.get("face") or 0.0), float(x.get("text") or 0.0))

        best = max(body, key=score)
        if score(best) > (0.0, 0.0):
            return best
    return body[len(body) // 2]


def key_frames(frames: list[dict], visual: dict | None) -> list[dict]:
    """The 6 tiles of the small/mcp sheets (6.1): hook 0.2 s and 1.0 s, the two cut frames nearest 1/3 and 2/3 of
    the duration, the body frame with the largest face (or most text), and the end frame. Gaps are filled with the
    nearest unused frames; fewer than 6 masters gives fewer tiles. Sorted by time."""
    if not frames:
        return []
    frames = sorted(frames, key=lambda f: f["t"])
    dur = max(f["t"] for f in frames) + 0.5
    chosen: list[dict] = []

    def take(f: dict | None) -> None:
        if f is not None and all(f["n"] != c["n"] for c in chosen):
            chosen.append(f)

    def nearest(t: float, pool: list[dict]) -> dict | None:
        pool = [f for f in pool if all(f["n"] != c["n"] for c in chosen)]
        return min(pool, key=lambda f: abs(f["t"] - t)) if pool else None

    hooks = [f for f in frames if f.get("kind") == "hook"]
    take(nearest(0.2, hooks or frames))
    take(nearest(1.0, hooks or frames))
    cut_pool = [f for f in frames if f.get("kind") == "cut"] or [f for f in frames if f.get("kind") == "body"]
    take(nearest(dur / 3, cut_pool))
    take(nearest(2 * dur / 3, cut_pool))
    rest = [f for f in frames if f.get("kind") in ("body", "cut")]
    take(best_body_frame([f for f in rest if all(f["n"] != c["n"] for c in chosen)] or rest, visual))
    ends = [f for f in frames if f.get("kind") == "end"]
    take(ends[-1] if ends else frames[-1])
    for target in (dur / 2, dur / 6, 5 * dur / 6, dur / 4, 3 * dur / 4):
        if len(chosen) >= 6:
            break
        take(nearest(target, frames))
    return sorted(chosen[:6], key=lambda f: f["t"])


# ============================================================================================== encoding

def _encode(src: Path, dest: Path, *, w: int, h: int, q: int, pad: bool) -> int:
    """Scale src to fit w x h (pad to exactly w x h when pad) and write a JPEG at -q:v q. Returns bytes."""
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,crop='min(iw,{w})':'min(ih,{h})'"
    if pad:
        vf += f",pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
    vf += ",setsar=1"
    subprocess.run([_need("ffmpeg"), "-y", "-loglevel", "error", "-i", str(src), "-vf", vf, "-frames:v", "1",
                    "-q:v", str(q), str(dest)], check=True, creationflags=NO_WINDOW)
    return dest.stat().st_size


def _steps(q0: int) -> list[int]:
    out, q = [], q0
    while q < MAX_Q:
        out.append(q)
        q = q + 2 if q < 12 else q + 4
    out.append(MAX_Q)
    return out


def jpeg_for(master: Path, dest: str, *, cap_bytes: int | None = None) -> bytes:
    """One frame for a destination (6.1.1 'frames' rows): fit the box, quality first, then smaller, within cap."""
    _check_dest(dest)
    spec = FRAMES[dest]
    cap = int(cap_bytes or spec.cap)
    master = Path(master)
    best: bytes | None = None
    scale = 1.0
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "f.jpg"
        while scale >= 0.3:
            w = max(16, int(spec.box_w * scale) // 2 * 2)
            h = max(16, int(spec.box_h * scale) // 2 * 2)
            for q in _steps(spec.q):
                size = _encode(master, out, w=w, h=h, q=q, pad=False)
                data = out.read_bytes()
                if best is None or size < len(best):
                    best = data
                if size <= cap:
                    return data
            scale *= 0.8
    log.warning("jpeg_for: %s for %s stays above %d bytes (%d)", master.name, dest, cap, len(best or b""))
    return best or b""


def sheet_tiles(reel_dir: Path, dest: str, *, index: dict | None = None, cap_bytes: int | None = None) -> list[dict]:
    """Tile number (1-based, reading order) -> frame, for the prompt text ("tile 3 = 4.2 s, cut"). After sheet_for
    ran, this reads the tiles of the sheet actually sent (a step-down may have switched to the 6 key frames)."""
    _check_dest(dest)
    side = _sheet_path(Path(reel_dir), dest, int(cap_bytes or SHEETS[dest].cap)).with_suffix(".json")
    if index is None and side.exists():
        try:
            return json.loads(side.read_text())["tiles"]
        except (OSError, ValueError, KeyError):
            pass
    idx = index or read_index(reel_dir)
    frames = idx.get("frames") or []
    spec = SHEETS[dest]
    chosen = key_frames(frames, {"per_frame": idx.get("per_frame")}) if spec.key_only else \
        sorted(frames, key=lambda f: f["t"])[:spec.cols * spec.rows]
    return [{"tile": i + 1, "n": f["n"], "t": f["t"], "kind": f.get("kind", "body"), "path": f.get("path")}
            for i, f in enumerate(chosen)]


def _sheet_path(reel_dir: Path, dest: str, cap: int) -> Path:
    spec = SHEETS[dest]
    return Path(reel_dir) / (f"sheet_{dest}.jpg" if cap == spec.cap else f"sheet_{dest}_{cap // 1024}k.jpg")


def sheet_for(reel_dir: Path, dest: str, *, cap_bytes: int | None = None, refresh: bool = False) -> bytes:
    """The destination's contact sheet (6.1.1 'sheet' rows), cached next to the frames. Steps down: quality, then
    tile size (to 60%), then (paid_api only) the 6-key-frame 3x2 layout. Missing masters leave black tiles."""
    _check_dest(dest)
    reel_dir = Path(reel_dir)
    spec = SHEETS[dest]
    cap = int(cap_bytes or spec.cap)
    cached = _sheet_path(reel_dir, dest, cap)
    if cached.exists() and not refresh and cached.stat().st_size <= cap:
        return cached.read_bytes()
    idx = read_index(reel_dir)
    tiles = sheet_tiles(reel_dir, dest, index=idx)
    if not tiles:
        raise FileNotFoundError(f"no frames in {reel_dir / 'frames'}")
    layouts = [(spec, tiles)]
    if not spec.key_only:
        small = SHEETS["small"]
        kt = sheet_tiles(reel_dir, "small", index=idx)
        layouts.append((SheetSpec(small.cols, small.rows, spec.tile_w, spec.tile_h, spec.q, cap, True), kt))
    best: tuple[bytes, list[dict]] | None = None
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "s.jpg"
        for lay, tl in layouts:
            for scale in (1.0, 0.9, 0.8, 0.7, 0.6):
                tw = max(32, int(lay.tile_w * scale) // 2 * 2)
                th = max(32, int(lay.tile_h * scale) // 2 * 2)
                imgs = [reel_dir / t["path"] if t.get("path") else None for t in tl]
                for q in _steps(lay.q):
                    contact_sheet(imgs, out, cols=lay.cols, rows=lay.rows, tile_w=tw, tile_h=th, q=q)
                    data = out.read_bytes()
                    if best is None or len(data) < len(best[0]):
                        best = (data, tl)
                    if len(data) <= cap:
                        _save(cached, data, tl)
                        return data
    assert best is not None
    log.warning("sheet_for: %s %s stays above %d bytes (%d)", reel_dir.name, dest, cap, len(best[0]))
    _save(cached, best[0], best[1])
    return best[0]


def _save(path: Path, data: bytes, tiles: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    path.with_suffix(".json").write_text(json.dumps({"tiles": tiles}))


def deep_frames(reel_dir: Path, dest: str, n: int, *, cap_bytes: int | None = None) -> list[tuple[dict, bytes]]:
    """n frames for a deep read, spread over the reel with the hook first (paid_api 6-10, small max_images - 1,
    mcp 2 = 1.0 s hook + best body frame; 6.1.1)."""
    _check_dest(dest)
    idx = read_index(reel_dir)
    frames = sorted([f for f in idx.get("frames") or [] if f.get("path")], key=lambda f: f["t"])
    if not frames or n <= 0:
        return []
    visual = {"per_frame": idx.get("per_frame")}
    if dest == "mcp" and n <= 2:
        pick = [min(frames, key=lambda f: abs(f["t"] - 1.0))]
        b = best_body_frame([f for f in frames if f["n"] != pick[0]["n"]], visual)
        if b is not None and n == 2:
            pick.append(b)
    elif n >= len(frames):
        pick = frames
    else:
        ks = key_frames(frames, visual)
        pick = ks[:n]
        rest = [f for f in frames if all(f["n"] != k["n"] for k in pick)]
        while len(pick) < n and rest:
            # add the frame farthest from everything already picked
            f = max(rest, key=lambda r: min(abs(r["t"] - p["t"]) for p in pick))
            pick.append(f)
            rest.remove(f)
    pick = sorted(pick, key=lambda f: f["t"])
    return [(f, jpeg_for(Path(reel_dir) / f["path"], dest, cap_bytes=cap_bytes)) for f in pick]


def make_default_sheets(reel_dir: Path) -> dict[str, int]:
    """Build and cache every destination's sheet now (end of process_reel). Returns {dest: bytes}."""
    out = {}
    for d in DESTS:
        out[d] = len(sheet_for(reel_dir, d, refresh=True))
    return out

