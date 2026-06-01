# /new-series — Generate top-half Remotion animations from transcript

You are a React/Remotion developer. Read `video-bot/workspace/transcript.json` and the rough cut duration from `video-bot/workspace/edit_report.json`.

## Steps

1. Read the cleaned transcript and divide it into segments:
   - Hook (first 3–5 seconds)
   - Main points (one per key idea)
   - CTA (last 3–5 seconds)

2. For each segment, write a Remotion scene into `video-bot/src/compositions/scenes/`:
   - File name: `scene-{n}-{slug}.tsx`
   - Each scene renders ONLY in the top 50% of 1080×1920 (y: 0 to 960px)
   - Use `spring()` for all entrance animations
   - Style options: dark terminal UI, browser mockup, typing animation, bold stat card

3. Update `video-bot/src/compositions/TopHalf.tsx` to sequence all scenes using `<Sequence>` tags timed to match the transcript timestamps.

4. Run a smoke test:
   ```bash
   cd video-bot && npx remotion render src/index.ts TopHalf out/top_animations.mov --codec=prores --prores-profile=4444 --browser-executable=/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell
   ```

5. Report: how many scenes were created, total duration matches rough cut.

## Constraints
- ALL elements must have `top` < 960px and `height` that does not exceed 960px
- No element can bleed into the bottom 50% (face-cam zone)
- Use only system fonts (Arial, Courier New) — no Google Fonts
- Background of each scene: `rgba(0,0,0,0)` (transparent) — black background is set by /produce
