# Implementation Plan — video-use + HyperFrames Integration

## What we're stealing (priority order)

### P1 — Fixes every video (quality)
1. **30ms audio fades** at every cut point → no clicking
2. **Lossless concat** → no re-encode quality loss
3. **Loudness normalization** → -14 LUFS (social media standard)

### P2 — More automated
4. **Session persistence** → skip Whisper/steps if already done
5. **Timeline preview PNG** → filmstrip + waveform before you commit to edit
6. **Auto color grade** → warm_cinematic preset by default

### P3 — Simpler for Claude to write animations
7. **HyperFrames HTML scenes** → plain HTML alternative to Remotion TSX

---

## Files

### [NEW] engine/render.py
- `extract_segment(video, start, end, out)` — ffmpeg -c copy, no re-encode
- `concat_segments(segment_files, out)` — concat demuxer, lossless
- `add_audio_fades(concat_file, cut_points, out)` — 30ms afade at each cut
- `normalize_loudness(file, out, target_lufs=-14)` — two-pass loudnorm
- `quality_encode(file, out, mode)` — draft(720p CRF28) / preview(1080p CRF22) / final(1080p CRF20)

### [NEW] engine/grade.py
- `sample_frames(video, n=8)` → avg brightness/saturation
- `auto_grade_filter(stats)` → ffmpeg eq+curves string
- Presets: warm_cinematic, neutral_punch, subtle, none
- `apply_grade(video, out, preset='warm_cinematic')`

### [NEW] engine/timeline_view.py
- `generate_preview(video, transcript, output_png)` 
- Filmstrip (8 frames) + RMS waveform + word labels + silence shading
- Output: workspace/timeline_preview.png

### [NEW] engine/session.py
- Reads/writes workspace/project.md
- `is_done(step)` → bool (skip if already completed)
- `mark_done(step, metadata)` → append to project.md
- Steps: transcribe, cut, grade, sfx, render, timeline

### [MODIFY] auto_edit.py
- Wrap every step with session.is_done() / mark_done()
- Add --quality flag (draft|preview|final, default=draft)
- Add --grade flag (warm_cinematic|neutral_punch|subtle|none, default=warm_cinematic)
- Step order: transcribe → preview PNG → cut → grade → sfx → render → XML

### [MODIFY] src/generate_xml.py
- Write workspace/edl.json alongside XML (structured edit decision list)

### [MODIFY] run.bat
- Add quality prompt (draft = fast preview, final = full quality)
- After transcription: open timeline_preview.png automatically
- Cleaner output messages

### [MODIFY] new-series.md slash-command
- Add HyperFrames as alternative: if scene is simple (text/chart/map) → write HTML
- If scene needs React/spring → write Remotion TSX

---

## Execution order
1. engine/session.py
2. engine/render.py
3. engine/grade.py
4. engine/timeline_view.py
5. generate_xml.py (EDL output)
6. auto_edit.py (wire everything)
7. run.bat (quality prompt + preview open)
8. new-series.md (HyperFrames option)

## Verification
- python engine/render.py --help → no import errors
- python engine/grade.py --help → no import errors
- python engine/timeline_view.py --help → no import errors
- npx tsc --noEmit → clean
