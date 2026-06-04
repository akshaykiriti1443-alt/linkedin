# /new-series — Generate V2 top-half motion graphics (Remotion or HyperFrames)

You are a motion graphics developer. Read `video-bot/workspace/transcript.json`.

## Renderer Decision — pick per scene

| Scene type | Use |
|---|---|
| 3D terrain, camera sweep, WebGL | **Remotion + VoxThreeScene** |
| Animated chart, stat card, text reveal | **HyperFrames (HTML)** — simpler, no JSX |
| Lower third, chapter card | **HyperFrames (HTML)** |
| Complex spring physics, React state | **Remotion** |

## Steps

1. Run the scaffold:
   ```bash
   cd video-bot && tsx src/new-series.ts
   ```

2. Read the transcript. Divide into segments:
   - Hook (first 3–5s)
   - Key points (one per idea)
   - CTA (last 3–5s)

3. For each segment, choose a renderer:

### Option A — Remotion (3D / complex)
Write a scene to `src/compositions/scenes/YourScene.tsx`:
- Use `<VoxThreeScene label="..." sublabel="..." accentColor="#ff6b00" />`
- ALL elements: `top` < 960px, `height` ≤ 960px
- Update `src/compositions/TopHalf.tsx` with `<Sequence from={frame}>` tags
- Render: `npm run render-top` → `out/top_animations.mov`

### Option B — HyperFrames (HTML / simple)
Write a plain HTML file to `src/compositions/html/scene_N.html`:
```html
<!DOCTYPE html>
<html>
<head>
  <style>
    body { margin: 0; background: transparent; width: 1080px; height: 960px; }
    .stat { font: 900 120px "Arial Black"; color: #fff; animation: fadeUp 0.4s ease; }
    @keyframes fadeUp { from { opacity:0; transform:translateY(40px) } to { opacity:1; transform:none } }
  </style>
</head>
<body>
  <div class="stat">47%</div>
  <div class="label">conversion rate</div>
</body>
</html>
```
Then render with HyperFrames:
```bash
cd video-bot
npx hyperframes render src/compositions/html/scene_N.html \
  --duration 3 --fps 30 --width 1080 --height 960 \
  --output out/scene_N.mov --transparent
```
Stitch all scene .mov files into top_animations.mov:
```bash
ffmpeg -y -f concat -safe 0 -i src/compositions/html/scenes.txt -c copy out/top_animations.mov
```

4. Copy to Premiere imports:
   ```bash
   cp out/top_animations.mov premiere_imports/graphics/
   ```

5. **Premiere action**: Drag onto **Track V2**, align to timecode 0.

## Constraints
- Background: `transparent`
- No Google Fonts — Arial, Arial Black, Courier New only
- Viewport: 1080×960 (top half only — never render below y=960)
- For HyperFrames: set `body { overflow: hidden; }`
