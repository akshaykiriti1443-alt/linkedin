#!/usr/bin/env python3
"""
generate_xml.py — Builds FCP7 XML from Whisper transcript for Premiere Pro import.

Usage:
  python generate_xml.py --video raw.mp4 --transcript workspace/transcript.json
                         --output workspace/timeline_cut.xml
                         [--silence-threshold 0.4]
                         [--layout shorts|youtube|center-split|floating-cam]

Layouts:
  shorts        Face anchored to bottom 50% (1080x1920, Position Y 1440). Default.
  youtube       Full 16:9 frame, no crop. Sequence 1920x1080.
  center-split  Face centered vertically; graphics left/right quadrants.
  floating-cam  Face as circular PiP in bottom-right corner over full frame.
"""

import json, argparse, os, subprocess, shutil
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

LAYOUTS = {
    "shorts":       {"width": 1080, "height": 1920, "desc": "9:16 Shorts — face bottom 50%"},
    "youtube":      {"width": 1920, "height": 1080, "desc": "16:9 YouTube — full frame"},
    "center-split": {"width": 1080, "height": 1920, "desc": "Center split — face centered"},
    "floating-cam": {"width": 1080, "height": 1920, "desc": "Floating cam — PiP bottom-right"},
}

def detect_aspect_ratio(video_path):
    """Use ffprobe to detect video dimensions. Returns (width, height) or None."""
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=10
        )
        parts = result.stdout.strip().split(",")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None

def auto_detect_layout(video_path):
    """Pick layout based on video aspect ratio."""
    dims = detect_aspect_ratio(video_path)
    if dims is None:
        return "shorts"
    w, h = dims
    ratio = w / h
    if ratio > 1.5:
        return "youtube"   # 16:9 or wider
    if ratio < 0.7:
        return "shorts"    # 9:16 portrait
    return "center-split"  # square-ish

def ticks(sec, tb=30):
    return int(round(sec * tb))

def build_xml(video_path, segments, silence_threshold=0.4, timebase=30, layout="shorts"):
    abs_path = os.path.abspath(video_path)
    file_url = "file://localhost" + abs_path.replace("\\", "/")
    if not abs_path.startswith("/"):
        file_url = "file://localhost/" + abs_path.replace("\\", "/")

    lyt = LAYOUTS.get(layout, LAYOUTS["shorts"])
    seq_w, seq_h = lyt["width"], lyt["height"]

    # Build keep segments from word-level silence detection
    keep = []
    for seg in segments:
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

    # Merge adjacent segments (gap < 0.05s rounding noise)
    merged = []
    for seg in keep:
        if merged and seg["start"] - merged[-1]["end"] < 0.05:
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(dict(seg))

    xmeml = Element("xmeml", version="4")
    seq = SubElement(xmeml, "sequence")
    SubElement(seq, "name").text = f"AI Rough Cut — V1 [{layout}]"
    rate_el = SubElement(seq, "rate")
    SubElement(rate_el, "timebase").text = str(timebase)
    SubElement(rate_el, "ntsc").text = "FALSE"
    total_ticks = sum(ticks(s["end"] - s["start"], timebase) for s in merged)
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

    vtk = SubElement(video, "track")
    audio = SubElement(media, "audio")
    atk = SubElement(audio, "track")

    tl_pos = 0
    for i, seg in enumerate(merged):
        in_t  = ticks(seg["start"], timebase)
        out_t = ticks(seg["end"], timebase)
        dur_t = out_t - in_t

        for is_audio in (False, True):
            clip = SubElement(atk if is_audio else vtk, "clipitem",
                              id=f"{'a' if is_audio else 'v'}{i}")
            SubElement(clip, "name").text = os.path.basename(abs_path)
            SubElement(clip, "start").text = str(tl_pos)
            SubElement(clip, "end").text = str(tl_pos + dur_t)
            SubElement(clip, "in").text = str(in_t)
            SubElement(clip, "out").text = str(out_t)
            fe = SubElement(clip, "file", id=f"f{i}")
            SubElement(fe, "pathurl").text = file_url
            r3 = SubElement(fe, "rate")
            SubElement(r3, "timebase").text = str(timebase)
            SubElement(r3, "ntsc").text = "FALSE"

            # Layout-specific motion filters on V1 clip
            if not is_audio and layout != "youtube":
                filters = SubElement(clip, "filters")
                filt = SubElement(filters, "filter")
                SubElement(filt, "name").text = "Motion"
                param_list = []
                if layout == "shorts":
                    param_list = [("scale", "180"), ("positionY", "1440")]
                elif layout == "center-split":
                    param_list = [("scale", "90"), ("positionY", "960")]
                elif layout == "floating-cam":
                    param_list = [("scale", "35"), ("positionX", "810"), ("positionY", "1700")]
                for pname, pval in param_list:
                    param = SubElement(filt, "parameter")
                    SubElement(param, "name").text = pname
                    SubElement(param, "value").text = pval

            if is_audio:
                SubElement(clip, "channelcount").text = "2"

        tl_pos += dur_t

    return xmeml, merged

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--transcript", required=True)
    p.add_argument("--output", default="workspace/timeline_cut.xml")
    p.add_argument("--silence-threshold", type=float, default=0.4)
    p.add_argument("--timebase", type=int, default=30)
    p.add_argument("--layout", choices=list(LAYOUTS.keys()), default=None,
                   help="Force a layout. Omit to auto-detect from video dimensions.")
    args = p.parse_args()

    # Auto-detect layout if not specified
    layout = args.layout or auto_detect_layout(args.video)
    lyt = LAYOUTS[layout]
    print(f"\n🎬 Layout: {layout} — {lyt['desc']}")

    with open(args.transcript) as f:
        data = json.load(f)

    xmeml, kept = build_xml(args.video, data.get("segments", []),
                            args.silence_threshold, args.timebase, layout)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    tree = ElementTree(xmeml)
    indent(tree, space="  ")
    with open(args.output, "wb") as out:
        out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        tree.write(out, encoding="utf-8", xml_declaration=False)

    total = sum(s["end"] - s["start"] for s in kept)
    print(f"✅ XML written → {args.output}")
    print(f"   Segments kept : {len(kept)}  |  Duration: {total:.1f}s")
    print(f"   Sequence size : {lyt['width']}×{lyt['height']}")
    print(f"\n▶  Premiere: File > Import > {args.output}")
    if layout == "shorts":
        print("   V1 clips already have Scale=180%, PositionY=1440 baked in.")
    elif layout == "floating-cam":
        print("   V1 clip is a PiP in bottom-right. Drag your b-roll to V0.")

if __name__ == "__main__":
    main()
