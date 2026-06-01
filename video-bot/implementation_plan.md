# Implementation Plan — Universal Layout + Auto-Edit + 3D Vox Graphics

## Files to touch

### [MODIFY] src/generate_xml.py
- Add `detect_aspect_ratio(video_path)` using ffprobe
- Add `--layout` CLI flag: `shorts` (default) | `youtube` | `center-split` | `floating-cam`
- `shorts` → face bottom 50% (current behaviour)
- `youtube` → full 16:9, no crop
- `center-split` → face centered, graphics left/right
- `floating-cam` → circular mask PiP overlay

### [NEW] auto_edit.py
- Single-command orchestrator: transcribe → silence-cut → semantic asset match → render → XML
- Reads `profiles/` directory for named configuration profiles
- Accepts: `python auto_edit.py workspace/raw.mp4 --profile shorts`

### [NEW] profiles/shorts.json
- Layout: shorts, style: vox-3d, sfx: on, overlays: on

### [NEW] profiles/youtube.json
- Layout: youtube, style: ali, sfx: off, overlays: on

### [MODIFY] src/compositions/TopHalf.tsx
- Replace flat HTML/CSS scenes with @remotion/three ThreeCanvas
- Vox-style: terrain mesh, bezier camera sweep, neon vector path, grunge noise texture
- Hook into useCurrentFrame() for frame-driven camera animation

### [MODIFY] src/compositions/Root.tsx
- Keep existing composition registrations; no structural change needed

### [NEW] src/compositions/scenes/VoxThreeScene.tsx
- ThreeCanvas composition with OrthographicCamera
- Terrain plane mesh + neon LineSegments
- useVideoTexture() to project raw footage onto 3D plane
- Grunge noise via alphaMap on MeshStandardMaterial

### [MODIFY] package.json
- Add @remotion/three, three, @types/three

### [MODIFY] .claude/commands/produce.md
- Document --layout flag usage

## Execution order
1. package.json — add deps
2. VoxThreeScene.tsx — 3D scene component
3. TopHalf.tsx — wire in ThreeCanvas + VoxThreeScene
4. generate_xml.py — add aspect ratio detection + layout flag
5. auto_edit.py — orchestrator
6. profiles/*.json — config profiles
7. produce.md — update docs

## Verification
- `npm install` — no errors
- `npx tsc --noEmit` — type check passes
- `npm run render-top` — renders top_animations.mov (requires headless shell)
