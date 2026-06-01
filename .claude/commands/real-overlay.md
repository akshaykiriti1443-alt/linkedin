# /real-overlay — Generate V3 transparent keyword overlays (Remotion → .mov)

You are a motion graphics developer. Read `video-bot/workspace/transcript.json`.

## Steps

1. Run the scaffold:
   ```bash
   cd video-bot && tsx src/real-overlay.ts
   ```
   Prints word-level timestamps for overlay planning.

2. Identify high-impact moments:
   - Key nouns, numbers, strong verbs
   - Transition phrases ("the key thing is", "here's why", "so what happens")
   - Max 1 overlay every 4 seconds

3. Write overlay components to `src/compositions/overlays/`:
   - Bold white text + colored highlight box
   - Position: top 30% only (y < 576px) — never cover the face
   - Duration: word end - word start + 0.3s

4. Update `src/compositions/Overlay.tsx` with `<Sequence>` tags.

5. Render + smoke test:
   ```bash
   cd video-bot && npm run render-overlay
   ```
   Output → `out/overlay.mov` (ProRes 4444, full alpha)

6. Fix any overflow automatically before reporting done (check no element y > 960px).

7. Copy to Premiere imports:
   ```bash
   mkdir -p video-bot/premiere_imports/overlays
   cp video-bot/out/overlay.mov video-bot/premiere_imports/overlays/
   ```

8. **Premiere action**: Drag `premiere_imports/overlays/overlay.mov` onto **Track V3**.
   The alpha channel stacks cleanly over V1 and V2.
