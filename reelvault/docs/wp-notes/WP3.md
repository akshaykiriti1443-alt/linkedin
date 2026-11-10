# WP3 Media, sound and visual signals: notes

Status: done. `tests/server/test_signals.py` has 57 tests. With the models, all 57 pass. Without them, 51 pass and
6 are skipped (CED-tiny, YuNet, whisper). The full server suite has 245 passed and 1 skipped (WP2's zip route). The
desktop suite (`tests/test_*.py`) has 51 passed. Nothing is committed.

## Files

| file | what |
|---|---|
| `reelvault/signals/__init__.py` | `process()`, `Signals`, `probe()`, `check_video()`, `VideoRejected`, `SIGNALS_VERSION` |
| `reelvault/signals/cuts.py` | scdet cut detection (`detect_cuts`, `frame_scores`, `pick_cuts`, `shots`) |
| `reelvault/signals/sound.py` | CED-tiny tagger + numpy DSP + fusion (`analyze`, `fuse`, `SoundRaw`) |
| `reelvault/signals/soundmap.py` | SoundMap JSON: cut alignment, music and speech, ducking, summary, SD hints |
| `reelvault/signals/editstats.py` | EditStats JSON |
| `reelvault/signals/visual.py` | VisualStats JSON, local format guess, `local_rules` loader |
| `reelvault/signals/frames.py` | `jpeg_for`, `sheet_for`, `sheet_tiles`, `key_frames`, `deep_frames`, budget specs |
| `reelvault/media.py` (additions only) | `cut_frame_times`, `extract_masters`, `contact_sheet`, `transcribe_server`, `server_whisper`, `whisper_path`, `speech_spans`, `EMPTY_TRANSCRIPT` |
| `reelvault/server/jobs_media.py` | handlers `fetch_video`, `process_reel` + helpers |
| `reelvault/data/taxonomy.json` | the `local_rules` block only (see "taxonomy.json" below) |
| `deploy/fetch_models.sh` | added a load test for CED-tiny and YuNet at the end |
| `tests/fixtures/make_media.py`, `tests/fixtures/media/.gitignore` | synthetic test reels (generated, never committed) |
| `tests/server/test_signals.py` | unit tests and job handler tests |
| `reelvault/server/bench.py` (WP1 file, 6-line fix) | see "Touched outside WP3" |

## Interfaces other packages use

```python
# signals
from reelvault import signals
sig = signals.process(video, reel_dir, *, ig_audio=None, words=False, whisper_model="base", has_audio=None,
                      whisper_threads=2, whisper_workers=1, transcribe=None, progress=None) -> Signals
sig.to_row()   # reel_signals columns: version, duration, width, height, fps, transcript, cuts, frames,
               # contact_sheet_path, sound, edit, visual, error
sig.skipped_asr, sig.has_audio, sig.timings   # timings: probe, cuts, frames, sound, transcript, soundmap, visual, sheets
signals.SIGNALS_VERSION   # 1; bump when the pipeline changes (dedupe keys use it)
signals.VideoRejected(message, code)          # codes: too_long, too_big, unreadable, empty, missing, no_frames

# frames / image budget (6.1.1); reel_dir = jobs_media.reel_dir(uid, rid) = users/<uid>/reels/<rid>/
from reelvault.signals import frames
frames.sheet_for(reel_dir, "paid_api" | "small" | "mcp", *, cap_bytes=None, refresh=False) -> bytes
frames.sheet_tiles(reel_dir, dest, *, cap_bytes=None) -> [{"tile": 1, "n", "t", "kind", "path"}]   # for prompt text
frames.jpeg_for(master_path, dest, *, cap_bytes=None) -> bytes
frames.deep_frames(reel_dir, dest, n, *, cap_bytes=None) -> [(frame, bytes)]   # mcp n=2: 1.0 s hook + best body frame
frames.key_frames(frames_list, visual) / frames.best_body_frame(frames_list, visual)
frames.SHEETS, frames.FRAMES (caps per destination), frames.MCP_DEEP_SHEET_CAP (28 KB)

# jobs (media pool)
from reelvault.server import jobs_media
jobs_media.queue_fetch(user_id, reel_id, *, priority=50, conn=None) -> job_id     # dedupe "fetch_video:<rid>"
jobs_media.queue_process(user_id, reel_id, *, words, priority=50, conn=None) -> job_id  # "process_reel:<rid>:v1"
jobs_media.signals_for(user_id, reel_id) -> dict | None      # the member's reel_signals row
jobs_media.reel_dir(user_id, reel_id) -> Path
```

### For WP5 (pipeline, work)
- Both handlers call `pipeline.advance(user_id, reel_id)` when they finish, including when they fail or skip. A
  `NotImplementedError` from the stub is ignored.
- `pipeline.advance` should queue jobs with `jobs_media.queue_fetch` / `queue_process`, so the dedupe keys match.
  Once a job ends, its dedupe key is cleared, so you can queue the reel again later.
- **Words later.** A sample reel is processed without word times (model `base`). If a deep read is asked for later,
  check `transcript["words"]`; if it is empty, queue `process_reel` again with `words=True`. Also check
  `transcript["model"]`, which is the model that ran (`None` when speech-to-text was skipped).
- **process_reel results**
  - Done: `signals_state=done`, and `reels.duration` / `has_audio` are filled when they were empty.
  - Video too long, too big or unreadable: `signals_state=failed`, the reason is in `reel_signals.error` in plain
    words, and the job itself ends as `done` with `result.error`.
  - Crash on the last attempt: `signals_state=failed` with "ReelVault could not read this video. It will be tagged
    from its caption and numbers."
  - Video file missing (for example removed by the retention sweep): `media_state` goes back to `none` and
    `advance` runs, so the video is fetched again.
- Fields the tag prompt uses:
  - `visual.local_vf` (at most 3 `{id, p}`) and `visual.local_recipe` (`headline_box`, `face_bubble`,
    `caption_band`).
  - `sound.summary`, `sound.pattern_hints`, `sound.ig_audio`; `edit`; `transcript.text`.
  - `visual.per_frame` (face, text and UI number for each frame) and `visual.sheet_bytes` are extra keys. Leave
    them out of prompts.
- "Agreement with a high-confidence local guess" (6.5) uses `local_vf.p >= 0.8`. With the default rules, only VF05
  reaches 0.8.

### For WP4 (reel API)
- The `signals` block of `GET /api/reels/<id>` is `jobs_media.signals_for(uid, rid)`.
- Frame paths are relative to the reel folder (`frames/f00_000.20.jpg`). Turn them into URLs with
  `storage.files_url(uid, f"reels/{rid}/{path}")`.
- The cached sheets are `reels/<rid>/sheet_<dest>.jpg`, each with a `sheet_<dest>.json` that maps tiles to times.

### For WP2
- When the server cannot download a video, `fetch_video` sets `media_state=need_extension` and calls
  `jobs_ig.schedule_need_extension(user_id)`. A test checks that an `ig_videos` job appears.
- The cases are: CDN answers 403, 404 or 410; the `oe` link expired; the host is not `*.cdninstagram.com` or
  `*.fbcdn.net`; there is no URL; `SERVER_CDN_FETCH=0`.

### Models (`RV_MODELS_DIR`, layout from `deploy/fetch_models.sh`)
- Paths: `ced-tiny/{model.int8.onnx,class_labels_indices.csv}`, `yunet/face_detection_yunet_2023mar.onnx`, and
  `whisper/hub/models--Systran--faster-whisper-{small,base}/snapshots/<rev>` (found through `refs/main`).
- Whisper loads from that snapshot path. When the snapshot is missing, it falls back to the model name, which
  downloads it. Dev boxes only.
- Each media thread has its own CED and YuNet instance. All threads share one whisper model per name
  (`num_workers = WHISPER_CONCURRENCY`).
- Without CED: `detector="dsp"`, there are no music or label events, and speech-to-text is not gated, so it always
  runs.
- Without YuNet: every face number is `null`, `face_detector="none"`, and the face rules of the local guess never
  match.

## Measured (this box; `taskset -c 0,1` = 2 cores; models already loaded)
- 60 s reel (speech + SFX + speech):
  - Priority (whisper small + word times): 13.9 s. Steps: cuts 1.4, frames 1.45, sound 2.0, transcript 6.9,
    visual 1.55, sheets 0.5. Target: under 45 s.
  - Sample (base, no word times): 8.7 s. Target: under 15 s.
  - Loading small + base once: 13.7 s.
- 15-24 s clips without speech: 3.3-4.0 s each. Clips with no speech skip speech-to-text.
- Cuts: all 30 fixture cuts found within 20 ms, with no extra cuts. scdet at threshold 8 alone found 7 of 30 (zoom
  jump cuts score 2.2-2.6; slide changes 0.5-4.4).
- Sheet sizes, against these caps: paid_api 160 KB, small 120 KB, mcp 32 KB.

  | clip | paid_api | small | mcp |
  |---|---|---|---|
  | screen (the heaviest) | 137 KB | 95 KB | 26.7 KB |
  | talking_head | 72 KB | 44 KB | 15.5 KB |
  | greenscreen | 83 KB | 53 KB | 16.8 KB |
  | speech | 39 KB | 23 KB | 7.8 KB |

- MCP tag batch (the 3 heaviest clips, with a 600-character caption and the numbers): 89,565 characters. Deep
  payload (28 KB sheet + 2 frames + 18,000 characters of text): 68,284 characters.
- Sound on `sfx_cuts`:
  - Silence 16.03-16.77 (truth 16.0-16.8). Riser 9.58-11.54 (truth 9.6-11.6).
  - All 4 whoosh peaks are within +0.1 s. All 3 hits are within 20 ms.
  - Beep (7.88 s) and ding (14.18 s) are labelled by CED.
  - 8 of 9 cuts have SFX (89%); the bare cut stays bare.
  - Hints: SD06, SD04, SD07, SD08, SD09.
- Visual guess on the fixtures:

  | clip | numbers | guess |
  |---|---|---|
  | screen | `ui_frames_share` 1.0; face bubble found | VF05 |
  | split | `split_share` 1.0, 2 panes | VF06 |
  | greenscreen | `cutout_share` 0.97 | VF04 |
  | talking_head | `face_share` 1.0; headline box; captions at the bottom | VF01 |
  | slideshow | `static_frame_share` 0.94 | VF09 |

## Deviations from DESIGN-SAAS.md (please fold in)
1. **Cuts (6.3).**
   - A frame counts as a cut when its scdet score is at least 8 (the design's rule), or when it is the highest score
     within ±0.25 s, at least 1.2, and at least 4 times the 90th percentile of the scores within ±1 s.
   - Between frozen frames (slides), any local peak of at least 0.4 counts.
   - No frame is subtracted: ffmpeg 6.1 scdet puts the score on the first frame of the new shot (measured).
2. **Sound fusion (6.4 step 4) additions.**
   - The beat grid also covers bar-length periods (0.25-4.2 s) and the half-beat phase. Only isolated transients
     count, so typing runs are never "music".
   - Strong transients on the grid survive when their flux is at least twice the grid's drums. A run of
     similar-strength transients every 1, 2, 4 or 8 beats is treated as music.
   - Hits found only by DSP are dropped inside speech (plosives).
   - A DSP whoosh must take at least 80 ms to climb its last 11 dB, so a hit's crack is not read as a whoosh.
   - The riser start is refined (a step of at least 3 dB, else a hinge fit).
   - ML "whoosh" and "sound effect" events that overlap a DSP whoosh or riser merge into it.
   - An ML event with no DSP onset is kept only when its score is at least 0.15 and its label is a reliable one.
3. **SoundMap extra keys:**
   - `music.under_speech_pct` (music under speech, used by SD02), `loudness` (RMS per 0.5 s), `ambient_pct`, and
     `no_audio` on the empty map.
   - `summary.accents_off_cut`, `riser_into_hit`, `silence_beat` and `music_starts_s`: the rule inputs for SD07,
     SD08, SD09 and SD15.
   - `under_speech_db` is the 10th percentile of 10 ms frames inside speech (the bed in the gaps between syllables).
     `in_gaps_db` is the median level of music outside speech.
4. **VisualStats extra keys:** `face_detector`, `frames`, `cutaway_share` (body frames with no face, used by
   VF01/VF02), `brightness_med`, `speech_ratio` (input to the rules), `per_frame`, `sheet_bytes`.
   - Text is measured on the 12 masters at 480 px wide. Caption bands are top < 30%, middle 30-70% and bottom > 70%.
   - The top headline box and captions are ignored on UI frames.
   - `ui_score` is halved when a frame has fewer than 3 small text boxes, and multiplied by 0.3 when a face covers
     more than 3% of the frame.
   - Split lines are looked for only at 38-62% of the frame.
   - Cut-out "halo": thin near-white or near-black structures must cover at least 1.5% of the person box.
5. **`local_rules`** is a list in `taxonomy.json`:
   - Format rules: `{"id", "p", "all": [[stat, op, value]...], "why"}`, in precedence order.
   - Recipe rules: `{"recipe": "headline_box" | "face_bubble", "when": [stat, op, value]}`.
   - VF01 and VF02 also require `small_face_corner_share < 0.5`, so a face bubble is not a talking head.
6. **Transcript extra keys:**
   - `model` (null when skipped) and `skipped`.
   - Without word times, `speech_spans` come from faster-whisper's own silero VAD, clipped to the segments. Whisper
     segments run into each other (one stretched over 30 s of music and gave 82 wpm instead of about 180).
7. **Files.**
   - `frames/index.json` holds the frame list plus the per-frame numbers.
   - Each sheet has a sidecar `sheet_<dest>.json` (tiles to times).
   - A sheet with a non-default cap is cached as `sheet_<dest>_<N>k.jpg` (e.g. `sheet_mcp_28k.jpg`).
8. **Sheets.**
   - The design says sheets are built with ffmpeg tile from a concat list. They are, with one more rule: the black
     placeholder has the same size as the masters. Otherwise ffmpeg rebuilds the filter graph when the size changes
     and the tile filter starts over (an all-black sheet).
   - Step-down order: JPEG quality up to q=31, then tile size down to 60%, then (paid_api only) the 6-key-frame 3x2
     layout. If the cap still cannot be met, the smallest attempt is returned and logged; the result is never empty.
9. **fetch_video.**
   - Pacing (2-5 s between downloads, 2 at a time per member) holds within one worker process.
   - "Smallest version ≥ 480 px when the URL list is known" is not built: `reels` stores one `video_url`.
   - The cover is downloaded too when `cover_url` is set and there is no cover yet.
10. **taxonomy.json**: WP5 owns it, but it did not exist yet. I created it with only `version` and the `local_rules`
    block.
    - **WP5: keep the `local_rules` block when you write the full file.** `test_local_rules_from_taxonomy_json`
      checks that it equals `visual.rules_json()`.
    - When you tune thresholds, edit both the JSON and `DEFAULT_LOCAL_RULES`, or change the test.
11. The job handler tests live in `tests/server/test_signals.py` (the file WP3 owns), not in a new file.

## Touched outside WP3 (minimal; please review)
- `reelvault/server/bench.py` (WP1), `one_reel()`. Its test `test_bench_quick_run_without_whisper` stopped passing
  once the real `signals.process` existed. The bench now:
  - passes `transcribe=<empty>` when whisper is off;
  - returns `steps` from `Signals.timings`, with `audio` as an alias of `sound`.

## Stubbed or untested
- **Real Instagram reels were never processed** (no Instagram data here). Every threshold was tuned on synthetic
  clips. CED's labels on synthetic clicks are mixed (`click` and `sound_effect`), so the tests check labels only
  for beep, ding and music.
- **Re-measure on real reels.** These are heuristics with known risks:
  - the cut-out halo (a tripod talking head with dark hair edges could trigger it);
  - UI-likeness;
  - the beat-grid rules.

  The thresholds live in `taxonomy.json` so the owner's 30 labelled reels (14.2) can tune them.
- **The speech fixture needs gTTS (network) or `RV_FIXTURE_SPEECH=<audio file>`.** Without it, the speech test is
  skipped. In CI the models are absent unless `fetch_models.sh` runs, so 6 tests are skipped there.
- **Beat detection (SD10)** is off, as the design says (`cuts_on_beat_pct: null`).
- **Not built here:** `get_tag_batch` / `get_reel_for_analysis` themselves (WP5/WP6). The test builds a matching
  payload from these functions and checks that it stays under 90,000 characters.
- **Not tested:** a GPU, Windows, and ffmpeg 7.x (cut timing). If scdet in ffmpeg 7.x stamps one frame late, cuts
  shift by 40 ms, which is still inside the 50 ms tolerance.
