#!/usr/bin/env python3
"""
generate_xml.py — Builds FCP7 XML from Whisper transcript for Premiere Pro import.

Usage:
  python generate_xml.py --video raw.mp4 --transcript workspace/transcript.json
                         --output workspace/timeline_cut.xml [--silence-threshold 0.4]
"""

import json, argparse, os
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

def ticks(sec, tb=30):
    return int(round(sec * tb))

def build_xml(video_path, segments, silence_threshold=0.4, timebase=30):
    abs_path = os.path.abspath(video_path)
    file_url = "file://localhost" + abs_path.replace("\\", "/")
    if not abs_path.startswith("/"):
        file_url = "file://localhost/" + abs_path.replace("\\", "/")

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
    SubElement(seq, "name").text = "AI Rough Cut — V1"
    rate_el = SubElement(seq, "rate")
    SubElement(rate_el, "timebase").text = str(timebase)
    SubElement(rate_el, "ntsc").text = "FALSE"
    total_ticks = sum(ticks(s["end"] - s["start"], timebase) for s in merged)
    SubElement(seq, "duration").text = str(total_ticks)

    media = SubElement(seq, "media")
    video = SubElement(media, "video")
    fmt = SubElement(video, "format")
    sc = SubElement(fmt, "samplecharacteristics")
    SubElement(sc, "width").text = "1080"
    SubElement(sc, "height").text = "1920"
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
    args = p.parse_args()

    with open(args.transcript) as f:
        data = json.load(f)

    xmeml, kept = build_xml(args.video, data.get("segments", []),
                            args.silence_threshold, args.timebase)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    tree = ElementTree(xmeml)
    indent(tree, space="  ")
    with open(args.output, "wb") as out:
        out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        tree.write(out, encoding="utf-8", xml_declaration=False)

    total = sum(s["end"] - s["start"] for s in kept)
    print(f"\n✅ XML written → {args.output}")
    print(f"   Segments kept: {len(kept)}  |  Duration: {total:.1f}s")
    print(f"\n▶  Premiere: File > Import > {args.output}")

if __name__ == "__main__":
    main()
