#!/usr/bin/env python3
"""
transcribe.py — faster-whisper wrapper (drop-in replacement for openai-whisper CLI).

Installs with ONE command: pip install faster-whisper
No torch, no ffmpeg-python, no heavy deps.

Usage:
  python engine/transcribe.py --video workspace/raw.mp4
  python engine/transcribe.py --video workspace/raw.mp4 --model small --output workspace/transcript.json

Output: workspace/transcript.json in Whisper-compatible format:
  {segments: [{text, start, end, words: [{word, start, end, probability}]}]}
"""

import json, os, sys, argparse
from pathlib import Path

WORKSPACE = Path("workspace")

def transcribe(video_path: str, model_size: str = "base", output_path: str = None) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("\n❌ faster-whisper not installed.")
        print("   Run: pip install faster-whisper")
        print("\n   OR use Premiere Pro transcription instead:")
        print("   Open Claude Code → type: /transcribe-premiere")
        sys.exit(1)

    print(f"\n🎙  Transcribing with faster-whisper (model={model_size})...")
    print(f"   Video: {video_path}")

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments_raw, info = model.transcribe(
        video_path,
        word_timestamps=True,
        language=None,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
    )

    print(f"   Detected language: {info.language} ({info.language_probability:.0%} confidence)")
    print(f"   Duration: {info.duration:.1f}s")

    segments = []
    all_words = 0
    for seg in segments_raw:
        words = []
        for w in (seg.words or []):
            words.append({
                "word":        w.word,
                "start":       round(w.start, 3),
                "end":         round(w.end,   3),
                "probability": round(w.probability, 3),
            })
            all_words += 1
        segments.append({
            "id":    seg.id,
            "start": round(seg.start, 3),
            "end":   round(seg.end,   3),
            "text":  seg.text.strip(),
            "words": words,
        })
        print(f"  [{seg.start:6.2f}s]  {seg.text.strip()}")

    result = {
        "language": info.language,
        "duration": info.duration,
        "segments": segments,
    }

    out = output_path or str(WORKSPACE / "transcript.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    srt_path = out.replace(".json", ".srt")
    txt_path = out.replace(".json", ".txt")

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            def fmt(t):
                h, r = divmod(t, 3600)
                m, s = divmod(r, 60)
                ms = int((s % 1) * 1000)
                return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"
            f.write(f"{i}\n{fmt(seg['start'])} --> {fmt(seg['end'])}\n{seg['text']}\n\n")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(" ".join(seg["text"] for seg in segments))

    print(f"\n✅ Transcript written:")
    print(f"   {out}  ({all_words} words, {len(segments)} segments)")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--video",  required=True)
    p.add_argument("--model",  default="base",
                   choices=["tiny","base","small","medium","large-v2","large-v3"])
    p.add_argument("--output", default=None)
    a = p.parse_args()
    transcribe(a.video, a.model, a.output)
