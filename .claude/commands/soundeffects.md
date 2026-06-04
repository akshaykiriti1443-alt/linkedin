# /soundeffects — Generate perfectly-timed SFX track (WAV) for your video

You are a sound designer. Read `video-bot/workspace/sfx_events_draft.json`.

## Steps

1. Read the draft:
   ```bash
   cat video-bot/workspace/sfx_events_draft.json
   ```
   The draft contains every word + timestamp from the transcript.

2. Plan 35–50 SFX placements based on context:
   - `digital_readout` → numbers, stats, tech terms, tool names (max 1 per 7s)
   - `whoosh`          → topic transitions, scene changes
   - `impact`          → dramatic reveals, strong claims, big numbers
   - `ding`            → key points confirmed, success moments
   - `keyboard`        → typing, code, commands, writing
   - `mouse_click`     → UI actions, clicking, selecting
   - `double_click`    → section starts, new topics introduced
   - `notification`    → alerts, pings, "important" moments
   - `camera_shutter`  → showing a result, screenshot, reveal
   - `riser`           → 1–2s before a big reveal (pair with impact)
   - `air_hit`         → punchy intros, action verbs, "boom" moments
   - `swoosh_down`     → endings, closings, dismissals

3. Write `video-bot/workspace/sfx_events.json`:
   ```json
   [
     {"sfx": "digital_readout", "at_ms": 1240, "reason": "said '47 percent'"},
     {"sfx": "whoosh",          "at_ms": 4800, "reason": "topic change to next point"},
     ...
   ]
   ```
   Rules:
   - Minimum 1500ms gap between ANY two events
   - Minimum 7000ms gap between same SFX type
   - at_ms = word.start_ms from the draft

4. Build the SFX track:
   ```bash
   cd video-bot && pip install pydub -q && python engine/build_sfx_track.py
   ```

5. Output: `workspace/sfx_track.wav`

6. Tell the user:
   - **Premiere**: import `workspace/sfx_track.wav` → drag onto **A2 track** at timecode 0
   - The WAV is already mixed and timed — no adjustments needed

## Notes
- SFX files are in `video-bot/sfx/*.wav`
- Replace with real audio from freesound.org for production quality
- Spacing rules are enforced by build_sfx_track.py (events too close will be skipped with a warning)
