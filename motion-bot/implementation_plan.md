# Implementation Plan — AI Video Production Pipeline

## Goal
One command takes a Premiere transcript → AI plans the edit → renders Vox/Ali-style motion graphics synced to exact word timestamps → places them on the Premiere timeline automatically.

---

## Architecture

```
transcript (SRT/txt)
        │
        ▼
[ 1. Transcript Parser ]   — parses word timestamps from Premiere-exported SRT
        │
        ▼
[ 2. AI Scene Planner ]    — Claude reads transcript, decides WHAT graphic
        │                    goes on WHICH word, and in which style
        ▼
[ 3. Remotion Generator ]  — generates per-cue TSX composition (Vox or Ali)
        │
        ▼
[ 4. Render Engine ]       — renders each composition to transparent .mov
        │
        ▼
[ 5. Premiere Placer ]     — MCP bridge places each .mov on the right timeline track
                             at the exact timestamp Premiere gave us
```

---

## Files

### [NEW] `premiere-mcp/` — Adobe Premiere MCP server (from cloned repo)
- Copy of `Adobe_Premiere_Pro_MCP/` integrated as a sub-package
- Windows temp dir: `C:\Temp\premiere-mcp-bridge`
- Built with `npm run build` inside this folder

### [NEW] `motion-bot/src/pipeline/`

| File | Purpose |
|------|---------|
| `[NEW] parse-transcript.ts` | Parses SRT → `{ word, startTime, endTime }[]` |
| `[NEW] plan-scenes.ts` | Calls Claude API: reads full transcript, returns JSON cue list |
| `[NEW] generate-compositions.ts` | For each cue, writes a Remotion TSX to `src/compositions/cues/` |
| `[NEW] render-cues.ts` | Renders each composition to `out/cues/*.mov` (transparent bg) |
| `[NEW] place-in-premiere.ts` | Calls Premiere MCP to import + place each .mov at correct timecode |
| `[NEW] produce.ts` | Orchestrator CLI — runs all 5 steps end to end |

### [NEW] `motion-bot/src/compositions/styles/`

| File | Purpose |
|------|---------|
| `[NEW] VoxStyle.tsx` | Bold kinetic text, highlight boxes, animated stat callouts |
| `[NEW] AliStyle.tsx` | Clean lower thirds, chapter cards, icon+text combos |

### [MODIFY] `motion-bot/package.json`
- Add `produce` script: `tsx src/pipeline/produce.ts`
- Add `@anthropic-ai/sdk` dependency back (for scene planner)
- Add `dotenv` dependency

### [NEW] `motion-bot/.env.example`
```
ANTHROPIC_API_KEY=sk-ant-...
PREMIERE_TEMP_DIR=C:\Temp\premiere-mcp-bridge
```

---

## Step-by-Step Logic

### Step 1 — parse-transcript.ts
- Input: path to `.srt` file (exported from Premiere via File > Export > Captions)
- Output: `Cue[]` = `{ index, text, startTime, endTime, words }`
- SRT format already has timestamps — no word-level needed, we sync to cue start

### Step 2 — plan-scenes.ts
- Sends full transcript text to Claude with this system prompt:
  ```
  You are a motion graphics director. Given a transcript, return a JSON array.
  Each item: { cueIndex, style: "vox"|"ali", type: "text_callout"|"lower_third"|"stat"|"chapter_card", content: "...", duration: seconds }
  Only add graphics to the most impactful moments. Max 1 graphic per 8 seconds.
  ```
- Returns `ScenePlan[]`

### Step 3 — generate-compositions.ts
- For each `ScenePlan`, writes a TSX file to `src/compositions/cues/cue-{n}.tsx`
- Uses `VoxStyle` or `AliStyle` component based on `plan.style`
- Sets `durationInFrames = Math.round(plan.duration * 30)`

### Step 4 — render-cues.ts
- For each cue composition, calls:
  ```
  npx remotion render src/index.ts CueN out/cues/cue-N.mov --codec=prores --prores-profile=4444
  ```
- ProRes 4444 = transparent alpha → overlays cleanly in Premiere

### Step 5 — place-in-premiere.ts
- Starts Premiere MCP bridge (reads PREMIERE_TEMP_DIR)
- For each rendered `.mov`:
  1. `import_media` → gets projectItemId
  2. `add_track` (video track above main footage)
  3. `add_to_timeline` at `cue.startTime`, `linkAudio: false`

---

## Vox Style Spec
- White or black background option
- Bold ALL-CAPS text, center screen, orange/yellow highlight box behind key word
- Stats: large number animates counting up
- Animated underline draw on key phrase

## Ali Abdaal Style Spec
- Lower third: clean white card slides in from left, name + descriptor
- Chapter card: full-screen title fade, emoji + bold text
- Subtle drop shadow, rounded corners, never loud

---

## CLI Usage (after setup)
```bash
npm run produce -- --transcript="C:\path\to\captions.srt" --style=vox
npm run produce -- --transcript="C:\path\to\captions.srt" --style=ali
npm run produce -- --transcript="C:\path\to\captions.srt" --style=both
```

---

## Approval checklist
- [ ] Approve this plan
- [ ] Confirm `ANTHROPIC_API_KEY` will be added to `.env`
- [ ] Confirm Premiere Pro is release build (not Beta)
- [ ] Confirm Windows temp path: `C:\Temp\premiere-mcp-bridge`
