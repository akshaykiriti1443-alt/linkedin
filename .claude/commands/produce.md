# /produce — Rough cut via FCP XML (imports into Premiere Pro)

You are an expert editor. Use `video-bot/workspace/transcript.json` from the /transcribe step.

## Steps

1. Run the XML generator:
   ```bash
   cd video-bot
   python src/generate_xml.py \
     --video "$(cat workspace/meta.json | python -c 'import sys,json; print(json.load(sys.stdin)["videoPath"])')" \
     --transcript workspace/transcript.json \
     --output workspace/timeline_cut.xml \
     --silence-threshold 0.4 \
     --layout auto
   ```

   **Layout options** (pass as `--layout <value>`):
   - omit / `auto`   → ffprobe detects your video dimensions and picks automatically
   - `shorts`        → 9:16, face bottom 50% (Scale 180%, Y 1440) — Reels/TikTok
   - `youtube`       → 16:9 full frame, no crop — YouTube essays
   - `center-split`  → face centered, graphics left/right
   - `floating-cam`  → face as small PiP bottom-right, b-roll fills frame

2. Print the edit summary:
   - How many silence gaps were removed
   - How many keep segments remain
   - Final duration vs original

3. Tell the user:
   - **Premiere action**: `File > Import > workspace/timeline_cut.xml`
   - This creates a sequence on V1 with all silences already cut
   - For `shorts` layout: motion parameters (Scale 180%, Y 1440) are already embedded in
     the XML — no manual preset needed. For other layouts, no crop is applied.

4. Then run /new-series to generate V2 motion graphics.

## Screen recording (optional)
If the user also has a screen recording, add:
```bash
--screen "$(cat workspace/meta.json | python -c 'import sys,json; print(json.load(sys.stdin).get("screenPath",""))')" \
--screen-offset 0
```
Or just pass the path directly: `--screen workspace/screen.mp4`

Segments where the screen recording is active → MODE B (screen full frame + face PiP).
Segments with no screen recording → MODE A (face bottom half + Vox 3D animations).

## Notes
- Python 3.8+ required. The script uses only stdlib (no pip dependencies).
- The XML uses relative file paths — keep your video in the same location.
- If retakes need removing, tell the user to flag the timestamps manually in Premiere
  (drag rolling edit tool) rather than auto-cutting — preserves syllables.
