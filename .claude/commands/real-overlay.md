# /real-overlay — Generate transparent keyword overlay

You are a motion graphics developer. Read `video-bot/workspace/transcript.json`.

## Steps

1. Scan the transcript for:
   - High-impact words/phrases (nouns, numbers, strong verbs)
   - Transition phrases ("the key thing is", "here's the problem", "so what happens")
   - Any word that appears while a screen recording would be shown

2. For each flagged moment, write a short overlay component in `video-bot/src/compositions/overlays/`:
   - File: `overlay-{timestamp}-{slug}.tsx`
   - Style: bold white text with colored highlight box, OR animated SVG icon
   - Position: safe zone — never cover the face (bottom 50%), stick to top 30%
   - Duration: match the word's end timestamp minus start timestamp + 0.3s buffer

3. Update `video-bot/src/compositions/Overlay.tsx` to sequence all overlays using `<Sequence>` tags.

4. Smoke test — render and check for text overflow:
   ```bash
   cd video-bot && npx remotion render src/index.ts Overlay out/overlay.mov --codec=prores --prores-profile=4444 --browser-executable=/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell
   ```

5. If any text overflows (>1080px wide or >960px tall), fix automatically before reporting done.

## Notes
- Output is ProRes 4444 with alpha channel — fully transparent background
- This layer goes ON TOP of everything else in /finalize
- Keep it minimal — max 1 overlay every 4 seconds
