# Build Task — AI Video Production Pipeline

## Checklist

- [x] VoxStyle.tsx — bold kinetic text, highlight boxes, stat callouts
- [x] AliStyle.tsx — lower thirds, chapter cards, clean overlays
- [x] parse-transcript.ts — SRT parser → Cue[]
- [x] render-cues.ts — renders all cues to out/cues/*.mov (ProRes 4444)
- [x] place-in-premiere.ts — MCP bridge placer
- [x] produce.ts — orchestrator CLI
- [x] Update package.json — new scripts, dotenv
- [x] Update Root.tsx — registers all cue compositions dynamically
- [x] .env.example — PREMIERE_TEMP_DIR=C:\Temp\premiere-editor
- [ ] Build + verify Adobe_Premiere_Pro_MCP (run on Windows)
