#!/usr/bin/env python3
"""
generate_xml.py — Smart dual-mode FCP7 XML builder.

Modes auto-assigned per segment:
  MODE A  (no screen recording for this range)
          V1 = talking head bottom half  |  V2 = Vox 3D animations
  MODE B  (screen recording covers this range)
          V1 = screen recording full frame  |  V2 = talking head PiP (bottom-right)

Usage:
  python generate_xml.py
    --video workspace/raw.mp4
    --transcript workspace/transcript.json
    --output workspace/timeline_cut.xml
    [--screen workspace/screen.mp4]
    [--screen-offset 0.0]          # seconds: when screen recording started vs talking head
    [--silence-threshold 0.4]
    [--layout shorts|youtube|center-split|floating-cam]
"""

import json, argparse, os, subprocess, shutil
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

# PiP position for talking head when screen recording is active
PIP = {"scale": "30", "positionX": "870", "positionY": "1750"}

# Talking head bottom-half params (MODE A, shorts layout)
SHORTS_MOTION = {"scale": "180", "positionY": "1440"}

def ticks(sec, tb=30):
    return int(round(sec * tb))

def get_video_duration(path):
    """Return duration in seconds via ffprobe. Returns None if unavailable."""
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10
        )
        return float(r.stdout.strip())
    except Exception:
        return None

def get_video_dims(path):
    """Return (width, height) via ffprobe."""
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10
        )
        parts = r.stdout.strip().split(",")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None

def auto_detect_layout(video_path):
    dims = get_video_dims(video_path)
    if dims is None:
        return "shorts"
    w, h = dims
    ratio = w / h
    if ratio > 1.5: return "youtube"
    if ratio < 0.7: return "shorts"
    return "center-split"

def build_keep_segments(whisper_segments, silence_threshold):
    """Detect silence gaps and return list of {start, end} keep windows."""
    keep = []
    for seg in whisper_segments:
        words = seg.get("words", [])
        if not words:
            keep.append({"start": seg["start"], "end": seg["end"]})
            continue
        chunk_start = words[0]["start"]
        for i in range(1, len(words)):
            gap = words[i]["start"] - words[i-1]["end"]
            if gap > silence_threshold:
                keep.append({"start": chunk_start, "end": words[i-1]["end"]})
                chunk_start = words[i]["start"]
        keep.append({"start": chunk_start, "end": words[-1]["end"]})

    # Merge segments with sub-50ms gap (rounding noise)
    merged = []
    for seg in keep:
        if merged and seg["start"] - merged[-1]["end"] < 0.05:
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    return merged

def motion_filter(params: dict) -> Element:
    """Build a <filters><filter> element with motion params."""
    filters = Element("filters")
    filt = SubElement(filters, "filter")
    SubElement(filt, "name").text = "Motion"
    for pname, pval in params.items():
        param = SubElement(filt, "parameter")
        SubElement(param, "name").text = pname
        SubElement(param, "value").text = pval
    return filters

def build_xml(
    video_path, segments, silence_threshold, timebase, layout,
    screen_path=None, screen_offset=0.0
):
    abs_cam = os.path.abspath(video_path)
    url_cam = "file://localhost" + (abs_cam if abs_cam.startswith("/") else "/" + abs_cam).replace("\\", "/")

    abs_scr = os.path.abspath(screen_path) if screen_path else None
    url_scr = None
    screen_duration = None
    if abs_scr:
        url_scr = "file://localhost" + (abs_scr if abs_scr.startswith("/") else "/" + abs_scr).replace("\\", "/")
        screen_duration = get_video_duration(abs_scr)

    keep = build_keep_segments(segments, silence_threshold)

    # Determine MODE per segment
    seg_modes = []
    for seg in keep:
        mode = "vox"  # default: Vox animations
        if screen_duration is not None:
            # Screen recording covers this segment if it overlaps [start-offset, end-offset]
            scr_start = seg["start"] - screen_offset
            scr_end   = seg["end"]   - screen_offset
            if scr_start >= 0 and scr_end <= screen_duration:
                mode = "screen"
        seg_modes.append(mode)

    # Sequence dimensions
    seq_w, seq_h = (1080, 1920)  # always output in 9:16 container
    if layout == "youtube":
        seq_w, seq_h = (1920, 1080)

    # Root XML
    xmeml = Element("xmeml", version="4")
    seq = SubElement(xmeml, "sequence")
    SubElement(seq, "name").text = "AI Smart Cut — V1/V2 dual-mode"
    rate_el = SubElement(seq, "rate")
    SubElement(rate_el, "timebase").text = str(timebase)
    SubElement(rate_el, "ntsc").text = "FALSE"

    total_ticks = sum(ticks(s["end"] - s["start"], timebase) for s in keep)
    SubElement(seq, "duration").text = str(total_ticks)

    media = SubElement(seq, "media")
    video = SubElement(media, "video")
    fmt = SubElement(video, "format")
    sc = SubElement(fmt, "samplecharacteristics")
    SubElement(sc, "width").text = str(seq_w)
    SubElement(sc, "height").text = str(seq_h)
    r2 = SubElement(sc, "rate")
    SubElement(r2, "timebase").text = str(timebase)
    SubElement(r2, "ntsc").text = "FALSE"

    # V1 track: talking head (MODE A) OR screen recording (MODE B)
    v1_track = SubElement(video, "track")
    # V2 track: Vox placeholder (MODE A) OR talking head PiP (MODE B)
    v2_track = SubElement(video, "track")

    audio = SubElement(media, "audio")
    a1_track = SubElement(audio, "track")  # voice always from talking head

    tl_pos = 0
    segments_meta = []

    for i, (seg, mode) in enumerate(zip(keep, seg_modes)):
        in_t  = ticks(seg["start"], timebase)
        out_t = ticks(seg["end"],   timebase)
        dur_t = out_t - in_t
        duration_sec = seg["end"] - seg["start"]

        # ── V1 clip ──────────────────────────────────────────────────────────
        v1_clip = SubElement(v1_track, "clipitem", id=f"v1_{i}")
        if mode == "screen":
            SubElement(v1_clip, "name").text = os.path.basename(abs_scr)
            scr_in  = ticks(max(0, seg["start"] - screen_offset), timebase)
            scr_out = ticks(max(0, seg["end"]   - screen_offset), timebase)
            SubElement(v1_clip, "start").text = str(tl_pos)
            SubElement(v1_clip, "end").text   = str(tl_pos + dur_t)
            SubElement(v1_clip, "in").text    = str(scr_in)
            SubElement(v1_clip, "out").text   = str(scr_out)
            fe = SubElement(v1_clip, "file", id=f"scrf{i}")
            SubElement(fe, "pathurl").text = url_scr
            r3 = SubElement(fe, "rate")
            SubElement(r3, "timebase").text = str(timebase)
            SubElement(r3, "ntsc").text = "FALSE"
            # Full frame — no motion filter
        else:
            # MODE A: talking head, bottom half
            SubElement(v1_clip, "name").text = os.path.basename(abs_cam)
            SubElement(v1_clip, "start").text = str(tl_pos)
            SubElement(v1_clip, "end").text   = str(tl_pos + dur_t)
            SubElement(v1_clip, "in").text    = str(in_t)
            SubElement(v1_clip, "out").text   = str(out_t)
            fe = SubElement(v1_clip, "file", id=f"camf{i}")
            SubElement(fe, "pathurl").text = url_cam
            r3 = SubElement(fe, "rate")
            SubElement(r3, "timebase").text = str(timebase)
            SubElement(r3, "ntsc").text = "FALSE"
            if layout != "youtube":
                v1_clip.append(motion_filter(SHORTS_MOTION))

        # ── V2 clip ──────────────────────────────────────────────────────────
        if mode == "screen":
            # Talking head PiP over the screen recording
            v2_clip = SubElement(v2_track, "clipitem", id=f"v2pip_{i}")
            SubElement(v2_clip, "name").text = f"PiP_{os.path.basename(abs_cam)}"
            SubElement(v2_clip, "start").text = str(tl_pos)
            SubElement(v2_clip, "end").text   = str(tl_pos + dur_t)
            SubElement(v2_clip, "in").text    = str(in_t)
            SubElement(v2_clip, "out").text   = str(out_t)
            fe2 = SubElement(v2_clip, "file", id=f"pipf{i}")
            SubElement(fe2, "pathurl").text = url_cam
            r4 = SubElement(fe2, "rate")
            SubElement(r4, "timebase").text = str(timebase)
            SubElement(r4, "ntsc").text = "FALSE"
            v2_clip.append(motion_filter(PIP))
        # MODE A: V2 left empty — build-timeline.ts will place Vox .mov here

        # ── A1 audio: always from talking head ───────────────────────────────
        a1_clip = SubElement(a1_track, "clipitem", id=f"a1_{i}")
        SubElement(a1_clip, "name").text = os.path.basename(abs_cam)
        SubElement(a1_clip, "start").text = str(tl_pos)
        SubElement(a1_clip, "end").text   = str(tl_pos + dur_t)
        SubElement(a1_clip, "in").text    = str(in_t)
        SubElement(a1_clip, "out").text   = str(out_t)
        afe = SubElement(a1_clip, "file", id=f"af{i}")
        SubElement(afe, "pathurl").text = url_cam
        ar = SubElement(afe, "rate")
        SubElement(ar, "timebase").text = str(timebase)
        SubElement(ar, "ntsc").text = "FALSE"
        SubElement(a1_clip, "channelcount").text = "2"

        segments_meta.append({
            "tl_start": tl_pos / timebase,
            "tl_end":   (tl_pos + dur_t) / timebase,
            "src_start": seg["start"],
            "src_end":   seg["end"],
            "mode": mode,
        })
        tl_pos += dur_t

    return xmeml, keep, seg_modes, segments_meta

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video",      required=True)
    p.add_argument("--transcript", required=True)
    p.add_argument("--output",     default="workspace/timeline_cut.xml")
    p.add_argument("--screen",     default=None, help="Screen recording file (optional)")
    p.add_argument("--screen-offset", type=float, default=0.0,
                   help="Seconds into talking head when screen recording started")
    p.add_argument("--silence-threshold", type=float, default=0.4)
    p.add_argument("--timebase",   type=int, default=30)
    p.add_argument("--layout",     default=None,
                   choices=["shorts","youtube","center-split","floating-cam"])
    args = p.parse_args()

    layout = args.layout or auto_detect_layout(args.video)
    print(f"\n🎬 Layout : {layout}")
    if args.screen:
        print(f"📺 Screen : {args.screen}  (offset {args.screen_offset}s)")

    with open(args.transcript) as f:
        data = json.load(f)

    xmeml, kept, modes, seg_meta = build_xml(
        args.video, data.get("segments", []),
        args.silence_threshold, args.timebase, layout,
        args.screen, args.screen_offset
    )

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    tree = ElementTree(xmeml)
    indent(tree, space="  ")
    with open(args.output, "wb") as out:
        out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        tree.write(out, encoding="utf-8", xml_declaration=False)

    # Write segments_meta.json for build-timeline.ts
    meta_path = os.path.join(os.path.dirname(args.output), "segments_meta.json")
    with open(meta_path, "w") as f:
        json.dump(seg_meta, f, indent=2)

    vox_count    = modes.count("vox")
    screen_count = modes.count("screen")
    total_dur    = sum(s["end"] - s["start"] for s in kept)

    print(f"✅ XML written → {args.output}")
    print(f"   Segments   : {len(kept)} total")
    print(f"   MODE A Vox : {vox_count} segments  (talking head + 3D animations)")
    print(f"   MODE B Scr : {screen_count} segments  (screen recording + face PiP)")
    print(f"   Duration   : {total_dur:.1f}s")
    print(f"\n▶  Premiere: File > Import > {args.output}")

if __name__ == "__main__":
    main()
