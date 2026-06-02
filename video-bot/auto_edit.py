#!/usr/bin/env python3
"""
auto_edit.py — Single-command autonomous pipeline.

Usage:
  python auto_edit.py workspace/raw.mp4 [options]

Options:
  --screen workspace/screen.mp4   Screen recording (optional)
  --screen-offset 0.0             Seconds offset between talking head + screen
  --profile shorts|youtube|...    Layout profile (default: talking-head)
  --quality draft|preview|final   Output quality (default: draft)
  --grade auto|warm_cinematic|... Color grade preset (default: warm_cinematic)
  --reset                         Clear session and re-run from scratch
  --reset-step <step>             Reset a specific step only

Session persistence: completed steps are saved to workspace/project.md.
Re-running auto_edit.py skips already-completed steps automatically.

Steps: transcribe → timeline_preview → cut_xml → color_grade → sfx → render_graphics → build_timeline
"""

import sys, os, json, subprocess, shutil, argparse, time, re as _re
from pathlib import Path

ROOT = Path(__file__).parent

# ── Import engine modules ─────────────────────────────────────────────────
sys.path.insert(0, str(ROOT))
from engine.session import is_done, mark_done, status as session_status, reset as session_reset

def run(cmd, label, cwd=None):
    print(f"\n{'─'*58}")
    print(f"▶  {label}")
    print('─'*58)
    result = subprocess.run(cmd, shell=True, cwd=cwd or ROOT)
    if result.returncode != 0:
        print(f"\n❌ FAILED: {label}")
        sys.exit(result.returncode)
    print(f"✅ {label}")

def load_profile(name):
    path = ROOT / "profiles" / f"{name}.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)

def detect_layout(video_path, profile):
    if "layout" in profile:
        return profile["layout"]
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
                if ratio > 1.5: return "youtube"
                if ratio < 0.7: return "shorts"
                return "center-split"
        except Exception:
            pass
    return "shorts"

def get_video_duration(video_path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=10
        )
        return float(r.stdout.strip())
    except Exception:
        return None

def open_file(path):
    """Open a file with the default system viewer (cross-platform)."""
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path])
    else:
        subprocess.run(["xdg-open", path])

def main():
    p = argparse.ArgumentParser()
    p.add_argument("video", help="Path to raw video")
    p.add_argument("--screen",        default=None)
    p.add_argument("--screen-offset", type=float, default=0.0)
    p.add_argument("--profile",       default="talking-head")
    p.add_argument("--quality",       choices=["draft","preview","final"], default="draft")
    p.add_argument("--grade",         default="warm_cinematic",
                   help="Color grade preset: auto|warm_cinematic|neutral_punch|subtle|none")
    p.add_argument("--reset",         action="store_true", help="Clear session, re-run all")
    p.add_argument("--reset-step",    default=None, help="Reset one specific step")
    p.add_argument("--status",        action="store_true", help="Show session status and exit")
    args = p.parse_args()

    os.chdir(ROOT)

    if args.status:
        session_status()
        return

    if args.reset:
        session_reset()
    elif args.reset_step:
        session_reset(args.reset_step)

    video = args.video
    if not os.path.exists(video):
        print(f"❌ Video not found: {video}")
        sys.exit(1)

    profile          = load_profile(args.profile)
    layout           = detect_layout(video, profile)
    silence_threshold = profile.get("silence_threshold", 0.4)
    whisper_model    = profile.get("whisper_model", "base")
    screen           = args.screen
    screen_offset    = args.screen_offset

    os.makedirs("workspace", exist_ok=True)
    os.makedirs("premiere_imports/graphics", exist_ok=True)
    os.makedirs("premiere_imports/overlays",  exist_ok=True)
    os.makedirs("out", exist_ok=True)

    # Write/update meta.json
    dur = get_video_duration(video)
    meta = {
        "videoPath":    os.path.abspath(video),
        "screenPath":   os.path.abspath(screen) if screen else None,
        "layout":       layout,
        "profile":      args.profile,
        "quality":      args.quality,
        "grade":        args.grade,
        "duration_s":   dur,
    }
    with open("workspace/meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\n{'═'*58}")
    print(f"  VIDEO BOT — AUTO EDIT")
    print(f"  Video    : {video}")
    if screen:
        print(f"  Screen   : {screen}  (offset {screen_offset}s)")
    print(f"  Layout   : {layout}  |  Quality: {args.quality}  |  Grade: {args.grade}")
    print(f"{'═'*58}")

    # ── STEP 1: TRANSCRIBE ────────────────────────────────────────────────
    if is_done("transcribe"):
        print("\n⏭  transcribe — already done (project.md). Skipping.")
    else:
        run(
            f'whisper "{video}" --model {whisper_model} --word_timestamps True '
            f'--output_format json --output_dir workspace --task transcribe',
            "Whisper transcription"
        )
        # Rename to transcript.json
        base = os.path.splitext(os.path.basename(video))[0]
        whisper_out = f"workspace/{base}.json"
        if os.path.exists(whisper_out) and whisper_out != "workspace/transcript.json":
            shutil.copy(whisper_out, "workspace/transcript.json")

        # Load transcript and print summary
        with open("workspace/transcript.json") as f:
            data = json.load(f)
        all_words = [w for seg in data.get("segments", []) for w in seg.get("words", [])]
        print(f"\n📝 Transcript: {len(all_words)} words, {dur:.1f}s")
        for seg in data.get("segments", []):
            print(f"  [{seg['start']:6.2f}s]  {seg['text'].strip()}")

        mark_done("transcribe", {"words": len(all_words), "duration_s": dur})

    # ── STEP 2: TIMELINE PREVIEW ──────────────────────────────────────────
    preview_png = "workspace/timeline_preview.png"
    if is_done("timeline_preview") and os.path.exists(preview_png):
        print(f"\n⏭  timeline_preview — already done. Skipping.")
    else:
        print(f"\n{'─'*58}")
        print(f"▶  Generating timeline preview PNG")
        print('─'*58)
        try:
            from engine.timeline_view import generate_preview
            generate_preview(video, "workspace/transcript.json", preview_png)
            mark_done("timeline_preview")
            # Open the preview so user can see it
            if os.path.exists(preview_png):
                try:
                    open_file(preview_png)
                    print(f"  👁  Opened: {preview_png}")
                except Exception:
                    print(f"  💡 View manually: {preview_png}")
        except Exception as e:
            print(f"  ⚠️  Timeline preview failed: {e} — continuing.")

    # ── STEP 3: CUT XML + EDL ─────────────────────────────────────────────
    if is_done("cut_xml"):
        print(f"\n⏭  cut_xml — already done. Skipping.")
    else:
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
            "Smart silence-cut → XML + EDL"
        )
        # Also build rough_cut.mp4 via lossless render pipeline
        if os.path.exists("workspace/edl.json") and shutil.which("ffmpeg"):
            try:
                from engine.render import build_rough_cut
                with open("workspace/edl.json") as f:
                    edl = json.load(f)
                build_rough_cut(
                    edl["video"], edl["keep_segments"],
                    "workspace/rough_cut.mp4", args.quality
                )
            except Exception as e:
                print(f"  ⚠️  Lossless render failed: {e}")
        mark_done("cut_xml")

    # ── STEP 4: COLOR GRADE ───────────────────────────────────────────────
    if args.grade == "none":
        print(f"\n⏭  color_grade — skipped (--grade none).")
    elif is_done("color_grade"):
        print(f"\n⏭  color_grade — already done. Skipping.")
    elif not os.path.exists("workspace/rough_cut.mp4"):
        print(f"\n⚠️  color_grade — rough_cut.mp4 not found, skipping.")
    else:
        print(f"\n{'─'*58}")
        print(f"▶  Color grading ({args.grade})")
        print('─'*58)
        try:
            from engine.grade import apply_grade
            apply_grade("workspace/rough_cut.mp4", "workspace/graded.mp4", args.grade)
            shutil.copy("workspace/graded.mp4", "workspace/rough_cut.mp4")
            mark_done("color_grade", {"preset": args.grade})
        except Exception as e:
            print(f"  ⚠️  Color grade failed: {e} — continuing with ungraded.")

    # ── STEP 5: SFX ───────────────────────────────────────────────────────
    sfx_events_path = "workspace/sfx_events.json"
    sfx_draft_path  = "workspace/sfx_events_draft.json"

    if is_done("sfx") and os.path.exists("workspace/sfx_track.wav"):
        print(f"\n⏭  sfx — already done. Skipping.")
    else:
        # Auto-generate basic events from keyword patterns
        if not os.path.exists(sfx_events_path) and os.path.exists("workspace/transcript.json"):
            with open("workspace/transcript.json") as f:
                data = json.load(f)
            all_words = [w for seg in data.get("segments", []) for w in seg.get("words", [])]
            events = []
            last_any, last_per = -2000, {}
            SFX_PATTERNS = [
                (r'\b\d+[\.,]?\d*[%$BMK]?\b', 'digital_readout', 7000),
                (r'\b(next|now|moving on|first|second|third|finally)\b', 'whoosh', 5000),
                (r'\b(actually|but here|key|important|critical)\b', 'impact', 6000),
                (r'\b(click|open|tap|select|press)\b', 'mouse_click', 3000),
                (r'\b(type|code|command|install|run)\b', 'keyboard', 3000),
                (r'\b(done|success|complete|finished)\b', 'ding', 5000),
            ]
            for w in all_words:
                at_ms = int(w.get("start", 0) * 1000)
                if at_ms - last_any < 1500:
                    continue
                for pat, sfx, min_gap in SFX_PATTERNS:
                    if at_ms - last_per.get(sfx, -min_gap) < min_gap:
                        continue
                    if _re.search(pat, w.get("word", ""), _re.IGNORECASE):
                        events.append({"sfx": sfx, "at_ms": at_ms})
                        last_any = at_ms
                        last_per[sfx] = at_ms
                        break
            with open(sfx_events_path, "w") as f:
                json.dump(events, f, indent=2)
            print(f"\n🎵 Auto SFX: {len(events)} events → workspace/sfx_events.json")
            print(f"   Run /soundeffects in Claude for smarter placements.")

        # Build SFX WAV
        try:
            import importlib.util
            if importlib.util.find_spec("pydub") and os.path.exists(sfx_events_path):
                run("python engine/build_sfx_track.py", "Mix SFX WAV")
                mark_done("sfx")
            else:
                if not importlib.util.find_spec("pydub"):
                    print("\n⚠️  pydub not installed — skipping SFX mix.")
                    print("   Install: pip install pydub && python engine/build_sfx_track.py")
        except Exception as e:
            print(f"  ⚠️  SFX mix failed: {e}")

    # ── STEP 6: RENDER GRAPHICS ───────────────────────────────────────────
    if is_done("render_graphics"):
        print(f"\n⏭  render_graphics — already done. Skipping.")
    elif shutil.which("node"):
        run("npm install --silent", "Install npm deps")
        run(
            'npx remotion render src/index.ts TopHalf out/top_animations.mov '
            '--codec=prores --prores-profile=4444',
            "Render V2 Vox 3D graphics"
        )
        if os.path.exists("out/top_animations.mov"):
            shutil.copy("out/top_animations.mov", "premiere_imports/graphics/top_animations.mov")
        run(
            'npx remotion render src/index.ts Overlay out/overlay.mov '
            '--codec=prores --prores-profile=4444',
            "Render V3 overlays"
        )
        if os.path.exists("out/overlay.mov"):
            shutil.copy("out/overlay.mov", "premiere_imports/overlays/overlay.mov")
        mark_done("render_graphics")
    else:
        print("\n⚠️  Node.js not found — skipping Remotion render.")
        print("   Run manually: npm run render-top && npm run render-overlay")

    # ── STEP 7: BUILD TIMELINE XML ────────────────────────────────────────
    if is_done("build_timeline"):
        print(f"\n⏭  build_timeline — already done. Skipping.")
    else:
        run("npx tsx src/build-timeline.ts", "Assemble final_timeline.xml")
        mark_done("build_timeline")

    # ── DONE ──────────────────────────────────────────────────────────────
    print(f"\n{'═'*58}")
    print("🎉  PIPELINE COMPLETE")
    print(f"{'═'*58}")
    print(f"\n  Timeline PNG : workspace/timeline_preview.png")
    print(f"  Rough cut    : workspace/rough_cut.mp4  (lossless, normalized)")
    print(f"  V2 graphics  : premiere_imports/graphics/top_animations.mov")
    print(f"  V3 overlays  : premiere_imports/overlays/overlay.mov")
    print(f"  SFX track    : workspace/sfx_track.wav")
    print(f"  Premiere XML : workspace/final_timeline.xml")
    print(f"\n▶  Premiere: File > Import > workspace/final_timeline.xml")
    print(f"\n💡 To re-run a step:  python auto_edit.py ... --reset-step transcribe")
    print(f"   To re-run all:     python auto_edit.py ... --reset")
    print(f"   Session status:    python auto_edit.py ... --status")
    print()

if __name__ == "__main__":
    main()
