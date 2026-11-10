# WP7 notes: onboarding, importers and the member web app

Status: done. Full suite from the project root (`cd reelvault && python -m pytest -q tests`): 576 passed, 8 skipped
(the skips are WP1 deploy-kit and WP3 model tests), including the web UI run below.

## What is here

| file | what |
|---|---|
| `reelvault/importers/{skool,website,linkedin,instagram,merge}.py` | 9.2 importers, the 9.1 questionnaire (exact copy), answer checks, the deterministic ProfileDigest (9.4) |
| `server/api_profile.py` | 4.4 profile, sources, draft, approve, topics; `GET /api/copy` (7.3.4); two additions below |
| `server/jobs_profile.py` | `import_source` (website, Skool fallback A, LinkedIn PDF) |
| `web/index.html`, `web/static/app.css`, `app.js`, `theme.js`, `views/*.js` | the SPA (plain ES modules, no build step, no CDN) |
| `web/copy/connector.json` | default Claude-connector setup copy (admin overrides in Admin > Copy) |
| `tests/server/test_{importers,profile}.py` | importer and API tests |
| `tests/web/serve.py`, `ui.test.mjs`, `test_web_ui.py` | seeded test server + fake extension; Playwright for Node UI tests; pytest wrapper |

### SPA layout
`app.js` holds the shared parts: `h()` (builds DOM, only ever sets text, so competitor captions can't inject markup),
`api()` (CSRF header, plain errors, 401 -> log in), the hash router, the nav shell, job polling (3 s while work runs,
10-30 s otherwise; `onJobs` / `onJobsDone` page hooks), `reelCard`, `buildOn` (Remake this · Use this format · Borrow
this hook -> `POST /api/outputs/quick` -> `#output/<id>`), `jobLine` (Stop / Try again). Views:
`auth` (join, login, reset, re-confirm), `onboarding` (5 steps), `home`, `competitors` (+ competitor, discover tick list),
`reels` (filters incl. purpose chips; reel page with tabs, frame strip, cue timeline with play-at-cue, Fix tags),
`working` (what works, `#format/<id>` playbook, banks), `outputs` (create forms, list, output page with live
"Waiting for Claude" state, cards per kind, downloads), `settings` (+ `ai`, `extension`, `profile` modules), `admin`,
`help` (privacy, ZIP install guide, second-account guide). Every route in section 13 "WP7" exists.

CSP (`style-src 'self'`) is respected: no inline `<style>` or `style=""`; dynamic styles go through `el.style`.
Light/dark follows the system; Settings > Account can force one (`localStorage rv.theme`, applied by `theme.js`
before paint).

`<html data-view="ready" data-ready="<hash>">` is set when a page has drawn; the UI tests wait on it.

### Tests (evidence)
`node --test tests/web/ui.test.mjs` (or `pytest tests/web`), about 80 s, ports 9241/9242/9251:
- every member screen (32 routes + competitor, discover, format, 2 reels, all 7 reel tabs, 4 outputs) and every admin
  tab, at 1280 px and 390 px, light and dark: a page title, no console errors (4xx "Failed to load resource" lines
  are allowed, they are handled answers), no 5xx, no sideways scroll, and the dark background really applies;
- signup with the community code -> "You're on the list" -> admin approves in Admin > Members -> login lands on
  onboarding step 1 -> role model + own Instagram -> step 2 pairs through the FAKE EXTENSION (the code shown in the UI),
  "ReelVault will read Instagram as @member.reader" -> Continue -> "Read my links now" -> "Find more accounts like ..."
  -> the fake extension runs the pull, the bio, the discovery and the checks -> tick list -> Track -> step 3 saves an
  API key through the form (fake OpenAI-compatible server) -> step 4 refuses review until q1/q3/q7/q12, then review and
  "This is right" -> step 5 shows "Your first look: N outliers from @other.creator" with build-on buttons -> Home;
- seeded member: Home format card -> playbook -> "Write 3 scripts in this format" -> script written by the fake AI;
  reel page -> "Remake this" -> script; txt/csv/md/print downloads answer 200; Fix tags saves an override; band filter;
- `#login?next=https://evil...` never leaves the site (only `/oauth/authorize?` is followed, WP6 round trip).

`tests/web/serve.py --port 9241 --data-dir <dir>` also serves the seeded app for manual looks (member@example.com /
member-pass-123; admin password printed in the READY line).

## Deviations and additions (please fold into DESIGN-SAAS.md)

1. **`POST /api/profile/sources/<id>/read`** (new): reads a link again. Links added in step 1 before Chrome is paired
   are `needs_paste` (4.4 default `via`), so step 2 offers "Read my links now" after pairing (on the member's click),
   and source rows offer "Read it again" (also for a failed website). Same routing as a new source; 409 while reading;
   400 for pasted text or when the extension is needed but not paired; another member's id -> 404.
2. **`GET /api/taxonomy`** (new, session): ids and labels only (visual formats, sound patterns, hook/CTA types, caption
   styles, B-roll/setting/framing choices) for the Fix tags editor, the reel filters and format labels. No cues or
   prompt text. Reads WP5's `taxonomy.json`.
3. Skool parser: a page without a group now takes `currentUser` as the page's person even without `profileData`
   (the extension's upload of the member's own @profile can lack it; WP2's e2e test needed this). A group page's
   viewer is still never used.
4. Voice words in the digest drop trailing punctuation ("Never: leads." -> `leads`).
5. Onboarding progress is derived from `/api/me` `setup`; "finished onboarding" is remembered per browser
   (`localStorage rv.onb.<user>`). New members with nothing set up land on `#onboarding/1` after login.
6. Signup's `skool_url` can't be pre-filled in step 1: `/api/me` doesn't return it (WP1). If WP1 adds
   `skool_url` to `Me`, `onboarding.js` already uses `S.me.skool_url`.
7. Once format playbooks exist they are Home's first card; the "first look" block then only shows while tags are
   still coming (it duplicates "New outliers" otherwise).
8. Admin "Who gets in with the community code" offers the server's two values (`pending`, `active`).

## Test-server only (never in production)
`tests/web/serve.py` sets `SERVER_CDN_FETCH=0`, raises the per-member 300 calls/minute limit to 5,000 (the sweep loads
about 60 screens a minute as one member), and runs a FakeExtension control server (`POST /claim {code}`,
`POST /work {device_id}`) that talks to `/api/ext/v2/*` with a real device token and WP2's fixtures.

## Not done / not tested here
- The real Chrome extension and real Instagram (WP2's e2e covers the extension; here a fake one).
- Deep reads for the NEW member's reels (no videos on the test server), so "tags -> format page -> Remake" runs on the
  seeded member, whose tags, deep reads and named formats are written by the seeder through WP5's `save_result`.
- The "Use my Claude plan" path end to end (needs claude.ai); the page, copy buttons, clients list and Disconnect
  call WP6's endpoints, and the output page's "Waiting for Claude" state shows when `job.executor == "mcp"`.
- Running `pytest reelvault/tests` from the repo root (`/home/user/linkedin`) fails in the desktop tests with
  `cannot import name '__version__'` (the outer folder becomes a namespace package). Pre-existing, noted by WP2/WP5;
  run from the project root.

## Fix round 1 (2026-11-10)
- `app.js` makes every `Element.append` / `DocumentFragment.append` skip null, undefined and false (and flatten
  arrays), like `h()` and `clear()`; the stray "null" lines on onboarding step 4 and the reel tabs are gone. The UI
  test now fails if `main` shows the word null, undefined or NaN on any screen.
- Competitors: "Reading now" only while the pull job runs; otherwise "Waiting for Chrome" (no extension or offline)
  or "Up next", plus a banner that links to Settings > Chrome extension. Toast and job message match.
- New `#help/desktop-app` guide (steps and the one-paste install command from connector.json
  `desktop_install_steps` / `desktop_install_command`, editable in Admin > Copy), linked from the desktop panel.
- Download labels by kind; plural-aware queue lines; Home shows "Chrome is paired but not open" when the device is
  offline and hides a job message that the ETA line repeats; tab bars wrap under 640 px; Admin > Health handles the
  community stats "off" answer (200) and shows a failed off-site copy.
- Harness: `tests/web/serve.py` accepts ports 9000-9999 (never 8765); `RV_WEB_AI_PORT` overrides the fake AI port.
