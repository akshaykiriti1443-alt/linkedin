#!/usr/bin/env python3
"""
auto_edit.py — Single-command autonomous pipeline.

Usage:
  python auto_edit.py workspace/raw.mp4 [--profile shorts|youtube|floating-cam]

Runs the full chain unattended:
  1. Whisper transcription → workspace/transcript.json
  2. Silence detection + XML cut → workspace/timeline_cut.xml
  3. Prints scene plan for Claude to write Remotion compositions
  4. Renders top_animations.mov + overlay.mov (if renderers are available)
  5. Assembles workspace/final_timeline.xml

Requirements: whisper, ffprobe, node/tsx all on PATH.
"""

import sys, os, json, subprocess, shutil, argparse, time

ROOT = os.path.dirname(os.path.abspath(__file__))

def run(cmd, label):
    print(f"\n{'─'*60}")
    print(f"▶  {label}")
    print(f"   {cmd}")
    print('─'*60)
    result = subprocess.run(cmd, shell=True, cwd=ROOT)
    if result.returncode != 0:
        print(f"\n❌ FAILED: {label}")
        sys.exit(result.returncode)
    print(f"✅ Done: {label}")

def load_profile(name):
    path = os.path.join(ROOT, "profiles", f"{name}.json")
    if not os.path.exists(path):
        print(f"⚠️  Profile '{name}' not found. Using defaults.")
        return {}
    with open(path) as f:
        return json.load(f)

def detect_layout(video_path, profile):
    if "layout" in profile:
        return profile["layout"]
    # ffprobe auto-detect
    if shutil.which("ffprobe"):
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path],
                capture_output=True, text=True, timeout=10
            )
            parts = r.stdout.strip().split(",")
            if len(parts) == 2:
                w, h = int(parts[0]), int(parts[1])
                ratio = w / h
                if ratio > 1.5:
                    return "youtube"
                if ratio < 0.7:
                    return "shorts"
                return "center-split"
        except Exception:
            pass
    return "shorts"

def main():
    p = argparse.ArgumentParser()
    p.add_argument("video", help="Path to raw video (e.g. workspace/raw.mp4)")
    p.add_argument("--screen", default=None,
                   help="Screen recording file (optional). When provided, segments where "
                        "the screen recording is active switch to Screen+PiP mode.")
    p.add_argument("--screen-offset", type=float, default=0.0,
                   help="Seconds into the talking head when the screen recording started")
    p.add_argument("--profile", default="shorts", help="Profile name from profiles/ dir")
    p.add_argument("--silence-threshold", type=float, default=None)
    args = p.parse_args()

    video = args.video
    if not os.path.exists(video):
        print(f"❌ Video not found: {video}")
        sys.exit(1)

    profile = load_profile(args.profile)
    layout = detect_layout(video, profile)
    silence_threshold = args.silence_threshold or profile.get("silence_threshold", 0.4)
    whisper_model = profile.get("whisper_model", "base")

    screen = args.screen
    screen_offset = args.screen_offset

    print(f"\n{'═'*60}")
    print(f"  AUTO-EDIT PIPELINE")
    print(f"  Video        : {video}")
    if screen:
        print(f"  Screen rec   : {screen}  (offset {screen_offset}s)")
    print(f"  Profile      : {args.profile}")
    print(f"  Layout       : {layout}")
    print(f"  Silence      : {silence_threshold}s threshold")
    print(f"{'═'*60}\n")

    os.makedirs("workspace", exist_ok=True)
    os.makedirs("premiere_imports/graphics", exist_ok=True)
    os.makedirs("premiere_imports/overlays", exist_ok=True)

    # Write meta.json
    with open("workspace/meta.json", "w") as f:
        json.dump({"videoPath": os.path.abspath(video), "layout": layout, "profile": args.profile}, f, indent=2)

    # STEP 1 — Transcribe
    run(
        f'whisper "{video}" --model {whisper_model} --word_timestamps True '
        f'--output_format json --output_dir workspace --task transcribe',
        "Whisper transcription"
    )

    # Whisper names output after the input file — rename to transcript.json
    base = os.path.splitext(os.path.basename(video))[0]
    whisper_out = os.path.join("workspace", f"{base}.json")
    if os.path.exists(whisper_out) and whisper_out != "workspace/transcript.json":
        shutil.copy(whisper_out, "workspace/transcript.json")
        print(f"   Renamed {whisper_out} → workspace/transcript.json")

    # STEP 2 — Cut silences → XML (smart mode switching)
    screen_args = ""
    if screen:
        screen_args = f'--screen "{screen}" --screen-offset {screen_offset}'
    run(
        f'python src/generate_xml.py '
        f'--video "{video}" '
        f'--transcript workspace/transcript.json '
        f'--output workspace/timeline_cut.xml '
        f'--silence-threshold {silence_threshold} '
        f'--layout {layout} '
        f'{screen_args}',
        "Smart silence-cut XML (Vox/Screen auto-mode)"
    )

    # STEP 3 — Print transcript for Claude scene planning
    with open("workspace/transcript.json") as f:
        data = json.load(f)

    print(f"\n{'─'*60}")
    print("📋 TRANSCRIPT (for Claude scene planning)")
    print('─'*60)
    all_words = []
    for seg in data.get("segments", []):
        for w in seg.get("words", []):
            all_words.append(w)
        print(f"  [{seg['start']:6.2f}s → {seg['end']:6.2f}s]  {seg['text'].strip()}")

    # Semantic keyword extraction — top nouns/numbers for V2 scene triggers
    import re
    keywords = []
    TRIGGER_PATTERNS = [
        r'\b\d+[\.,]?\d*[%$BMK]?\b',          # numbers/stats
        r'\b[A-Z][a-z]+ [A-Z][a-z]+\b',        # proper nouns
        r'\b(because|here\'s why|the key|so what|turns out|actually|but here)\b',  # hooks
    ]
    full_text = " ".join(s["text"] for s in data.get("segments", []))
    for pat in TRIGGER_PATTERNS:
        for m in re.finditer(pat, full_text, re.IGNORECASE):
            keywords.append(m.group(0))
    if keywords:
        print(f"\n🎯 Auto-detected scene triggers: {', '.join(set(keywords[:10]))}")

    # STEP 4 — Render Remotion compositions (non-blocking if tsx not available)
    node_ok = shutil.which("node") is not None
    tsx_ok = shutil.which("tsx") is not None or node_ok

    if node_ok:
        run("npm install --silent", "Install npm deps")
        run(
            'npx remotion render src/index.ts TopHalf out/top_animations.mov '
            '--codec=prores --prores-profile=4444',
            "Render V2 motion graphics (TopHalf)"
        )
        if os.path.exists("out/top_animations.mov"):
            shutil.copy("out/top_animations.mov", "premiere_imports/graphics/top_animations.mov")
            print("   Copied → premiere_imports/graphics/top_animations.mov")

        run(
            'npx remotion render src/index.ts Overlay out/overlay.mov '
            '--codec=prores --prores-profile=4444',
            "Render V3 overlays (Overlay)"
        )
        if os.path.exists("out/overlay.mov"):
            shutil.copy("out/overlay.mov", "premiere_imports/overlays/overlay.mov")
            print("   Copied → premiere_imports/overlays/overlay.mov")
    else:
        print("\n⚠️  Node.js not found — skipping Remotion render.")
        print("   Run manually: npm run render-top && npm run render-overlay")

    # STEP 5 — Assemble final XML
    run("npx tsx src/build-timeline.ts", "Assemble final_timeline.xml")

    print(f"\n{'═'*60}")
    print("🎉  PIPELINE COMPLETE")
    print(f"{'═'*60}")
    print(f"\n  V1 XML  : workspace/timeline_cut.xml")
    print(f"  V2 .mov : premiere_imports/graphics/top_animations.mov")
    print(f"  V3 .mov : premiere_imports/overlays/overlay.mov")
    print(f"  FINAL   : workspace/final_timeline.xml")
    print(f"\n▶  Premiere: File > Import > workspace/final_timeline.xml")
    print(f"   Layout applied: {layout}")
    if layout == "shorts":
        print("   V1 clips: Scale 180%, Position Y 1440 (already set in XML)")
    elif layout == "youtube":
        print("   V1 clips: Full 1920×1080 frame — no crop needed")
    elif layout == "floating-cam":
        print("   V1 = PiP bottom-right. Drop your b-roll onto V0 track.")
    print()

if __name__ == "__main__":
    main()
