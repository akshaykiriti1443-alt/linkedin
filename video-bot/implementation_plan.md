# Implementation Plan — Smart Layout Switcher

## The Logic
Two modes, auto-switched per segment:

  MODE A (no screen recording for this range):
    V2 = Vox 3D animations (top half)
    V1 = talking head (bottom half)
    A1 = voice

  MODE B (screen recording exists for this range):
    V1 = screen recording (full frame)
    V2 = talking head PiP (small, bottom-right corner)
    A1 = voice from talking head

## How we detect which mode per segment
  - User supplies: talking head video + (optional) screen recording video
  - Screen recording has its own start timestamp (user sets offset, default 0)
  - generate_xml.py compares each keep-segment against screen recording duration
  - If screen.mp4 covers that time range → MODE B, else → MODE A
  - If no screen.mp4 provided at all → always MODE A

## Files to touch

### [MODIFY] src/generate_xml.py
  - Add --screen-recording flag (optional path)
  - Add --screen-offset flag (float, seconds, default 0.0)
  - For each keep segment: check if screen recording covers [start, end]
  - Output TWO tracks on V1:
    - MODE A segments: talking head clip with bottom-half motion params
    - MODE B segments: screen recording clip at full frame
  - Output PiP track on V2 for MODE B segments (talking head small, bottom-right)
  - Output Vox placeholder marker track on V2 for MODE A segments

### [MODIFY] auto_edit.py
  - Add --screen argument (optional)
  - Add --screen-offset argument
  - Pass both to generate_xml.py
  - Print mode-switch summary (which segments are screen vs vox)

### [MODIFY] src/build-timeline.ts
  - Read mode metadata from workspace/segments_meta.json (written by generate_xml.py)
  - For MODE A segments: place Remotion Vox .mov on V3 at those timecodes
  - For MODE B segments: place talking head PiP at correct timecode, no Vox overlay

### [NEW] workspace/segments_meta.json (runtime artifact)
  - Written by generate_xml.py
  - Shape: [{start, end, mode: "vox"|"screen", screen_in, screen_out}]

### [MODIFY] profiles/talking-head.json (new profile)
  - layout: smart, screen: null, vox_style: true

### [MODIFY] .claude/commands/produce.md
  - Document --screen flag

## Execution order
1. generate_xml.py — smart mode switching logic
2. auto_edit.py — pass --screen arg through
3. build-timeline.ts — read segments_meta.json
4. profiles/talking-head.json — new profile
5. produce.md — update docs

## Verification
- python src/generate_xml.py with no --screen → same output as before (MODE A always)
- python src/generate_xml.py --screen screen.mp4 → dual-track XML
- npx tsc --noEmit → clean
