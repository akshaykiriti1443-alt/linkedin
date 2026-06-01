# /build-timeline — Assemble final 3-track XML for Premiere Pro

You are a post-production engineer. Assembles V1 + V2 + V3 + A2 SFX into one importable XML.

## Steps

1. Check all assets exist:
   - `workspace/timeline_cut.xml` (V1 rough cut) — from /produce
   - `premiere_imports/graphics/*.mov` (V2) — from /new-series
   - `premiere_imports/overlays/*.mov` (V3) — from /real-overlay

2. Run:
   ```bash
   cd video-bot && npx tsx src/build-timeline.ts
   ```

3. Output: `workspace/final_timeline.xml`

4. **Premiere actions** (tell the user exactly):
   a. `File > Import > workspace/final_timeline.xml`
   b. Select all V1 clips → apply `Shorts_Bottom_Half` preset:
      - Scale: ~180%  |  Position Y: 1440
   c. V2 graphics are pre-placed at timecode 0 — align manually if needed
   d. V3 overlays stack cleanly via alpha channel
   e. A2 SFX are placed at keyword timecodes

5. Final export from Premiere:
   - Sequence settings: 1080×1920
   - Export via Media Encoder: H.264, Instagram preset
   - Or: `File > Export > Media` → Match Source + H.264

## Track layout in Premiere
```
V3  ████░░████░░░░████  ← overlays (alpha)
V2  ████████░░░████████ ← motion graphics (top 50%)
V1  ████████████████████ ← talking head (bottom 50%)
A1  ════════════════════ ← voice audio
A2  ⚡  ⚡    ⚡  ⚡    ← SFX cues
```
