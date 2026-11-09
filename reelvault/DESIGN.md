# ReelVault 0.4: one app + four purposes (build blueprint)

This is the contract between the backend and the UI. Keep to it exactly. If you must
change it, update this file in the same change.

## Goals
1. **One app.** The local web app (`python -m reelvault.app`, http://127.0.0.1:8765) serves a
   single-page UI for everything: Home, Library, Reel page, Studio, Settings. The old
   control panel (`PANEL` string in app.py) and the separate static library page stop being
   the main UI. `site.py` stays only to export a portable page (used later for share packs).
2. **Four purposes.** Every reel has one or more purposes. The AI guesses; the user confirms
   or changes with one tap. Changing never deletes anything.
3. **Self-update.** The app updates itself from GitHub with one click (and tells the user
   when an update exists), so the user never pastes into PowerShell again.

## The user
Non-technical owner of a paid-ads agency (scaleitads.com), Windows + Chrome, no command line.
UI copy: plain, short sentences, active voice, simple words, no jargon. Buttons say exactly
what happens ("Get new reels", "Make it better"). Errors say what went wrong and what to do.

## Purposes
| id | Label | When | Shows first on the reel page |
|---|---|---|---|
| `learn` | Learn it | saved for the advice/information | 3-line summary, key takeaways, "Do this" checklist (tickable), quotes, "Check before you trust" (claims_to_check), "Explain it to a client" (client_explainer) |
| `hook` | Borrow the hook | the first seconds stopped you | hook card (spoken, on-screen text, visual, hook_type), why it works, hook formula (fill_in_template first line or derived), 5 hook_variations (copy buttons) |
| `format` | Use the format | you like how it is built | structure_name, beat map (meat with timestamps + visuals), pacing, retention devices, fill_in_template, 3 script_ideas |
| `remake` | Remake it better | you want your own stronger version | if a remake exists: core idea, what's weak, your angle, script, shot list, caption, too-close flags; else a "Make it better" button (for: me / client name) |

Constants live in `reelvault/purposes.py`:
```python
PURPOSES = {"learn": "Learn it", "hook": "Borrow the hook", "format": "Use the format", "remake": "Remake it better"}
```

## Data model
### `schema.py` (new reels get these in the SAME Claude call; no extra usage)
Add to `ReelAnalysis`:
- `purpose_guess: PurposeGuess` with `purpose: str` (one of learn|hook|format|remake) and `reason: str` (one short line, why).
- `claims_to_check: List[str]` (numbers/claims to verify before repeating them; empty if none)
- `client_explainer: str` (2-3 sentence plain explanation a business owner could paste to a client)

Keep the Windows command-line limit in mind: `brain.compact_schema` must stay under ~4,500 characters
(test exists in tests/test_brain_cmdline.py: keep it passing).

New model `RemakePack` (generated on demand, separate Claude call):
`core_idea: str`, `whats_weak: List[str]`, `your_angle: str`, `script: str` (with [VISUAL] cues),
`shot_list: List[str]`, `caption: str`, `hashtags: List[str]`, `too_close: List[TooClose]`
(`line: str`, `why: str`), `made_for: str`.

New model `PurposeGuesses` (batch guess for old reels): `guesses: List[{id: str, purpose: str, reason: str}]`.

### `library.py` (SQLite `items` table): add columns via safe migration (ALTER TABLE if missing)
- `purposes TEXT` JSON list of purpose ids the user confirmed or accepted (default `[]`)
- `purpose_guess TEXT` JSON `{"purpose": "...", "reason": "..."}` or NULL
- `purpose_confirmed INTEGER` 0/1
- `extra TEXT` JSON object for user state: `{"todo_done": [indexes], "remakes": [RemakePack+{"created_at"}]}`

On `file()` (new reel): copy `analysis.purpose_guess` into `purpose_guess`; `purposes = [guess]`, `purpose_confirmed = 0`.
Old reels without a guess: `purpose_guess` NULL until the batch guess runs.

## HTTP API (all JSON; same localhost/origin rules as today)
Existing endpoints stay and keep working: `/api/status` (add `"version"` already there),
`/api/run`, `/api/links`, `/api/retry`, `/api/remix`, `/api/ig/*`, `/api/ext/*`, `/api/claude/signin`,
`/api/schedule`, `/api/profile`, `/api/quit`, `/vault/<path>` (media files).

New:
- `GET /api/reels` -> `{"reels": [ReelSummary...], "categories": [...], "creators": [...]}` newest first. ReelSummary:
  `{id, title, owner, url, category, subcategory, tags, duration, thumb, posted_at, analyzed_at,
    purposes, purpose_guess, purpose_confirmed, summary, hook: {spoken, on_screen_text, hook_type},
    structure_name, cta_type, content_format, has_remake}`.
  `thumb` is a URL path under `/vault/...` (cover.jpg or a frame) or null.
- `GET /api/reels/<id>` -> full record: everything in the analysis.json record plus
  `purposes, purpose_guess, purpose_confirmed, extra, media: {video: url|null, thumb: url|null, frames: [url...]}`.
- `POST /api/reels/<id>/purposes` body `{"purposes": ["hook","format"]}` -> saves, sets confirmed=1. At least one purpose.
- `POST /api/reels/<id>/todo` body `{"index": 2, "done": true}` -> ticks an action-plan step.
- `POST /api/reels/<id>/remake` body `{"for": "me" | "<client name>", "notes": "optional"}` -> starts a background job
  (uses the job system and log), stores the RemakePack in `extra.remakes`, adds `remake` to purposes.
- `POST /api/sort/guess` -> background job: one batch Claude call (text only, low effort) guesses purposes for every
  done reel whose `purpose_guess` is NULL, using title, summary, hook, structure_name. Saves guesses (does not confirm).
- `GET /api/banks/hooks` -> `{"hooks": [{reel_id, title, owner, spoken, on_screen_text, visual, hook_type, why_it_works, formula, variations: [...]}]}`
  from reels whose purposes include `hook` (query `?all=1` = every reel).
- `GET /api/banks/formats` -> `{"formats": [{reel_id, title, owner, structure_name, beats: [purpose...], pacing, template, script_ideas}]}`
  from reels whose purposes include `format` (`?all=1` = every reel).
- `GET /api/todos` -> open "Do this" steps from `learn` reels: `[{reel_id, title, index, step, why, effort, done}]`.
- `GET /api/update` -> `{"current": "0.4.0", "latest": "0.4.1"|null, "available": bool, "error": str|null}` (cached 1 hour).
- `POST /api/update` -> downloads the newest code and restarts the app (see Self-update). Returns `{"ok": true}` then the app exits.

All new endpoints: errors return `{"error": "plain message"}` with a 4xx/5xx code; never crash the server.

## Self-update (`reelvault/update.py`)
- Source: `https://raw.githubusercontent.com/akshaykiriti1443-alt/linkedin/<branch>/reelvault/reelvault/__init__.py`
  for the latest version; the code zip from `https://github.com/akshaykiriti1443-alt/linkedin/archive/refs/heads/<branch>.zip`.
  `<branch>` comes from config `update_branch` (default `claude/kind-dijkstra-0o03in`).
- Compare versions numerically (0.4.10 > 0.4.9).
- Apply: download + unzip into `HOME/source` (replace), then start a detached helper process
  (`python -m reelvault.update --apply <src_dir> --port <port>`) using the app's own `sys.executable`.
  The helper waits until the app has quit (port free, up to 60 s), runs
  `python -m pip install --upgrade <pkg>` then `python -m pip install --force-reinstall --no-deps <pkg>`,
  logs to `HOME/update.log`, then relaunches the app with `pythonw -m reelvault.app` (Windows; `python` elsewhere).
  The app quits right after starting the helper. Windows flags: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`.
- If anything fails, the old version keeps working; the panel shows the error from update.log on next start.
- Never update while a job is running (return an error asking to wait).

## UI (`reelvault/ui/index.html`, `ui/app.css`, `ui/app.js`; plain JS, no build step, no CDN required)
Served by the app: `GET /` -> index.html, `GET /ui/<file>` -> static files. Add `ui/*` to package-data.
Hash routing: `#home`, `#library`, `#library/learn|hooks|formats|remakes`, `#reel/<id>`, `#studio`, `#settings`.
One look everywhere (reuse the current token palette, light + dark via prefers-color-scheme). Left sidebar on
desktop, bottom/top bar on narrow screens. Visible version in the corner. "Update available" banner when `/api/update` says so.

- **Home**: setup lights (Claude, Instagram, Chrome extension; each links to Settings if not ready);
  "Get new reels" (count box, default 20) + "Analyze links" (textarea, collapsed);
  **Sort new reels** strip: unconfirmed reels with the AI guess pre-selected as one of 4 chips + reason; tap chips to
  change, "Looks right" confirms; "Guess purposes for older reels" button when some have no guess;
  **This week**: open "Do this" steps (tickable); activity (live log, collapsible), job status.
- **Library**: tabs All / Learn it / Hooks / Formats / Remakes. All + Learn = card grid (cover, title, creator, purpose
  chips). Hooks = hook cards (spoken line big, type, why, copy buttons, "Use in Studio"). Formats = format cards
  (structure name, beat chips, template preview, "Use in Studio"). Remakes = reels with a remake. Search box + topic
  and creator filters.
- **Reel page**: left = video (or cover linking to Instagram), title, creator, topic, purpose chips (multi-select,
  saves immediately), "Watch on Instagram". Right = one section per purpose in purpose order, then **More**
  (collapsed: full skeleton, visuals, transcript with timestamps that seek the video, caption, why it performed).
  Remake section: "Make it better" with a "For: me / client name" field; shows progress while the job runs.
- **Studio**: the existing "Write new scripts" (topic, category, count) moved here, plus "Start from" pickers listing
  hooks/formats from the banks (the chosen hook text and format name are passed as extra context in the topic for now).
  Result opens inline (load the markdown from `/vault/scripts/...` and show it as preformatted text with Copy).
- **Settings**: Claude sign-in, Instagram connect (+ Chrome extension setup card + "Other ways"), About you (profile),
  daily run, updates ("Check for updates" / "Update now"), version, "Close app".

Every existing capability of the old panel must exist somewhere in the new UI (nothing lost).

## Tests
Keep all current tests passing. Add tests for: library migration (old DB gains columns, data kept), purposes API
(set/confirm, at least one), todo ticks, banks, update version compare + "update not while job running",
remake job stores a pack (Claude call faked), batch guess (Claude call faked), schema size limit.
