# /new-series — Generate V2 top-half motion graphics (Remotion → .mov)

You are a React/Remotion developer. Read `video-bot/workspace/transcript.json`.

## Steps

1. Run the scaffold:
   ```bash
   cd video-bot && tsx src/new-series.ts
   ```
   This prints the timestamped transcript and creates skeleton files.

2. Read the transcript output. Divide into segments:
   - Hook (first 3–5s)
   - Key points (one per idea)
   - CTA (last 3–5s)

3. For each segment, write a Remotion scene to `src/compositions/scenes/`:
   - Dark-mode UI mockup, terminal animation, stat card, or browser mockup
   - ALL elements: `top` < 960px, `height` ≤ 960px (top 50% of 1080×1920)
   - Use `spring()` for entrances, system fonts only

4. Update `src/compositions/TopHalf.tsx` with `<Sequence from={frame}>` tags.

5. Render:
   ```bash
   cd video-bot
   npm run render-top
   ```
   Output → `out/top_animations.mov` (ProRes 4444, transparent bg)

6. Copy to Premiere imports:
   ```bash
   mkdir -p video-bot/premiere_imports/graphics
   cp video-bot/out/top_animations.mov video-bot/premiere_imports/graphics/
   ```

7. **Premiere action**: Drag `premiere_imports/graphics/top_animations.mov` onto **Track V2**.
   Align to timecode 00:00:00:00.

## Constraints
- Background: `transparent` (Premiere shows black top half from V1 sequence)
- No Google Fonts — Arial, Arial Black, Courier New only
- Viewport: 1080×960 (NOT 1080×1920 — TopHalf only renders top half)
