"""VisualStats: local, free visual measurements for every processed reel (DESIGN-SAAS.md 6.5.1). OpenCV only.

    compute(video, masters, *, duration, cuts, speech_ratio, width, height) -> dict   # VisualStats JSON

Inputs: the 12 master frames (faces, text, UI-likeness, colour) and a 4 fps 160 px wide grayscale stream (motion,
stills, panes, cut-out over still). Faces use YuNet (`<models>/yunet/face_detection_yunet_2023mar.onnx`, score
>= 0.7); without the model every face number is null and the face rules of the local format guess are skipped.

The local format guess (`local_vf`) and `local_recipe` come from rules whose thresholds live in
`reelvault/data/taxonomy.json` -> `local_rules` (WP3 block), so they can be tuned from labelled reels without code.
`DEFAULT_LOCAL_RULES` below is the same table, used when the taxonomy file has none.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

import numpy as np

from ..media import NO_WINDOW, _need

log = logging.getLogger("reelvault.signals.visual")

VERSION = 1
FACE_SCORE = 0.7
STREAM_FPS = 4
STREAM_W = 160
ANALYSIS_W = 480          # masters are analysed at this width
PIX_CHANGE = 12           # a pixel "changed" between two frames when |diff| > 12 (of 255)

# Rules over the VisualStats numbers -> local format guess (6.5.1). Evaluated in this order (= the 10.1 precedence);
# every matching rule adds {"id", "p"}; at most 3. A rule with a missing (null) number never matches.
DEFAULT_LOCAL_RULES: list[dict] = [
    {"id": "VF05", "p": 0.8, "all": [["ui_frames_share", ">=", 0.5]],
     "why": "app or browser UI fills most frames"},
    {"id": "VF06", "p": 0.75, "all": [["split_share", ">=", 0.5]], "why": "two moving panes"},
    {"id": "VF04", "p": 0.7, "all": [["cutout_share", ">=", 0.4]], "why": "cut-out person over a still"},
    {"id": "VF09", "p": 0.7, "all": [["static_frame_share", ">=", 0.6], ["face_share", "<", 0.2]],
     "why": "stills held, no face"},
    {"id": "VF08", "p": 0.6, "all": [["speech_ratio", "<", 0.15], ["text_frames_share", ">=", 0.6],
                                      ["face_share", "<", 0.5]], "why": "big text, no voice"},
    {"id": "VF01", "p": 0.7, "all": [["face_share", ">=", 0.7], ["cutaway_share", "<", 0.2],
                                      ["small_face_corner_share", "<", 0.5]], "why": "one face in most frames"},
    {"id": "VF02", "p": 0.55, "all": [["face_share", ">=", 0.4], ["face_share", "<", 0.8],
                                       ["cutaway_share", ">=", 0.2], ["small_face_corner_share", "<", 0.5]],
     "why": "speaker plus cutaways"},
    {"id": "VF03", "p": 0.6, "all": [["face_share", "<", 0.2], ["speech_ratio", ">=", 0.6]],
     "why": "narration, no face"},
]
DEFAULT_RECIPE_RULES = {"headline_box": ["top_text_box_share", ">=", 0.5],
                        "face_bubble": ["small_face_corner_share", ">=", 0.3]}


def local_rules() -> tuple[list[dict], dict]:
    """(format rules, recipe rules) from taxonomy.json `local_rules`, else the defaults above.

    taxonomy.json shape (a list, design section 10): format rules {"id": "VF05", "p": 0.8, "all": [[stat, op,
    value], ...], "why": "..."} in precedence order, and recipe rules {"recipe": "headline_box", "when": [stat, op,
    value]}."""
    try:
        from .. import taxonomy

        lr = (taxonomy.load() or {}).get("local_rules")
    except Exception:  # noqa: BLE001
        lr = None
    if isinstance(lr, list) and lr:
        fmts = [r for r in lr if isinstance(r, dict) and "id" in r and isinstance(r.get("all"), list)]
        rec = {r["recipe"]: r["when"] for r in lr if isinstance(r, dict) and "recipe" in r
               and isinstance(r.get("when"), list) and len(r["when"]) == 3}
        if fmts:
            return fmts, {**DEFAULT_RECIPE_RULES, **rec}
    return DEFAULT_LOCAL_RULES, DEFAULT_RECIPE_RULES


def rules_json() -> list[dict]:
    """The default table in taxonomy.json form (what WP3 keeps in the local_rules block)."""
    return [*DEFAULT_LOCAL_RULES, *({"recipe": k, "when": v} for k, v in DEFAULT_RECIPE_RULES.items())]


_OPS = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b}


def _cond(stats: dict, c: list) -> bool:
    k, op, v = c
    x = stats.get(k)
    if x is None or op not in _OPS:
        return False
    try:
        return bool(_OPS[op](x, v))
    except TypeError:
        return False


def guess_format(stats: dict, rules: list[dict] | None = None) -> list[dict]:
    rules = rules if rules is not None else local_rules()[0]
    out = []
    for r in rules:
        if all(_cond(stats, c) for c in r.get("all", [])):
            if all(o["id"] != r["id"] for o in out):
                out.append({"id": r["id"], "p": float(r.get("p", 0.5))})
        if len(out) >= 3:
            break
    return out


# ============================================================================================== faces (YuNet)

def yunet_path() -> Path:
    return Path(os.environ.get("RV_MODELS_DIR", "/opt/reelvault/models")) / "yunet" / \
        "face_detection_yunet_2023mar.onnx"


_local = threading.local()


def face_detector():
    """The calling thread's YuNet detector, or None when the model file is missing."""
    p = yunet_path()
    if not p.is_file():
        return None
    got = getattr(_local, "yunet", None)
    if got is None or got[0] != str(p):
        import cv2

        try:
            det = cv2.FaceDetectorYN.create(str(p), "", (320, 320), FACE_SCORE, 0.3, 50)
        except Exception:  # noqa: BLE001
            log.exception("could not load YuNet; face numbers will be empty")
            return None
        got = (str(p), det)
        _local.yunet = got
    return got[1]


def detect_faces(det, img: np.ndarray) -> list[tuple[float, float, float, float, float]]:
    """[(x, y, w, h, score)] in pixels of img, score >= FACE_SCORE."""
    h, w = img.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    if faces is None:
        return []
    out = []
    for f in faces:
        x, y, fw, fh, sc = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])
        if sc >= FACE_SCORE and fw > 4 and fh > 4:
            out.append((max(0.0, x), max(0.0, y), min(fw, w - x), min(fh, h - y), sc))
    return out


# ============================================================================================== text (no OCR)

def text_boxes(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Text-like blocks: morphological gradient + Otsu + a wide closing, then connected components with
    aspect >= 2, height 1.5-12% of the frame and a stroke density typical of letters (classic scene-text cue)."""
    import cv2

    H, W = gray.shape
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if bw.mean() > 0.35 * 255:     # a busy texture, not text
        _, bw = cv2.threshold(grad, 80, 255, cv2.THRESH_BINARY)
    kw = max(5, W // 40)
    closed = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kw, 1)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if h < 0.015 * H or h > 0.12 * H or w < 0.04 * W or w < 2 * h:
            continue
        density = float(bw[y:y + h, x:x + w].mean()) / 255
        fill = area / float(w * h)
        if 0.12 <= density <= 0.75 and fill >= 0.35:
            out.append((int(x), int(y), int(w), int(h)))
    return out


def _union(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1, y1)


# ============================================================================================== UI-likeness

def ui_parts(img: np.ndarray, gray: np.ndarray, boxes: list) -> dict:
    """Numbers behind ui_score: long straight H/V segments, small text boxes, flat few-colour regions, skin share."""
    import cv2

    H, W = gray.shape
    edges = cv2.Canny(gray, 60, 160)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60, minLineLength=int(0.15 * W), maxLineGap=3)
    n_lines = 0
    if lines is not None:
        for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
            if abs(y2 - y1) <= 2 or abs(x2 - x1) <= 2:
                n_lines += 1
    small = sum(1 for b in boxes if b[3] < 0.03 * H)
    # flat regions: local std < 4 over a 5x5 window
    g = gray.astype(np.float32)
    mu = cv2.blur(g, (5, 5))
    var = cv2.blur(g * g, (5, 5)) - mu * mu
    flat = var < 16
    flat_share = float(flat.mean())
    q = (img[flat] >> 4).astype(np.int32) if flat.any() else np.zeros((0, 3), np.int32)
    n_colors = 0
    if len(q):
        codes = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
        _, counts = np.unique(codes, return_counts=True)
        n_colors = int((counts >= 0.01 * len(codes)).sum())
    ycc = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    skin = ((ycc[..., 1] >= 135) & (ycc[..., 1] <= 180) & (ycc[..., 2] >= 85) & (ycc[..., 2] <= 135))
    return {"lines": n_lines, "small_text": small, "flat": flat_share, "colors": n_colors,
            "skin": float(skin.mean())}


def ui_score(p: dict, face_area: float) -> float:
    lines = min(1.0, p["lines"] / 10)
    small = min(1.0, p["small_text"] / 8)
    flat = p["flat"] if p["colors"] <= 16 else p["flat"] * 0.5
    noskin = 1.0 - min(1.0, p["skin"] / 0.12)
    s = 0.35 * lines + 0.30 * small + 0.20 * min(1.0, flat / 0.7) + 0.15 * noskin
    if p["small_text"] < 3:      # an app shows many small labels; flat graphics with a few lines are not a UI
        s *= 0.5
    if face_area > 0.03:         # a big face is a person on camera, not a screen (a face bubble stays < 3%)
        s *= 0.3
    return round(float(min(1.0, s)), 3)


def colorfulness(img: np.ndarray) -> float:
    """Hasler & Suesstrunk (2003)."""
    b, g, r = [c.astype(np.float32) for c in (img[..., 0], img[..., 1], img[..., 2])]
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


# ============================================================================================== stream

def gray_stream(video: Path, width: int, height: int, *, timeout: float = 300) -> tuple[np.ndarray, int, int]:
    """4 fps grayscale frames, 160 px wide. Returns (frames [N, h, w] uint8, w, h)."""
    w = STREAM_W
    h = max(2, int(round(STREAM_W * (height / width if width and height else 16 / 9) / 2)) * 2)
    p = subprocess.run([_need("ffmpeg"), "-v", "error", "-i", str(video), "-an", "-vf",
                        f"fps={STREAM_FPS},scale={w}:{h},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    n = len(p.stdout) // (w * h)
    return np.frombuffer(p.stdout[:n * w * h], np.uint8).reshape(n, h, w), w, h


def split_lines(fr: np.ndarray) -> tuple[list[int], list[int]]:
    """Rows/columns near the middle (38-62%) where a straight edge spans >= 90% of the frame."""
    f = fr.astype(np.int16)
    H, W = f.shape
    rows, cols = [], []
    dy = np.abs(np.diff(f, axis=0))                    # (H-1, W)
    for y in range(int(0.38 * H), int(0.62 * H)):
        if (dy[y] > 25).mean() >= 0.9:
            rows.append(y)
    dx = np.abs(np.diff(f, axis=1))                    # (H, W-1)
    for x in range(int(0.38 * W), int(0.62 * W)):
        if (dx[:, x] > 25).mean() >= 0.9:
            cols.append(x)
    return rows, cols


def _group(v: list[int]) -> list[int]:
    out: list[int] = []
    for x in sorted(v):
        if not out or x - out[-1] > 3:
            out.append(x)
    return out


# ============================================================================================== main

def compute(video: Path, masters: list[dict], *, reel_dir: Path, duration: float, cuts: list[float],
            speech_ratio: float | None, width: int, height: int) -> dict:
    """VisualStats JSON (6.5.1) + `per_frame` (face/text/ui per master, used by frames.key_frames)."""
    import cv2

    det = face_detector()
    per_frame = []
    face_n = small_corner = center = 0
    face_areas: list[float] = []
    faces_max = 0
    text_areas, text_has, small_counts = [], [], []
    top_unions: list[tuple | None] = []
    band_boxes: list[list] = []
    grays: list[np.ndarray] = []
    ui_scores, colors, bright = [], [], []
    face_boxes_by_n: dict[int, list] = {}
    used: list[dict] = []
    for f in masters:
        img = cv2.imread(str(Path(reel_dir) / f["path"]))
        if img is None:
            continue
        used.append(f)
        h0, w0 = img.shape[:2]
        img = cv2.resize(img, (ANALYSIS_W, max(2, int(round(h0 * ANALYSIS_W / w0)))), interpolation=cv2.INTER_AREA)
        H, W = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        grays.append(gray)
        faces = detect_faces(det, img) if det is not None else []
        face_boxes_by_n[int(f["n"])] = [(x / W, y / H, w / W, h / H) for x, y, w, h, _ in faces]
        largest = max((w * h for _, _, w, h, _ in faces), default=0.0) / (W * H)
        if faces:
            face_n += 1
            face_areas.append(largest)
            faces_max = max(faces_max, len(faces))
            x, y, w, h, _ = max(faces, key=lambda b: b[2] * b[3])
            cx, cy = (x + w / 2) / W, (y + h / 2) / H
            if len(faces) == 1 and largest < 0.03 and (cx < 1 / 3 or cx > 2 / 3) and (cy < 1 / 3 or cy > 2 / 3):
                small_corner += 1
            if 1 / 3 <= cx <= 2 / 3:
                center += 1
        boxes = text_boxes(gray)
        # text on the face is not text (eyes/brows/mouth make text-like blobs)
        boxes = [b for b in boxes if not any(fx * W - 4 <= b[0] + b[2] / 2 <= (fx + fw) * W + 4 and
                                             fy * H - 4 <= b[1] + b[3] / 2 <= (fy + fh) * H + 4
                                             for fx, fy, fw, fh in face_boxes_by_n[int(f["n"])])]
        area = sum(b[2] * b[3] for b in boxes) / float(W * H)
        text_areas.append(area)
        text_has.append(area >= 0.003 and len(boxes) >= 1)
        small_counts.append(sum(1 for b in boxes if b[3] < 0.03 * H))
        top_unions.append(None)       # filled after the UI score: an app's own top bar is not a headline box
        band_boxes.append(boxes)
        parts = ui_parts(img, gray, boxes)
        us = ui_score(parts, largest)
        ui_scores.append(us)
        if us < 0.6:
            top_unions[-1] = _union([b for b in boxes if b[1] + b[3] / 2 < 0.25 * H])
        colors.append(colorfulness(img))
        bright.append(float(gray.mean()) / 255)
        per_frame.append({"n": int(f["n"]), "t": f["t"], "face": round(largest, 4), "faces": len(faces),
                          "text": round(area, 4), "ui": us})
    n = len(per_frame)
    have_faces = det is not None and n > 0
    stats: dict = {"version": VERSION, "face_detector": "yunet" if det is not None else "none", "frames": n}
    stats["face_share"] = round(face_n / n, 3) if have_faces else None
    stats["face_area_med"] = round(float(np.median(face_areas)), 4) if have_faces and face_areas else (
        0.0 if have_faces else None)
    stats["faces_max"] = faces_max if have_faces else None
    stats["small_face_corner_share"] = round(small_corner / n, 3) if have_faces else None
    stats["face_center_share"] = round(center / n, 3) if have_faces else None
    body = [p for p, f in zip(per_frame, used) if f.get("kind") in ("body", "cut")]
    stats["cutaway_share"] = (round(sum(1 for p in body if p["faces"] == 0) / len(body), 3)
                              if have_faces and body else None)
    stats["text_area_med"] = round(float(np.median(text_areas)), 4) if n else 0.0
    stats["text_frames_share"] = round(sum(text_has) / n, 3) if n else 0.0
    stats["top_text_box_share"] = round(_persistent_share(top_unions, gray_shape=grays[0].shape if grays else None), 3)
    stats["caption_band"] = _caption_band(band_boxes, grays, top_unions, ui_scores)
    stats["small_text_boxes_med"] = int(np.median(small_counts)) if n else 0
    stats["ui_score"] = round(float(np.median(ui_scores)), 3) if n else 0.0
    stats["ui_frames_share"] = round(sum(1 for u in ui_scores if u >= 0.6) / n, 3) if n else 0.0
    stats["colorfulness"] = round(float(np.median(colors)), 1) if n else 0.0
    stats["brightness_med"] = round(float(np.median(bright)), 3) if n else 0.0
    # ---- stream: motion, stills, panes, cut-out
    stream, sw, sh = gray_stream(video, width, height)
    st = _stream_stats(stream, cuts, used, face_boxes_by_n if det is not None else {}, grays)
    stats.update(st)
    stats["speech_ratio"] = None if speech_ratio is None else round(float(speech_ratio), 3)
    rules, recipe_rules = local_rules()
    stats["local_vf"] = guess_format(stats, rules)
    stats["local_recipe"] = {
        "headline_box": _cond(stats, recipe_rules.get("headline_box", DEFAULT_RECIPE_RULES["headline_box"])),
        "face_bubble": _cond(stats, recipe_rules.get("face_bubble", DEFAULT_RECIPE_RULES["face_bubble"])),
        "caption_band": stats["caption_band"],
    }
    stats["per_frame"] = per_frame
    return stats


def _persistent_share(unions: list, gray_shape) -> float:
    """Share of frames whose top text block stays within +-5% (of the frame) over >= 3 consecutive frames."""
    if not unions or gray_shape is None:
        return 0.0
    H, W = gray_shape
    n = len(unions)
    keep = [False] * n

    def same(a, b) -> bool:
        if a is None or b is None:
            return False
        return (abs(a[0] - b[0]) <= 0.05 * W and abs(a[2] - b[2]) <= 0.05 * W and abs(a[1] - b[1]) <= 0.05 * H
                and abs(a[3] - b[3]) <= 0.05 * H)

    i = 0
    while i < n:
        j = i
        while j + 1 < n and same(unions[j], unions[j + 1]):
            j += 1
        if unions[i] is not None and j - i + 1 >= 3:
            for k in range(i, j + 1):
                keep[k] = True
        i = j + 1
    return sum(keep) / n


def _caption_band(band_boxes: list, grays: list, top_unions: list, ui_scores: list) -> str:
    """Where per-frame CHANGING text sits: top | middle | bottom | none. A persistent headline box is ignored;
    UI frames are ignored (their text is the app, not captions)."""
    if len(grays) < 2:
        return "none"
    H, W = grays[0].shape
    bands = {"top": (0.0, 0.3), "middle": (0.3, 0.7), "bottom": (0.7, 1.0)}
    changing = {k: 0 for k in bands}
    pairs = 0
    for i in range(1, len(grays)):
        if ui_scores[i] >= 0.6 or ui_scores[i - 1] >= 0.6:
            continue
        pairs += 1
        for name, (a, b) in bands.items():
            cur = [bx for bx in band_boxes[i] if a <= (bx[1] + bx[3] / 2) / H < b]
            prev = [bx for bx in band_boxes[i - 1] if a <= (bx[1] + bx[3] / 2) / H < b]
            if not cur:
                continue
            u = _union(cur + prev)
            if u is None:
                continue
            x0, y0, x1, y1 = u
            if top_unions[i] is not None and top_unions[i - 1] is not None and name == "top":
                tu = top_unions[i]
                if abs(tu[1] - y0) <= 0.05 * H and abs(tu[3] - y1) <= 0.05 * H:
                    d = np.abs(grays[i][y0:y1, x0:x1].astype(np.int16) - grays[i - 1][y0:y1, x0:x1]).mean()
                    if d < 8:          # the same headline box: not a caption
                        continue
            d = np.abs(grays[i][y0:y1, x0:x1].astype(np.int16) - grays[i - 1][y0:y1, x0:x1].astype(np.int16)).mean()
            if d >= 8:
                changing[name] += 1
    if pairs == 0:
        return "none"
    best = max(changing, key=lambda k: changing[k])
    return best if changing[best] >= max(2, 0.3 * pairs) else "none"


def _stream_stats(stream: np.ndarray, cuts: list[float], masters: list[dict], face_boxes: dict,
                  grays: list) -> dict:
    out = {"motion_med": 0.0, "motion_p90": 0.0, "static_frame_share": 0.0, "split_share": 0.0, "panes_med": 1,
           "cutout_share": 0.0 if face_boxes else None}
    N = len(stream)
    if N < 2:
        return out
    times = np.arange(N) / STREAM_FPS
    shot_of = np.array([sum(1 for c in cuts if t >= c - 0.01) for t in times])
    motion, changed, pair_t = [], [], []
    for i in range(1, N):
        if shot_of[i] != shot_of[i - 1]:
            continue
        d = np.abs(stream[i].astype(np.int16) - stream[i - 1].astype(np.int16))
        motion.append(float(d.mean()) / 255)
        changed.append(float((d > PIX_CHANGE).mean()))
        pair_t.append(i)
    if motion:
        out["motion_med"] = round(float(np.median(motion)), 4)
        out["motion_p90"] = round(float(np.percentile(motion, 90)), 4)
        out["static_frame_share"] = round(float(np.mean([c < 0.01 for c in changed])), 3)
    # ---- panes: a straight middle edge in this frame and its neighbours, both sides moving on their own
    H, W = stream.shape[1:]
    lines = [split_lines(f) for f in stream]
    split_frames, panes = 0, []
    for i in range(N):
        rows, cols = _group(lines[i][0]), _group(lines[i][1])
        stable_r = [r for r in rows if all(any(abs(r - r2) <= 2 for r2 in lines[j][0])
                                            for j in range(max(0, i - 2), min(N, i + 3)))]
        stable_c = [c for c in cols if all(any(abs(c - c2) <= 2 for c2 in lines[j][1])
                                            for j in range(max(0, i - 2), min(N, i + 3)))]
        ok = []
        lo, hi = max(1, i - STREAM_FPS), min(N - 1, i + STREAM_FPS)
        for r in stable_r:
            a = _side_motion(stream, lo, hi, (slice(0, r - 2), slice(None)), shot_of)
            b = _side_motion(stream, lo, hi, (slice(r + 3, None), slice(None)), shot_of)
            if a >= 0.004 and b >= 0.004:
                ok.append(("h", r))
        for c in stable_c:
            a = _side_motion(stream, lo, hi, (slice(None), slice(0, c - 2)), shot_of)
            b = _side_motion(stream, lo, hi, (slice(None), slice(c + 3, None)), shot_of)
            if a >= 0.004 and b >= 0.004:
                ok.append(("v", c))
        if ok:
            split_frames += 1
        panes.append(1 + len(ok))
    out["split_share"] = round(split_frames / N, 3)
    out["panes_med"] = int(np.median(panes)) if panes else 1
    # ---- cut-out over still: static background, moving person box, a bright/dark halo on the person's edge
    if face_boxes:
        out["cutout_share"] = round(_cutout_share(stream, shot_of, masters, face_boxes, grays), 3)
    return out


def _side_motion(stream, lo: int, hi: int, region, shot_of) -> float:
    vals = []
    for j in range(lo, hi + 1):
        if j < 1 or shot_of[j] != shot_of[j - 1]:
            continue
        a = stream[j][region].astype(np.int16)
        b = stream[j - 1][region].astype(np.int16)
        if a.size:
            vals.append(float(np.abs(a - b).mean()) / 255)
    return float(np.mean(vals)) if vals else 0.0


def _halo(gray: np.ndarray, box: tuple[int, int, int, int]) -> float:
    """Density of thin near-white / near-black structures in the person box (keyer edge halo)."""
    import cv2

    x0, y0, x1, y1 = box
    roi = gray[y0:y1, x0:x1]
    if roi.size == 0:
        return 0.0
    ext = ((roi > 235) | (roi < 20)).astype(np.uint8)
    opened = cv2.morphologyEx(ext, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    thin = ext & (1 - opened)
    return float(thin.mean())


def _cutout_share(stream, shot_of, masters, face_boxes, grays) -> float:
    N, H, W = stream.shape
    hits = 0
    # person box per shot from the master frames in that shot (largest face, grown 40%, down to the bottom)
    by_shot: dict[int, tuple] = {}
    gray_by_shot: dict[int, tuple] = {}
    for k, f in enumerate(masters):
        fb = face_boxes.get(int(f["n"])) or []
        if not fb:
            continue
        x, y, w, h = max(fb, key=lambda b: b[2] * b[3])
        if w * h < 0.005:      # a face bubble is not a cut-out speaker
            continue
        gx0, gx1 = max(0.0, x - 0.4 * w * 1.5), min(1.0, x + w + 0.4 * w * 1.5)
        gy0 = max(0.0, y - 0.4 * h)
        s = int(f.get("shot", 0))
        if s not in gray_by_shot or w * h > gray_by_shot[s][1][2] * gray_by_shot[s][1][3]:
            by_shot[s] = (gx0, gy0, gx1, 1.0)
            if k < len(grays):
                gray_by_shot[s] = (grays[k], (x, y, w, h))
    for s, (gx0, gy0, gx1, gy1) in by_shot.items():
        idx = [i for i in range(1, N) if shot_of[i] == s and shot_of[i - 1] == s]
        if not idx:
            continue
        g, (x, y, w, h) = gray_by_shot.get(s, (None, None))
        halo_ok = False
        if g is not None:
            GH, GW = g.shape
            bx = (int(max(0, x - 0.6 * w) * GW), int(max(0, y - 0.3 * h) * GH), int(min(1, x + 1.6 * w) * GW),
                  int(min(1, y + 2.0 * h) * GH))
            halo_ok = _halo(g, bx) >= 0.015
        x0, y0, x1, y1 = int(gx0 * W), int(gy0 * H), int(gx1 * W), int(gy1 * H)
        mask = np.zeros((H, W), bool)
        mask[y0:y1, x0:x1] = True
        for i in idx:
            d = np.abs(stream[i].astype(np.int16) - stream[i - 1].astype(np.int16)) > PIX_CHANGE
            bg = float(d[~mask].mean()) if (~mask).any() else 1.0
            fg = float(d[mask].mean()) if mask.any() else 0.0
            if halo_ok and bg < 0.02 and fg > 0.08:
                hits += 1
        # pairs in shots without a usable person box still count in the denominator
    all_pairs = sum(1 for i in range(1, N) if shot_of[i] == shot_of[i - 1])
    return hits / all_pairs if all_pairs else 0.0
