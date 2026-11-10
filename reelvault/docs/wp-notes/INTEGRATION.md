# Integration pass: notes (2026-11-10)

The seven packages were joined and run end to end. Contract-level results are in DESIGN-SAAS.md 15.1 (D51-D85);
this file says how to repeat the run and what is still open.

## How to run the journey
`tests/journey/` (not collected by pytest; about 15-30 minutes):
```
cd reelvault
RV_PYTHON=$(which python) RV_MODELS_DIR=/path/to/models RV_JOURNEY_OUT=/tmp/rv-journey \
NODE_PATH=$(npm root -g) node tests/journey/journey.mjs
```
- `journey_server.py`: dev server, web + in-process worker (`create_app("all")`), `RV_TEST_PACING=1`. Harness-only
  overrides in that process: the server's CDN download goes to the fake Instagram (`jobs_media.TRANSPORT`), the SSRF
  guard lets the website importer read the local test site on 127.0.0.1, and a fake OpenAI-compatible AI answers
  (WP5's fake answers; quick tags follow each reel's local format guess).
- `journey_ig.py`: WP2's fake Instagram with 3 competitors x 250 reels (planted outliers), real synthetic videos
  from `tests/fixtures/media/`, signed links valid for 7 days from now; the CDN refuses every 5th video to the
  server (403), so the extension's `ig_videos` path runs too.
- `journey.mjs`: Playwright with the REAL extension (`BUILD = "test"`), through the web UI: admin invite and
  settings, signup, onboarding 1-5 (website import, popup pairing, account confirm, API key, 4 questions, first look),
  a 3rd competitor, next 100 for each, scoring, media + signals + tags, insights, playbooks, deep reads, the four
  outputs and their downloads, Remake this; then the Claude connector and the desktop app.
- `journey_mcp.py`: the official `mcp` client with its own `OAuthClientProvider` (401 → metadata → DCR → consent →
  token), then `list_pending_work` → `get_tag_batch`/`save_tags`, `get_reel_for_analysis`/`get_more_frames`/
  `save_analysis`, `get_output_brief`/`save_output`, `get_next_task` + the save tools. The "model" is a stand-in
  (WP5's fake answers); a real claude.ai chat is not possible on this machine.
- `journey_desktop.py`: `members_worker.pair` with the web UI's code, then `Worker.tick()` with the real Claude Code
  CLI on this machine's login.

## Fixed in this pass (see 15.1)
- D54 `Me.skool_url` (onboarding step 1 pre-fills the Skool profile).
- D59 the media share counts processed reels only (150 skipped fetches had used a member's whole day).
- D77 a downloaded reel is processed before the next download (sample processing waited behind every download).
- D78 API-key tag slots gather up to 10 reels while media work runs (was 1 reel per AI call).
- D79 honest ETA (a stale share said "19 hours" for 4 minutes of work; downloads now count).
- D80 OAuth cleanup in the daily sweep; `plugin/` shipped in the image; `tests/conftest.py` so the suite also runs
  from the repository root (`pytest reelvault/tests` / `python -m pytest reelvault/tests`).
- D81 tags queue the debounced insights refresh (insights and formats stayed at 2 tagged reels during tagging).
- D83 Retry on a failed tag batch re-queues its reels on the member's current AI (it re-ran an empty batch).
- D84 failed tags are no longer "left to tag" forever; sidebar says "Working on N jobs"; an output waiting for
  Claude names the right thing to say ("Write my ReelVault output out_x").
- D85 Home lists only formats that beat normal; lift under 1 reads "Less likely"; the setup checklist is fresh.
- D82 UI and copy: competitor page shows only its own pull; first-look title names every account; editor brief
  music and caption lines, the reel's audio line and weak-spot names in plain words.
- D79 also: a member with queued processing but no usable share gets one within 15 s, not up to a minute.

## What the journey showed (run of 2026-11-10, this machine: 4 CPUs, models from deploy/fetch_models.sh)
- Signup to first look took about 8 s with the test build's short Instagram gaps (real pacing adds minutes):
  "Your first look: 10 outliers from @smile.marketing and @dental.growth.co".
- 3 competitors x 200 reels (first 100 + next 100 from the saved cursor, `target_reached` each): 22 outliers found
  (2 breakout, 10 big, 10 outlier) out of 24 planted.
- 211 reels (60 priority + 151 sample) downloaded (server CDN path, every 5th through the extension), measured
  (cuts, sound map, VisualStats, transcript) and tagged in about 15 minutes, every tag call a full batch.
- Lift came out where the videos put it: green screen 2.84x and text on B-roll 3.08x (the outliers' formats), talking
  head 1.37x, slideshow 0.28x, split screen 0.09x; 3 named sub-format playbooks.
- All four outputs, 16 downloads (md, txt, csv, print) and Remake this; no console error or 5xx on any page.
- Claude connector: the SDK's OAuth flow, 20 tools, 2 tag batches, 2 deep reads with extra frames, a Meta ads
  output and a format-naming task, largest result 24,249 characters.
- Desktop app with the real Claude Code CLI 2.1.322: a deep read through `schema.json` (56 s) and a tag batch (8 s;
  it tagged the cartoon clip as animation, VF19, with 5 weak spots). Separately, on a copy of the journey's data, a
  failed tag batch was retried from the web UI (D83) and the real CLI tagged it.
- Final run: 36 of 36 steps passed (`journey-results.json` in `RV_JOURNEY_OUT`).

## Still open
- The fake AI names every format group the same ("Desk talking head + top headline box"), so the journey's
  playbook and reel pages can't show whether format naming reads well; that needs a real model (14.2 step 9).
- Nothing ran against real Instagram, real claude.ai or real AI provider keys (owner live tests, 14.2).
- The media pool also runs downloads: a download holds a lane through its 2-5 s CDN gap. D77 keeps results flowing,
  but a separate light "download" lane would roughly double media throughput on a busy server.
- `suggest_topics` from reels runs at 30 tagged reels and every 30 more (WP5's approximation): a first big pull
  gives the member several suggestion rounds in a row.
- A failed quick-tag batch (`invalid_output` twice) leaves its reels `failed` until the member presses Retry
  (which now works, D83); nothing retries it by itself.
