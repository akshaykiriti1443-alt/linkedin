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
     --silence-threshold 0.4
   ```

2. Print the edit summary:
   - How many silence gaps were removed
   - How many keep segments remain
   - Final duration vs original

3. Tell the user:
   - **Premiere action**: `File > Import > workspace/timeline_cut.xml`
   - This creates a sequence on V1 with all silences already cut
   - Next: apply the `Shorts_Bottom_Half` preset to all V1 clips:
     - Scale: ~180% | Position Y: 1440 | Sequence: 1080×1920

4. Then run /new-series to generate V2 motion graphics.

## Notes
- Python 3.8+ required. The script uses only stdlib (no pip dependencies).
- The XML uses relative file paths — keep your video in the same location.
- If retakes need removing, tell the user to flag the timestamps manually in Premiere
  (drag rolling edit tool) rather than auto-cutting — preserves syllables.
