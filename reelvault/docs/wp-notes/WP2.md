# WP2 Instagram reader: notes

Status: built and tested. Extension (`extension-members/`), server ingest (`server/api_ext.py`, `server/jobs_ig.py`,
`reelvault/igparse.py`), `igweb.meta_of` additions, fixtures (`tests/fixtures/ig/`), server tests, Node unit tests and
a Playwright end-to-end suite against a fake Instagram.

## How to run the tests

```bash
. /tmp/claude-0/-home-user/4c2cbd5e-b8a1-556b-adde-3bbaa58a25e4/scratchpad/venv/bin/activate
python -m pytest -q tests/server/test_igparse.py tests/server/test_ext_api.py   # server side, ~8 s
node --test tests/ext/unit.test.mjs                                              # pure JS modules, ~1 s
python -m pytest -q tests/ext/test_ext_e2e.py      # unit + end to end (Chromium, fake Instagram, real app), ~75 s
```
The e2e suite needs node 20+ and Playwright for Node with Chromium; it is skipped when they are missing, or with
`RV_SKIP_EXT_E2E=1`. Ports: `RV_E2E_PORT_BASE` (default 9140; uses base+1 for ReelVault, base+3 for the fake).
Run it by hand with `RV_PYTHON=<venv python> NODE_PATH=$(npm root -g) node --test tests/ext/ext_e2e.mjs`.

Files: `tests/ext/fake_ig_server.py` (HTTPS fake of Instagram, its CDN, Skool and LinkedIn, with scenario switches),
`tests/ext/run_server.py` (the real app + `/__test/*` control routes + a log of every `/api/ext/v2/*` request; test
only), `tests/ext/ext_e2e.mjs` (35 cases), `tests/ext/unit.test.mjs`, `tests/ext/trim_dump.mjs` (used by
`test_igparse.py` to prove `trim.js` and `igparse.trim_media` agree), `tests/ext/test_ext_e2e.py` (pytest wrapper).

What the e2e suite proves (each line is a test): nothing is called before pairing; pairing in the popup refuses a
link from a server not in `config.js` and makes no call for it; the real link connects and does one sign-in call;
Instagram jobs wait for the account confirm; first pull = exactly 100 over pages of 50, cursor saved, own pinned
reading tab on `/robots.txt` with `autoDiscardable:false`; requests carry the member's cookies, `X-CSRFToken` =
cookie, `X-IG-App-ID`, `Origin: https://www.instagram.com` (POST) and `Sec-Fetch-Site: same-origin`; one request at
a time with the server's gaps; next pull continues from the saved cursor with reels 101-200; refresh stops at the
first known reel and keeps the cursor; popup status line; video job picks the smallest version >= 480 px, the CDN sees
`Referer: https://www.instagram.com/` and no cookies, a 60 MB file is refused; bigint pk stored exactly, collab kept,
suggested reels dropped; recoveries `page_size_12`, stale cursor -> `feed_oldest_pk` (no double count), cursor error
-> feed, clips 404 -> feed; 429 -> 24 h cooldown, cursor kept, only `page_read` leased, then it goes on to 100; 429
while ReelVault answers 503 -> the extension still keeps its own 24 h stop; login redirect -> signed_out, Try again;
checkpoint / feedback_required / HTML 200 -> challenge / warning / signed_out with the right cooldowns; HttpOnly
`ds_user_id` -> `current_user`; another Instagram account in Chrome -> job back to waiting, re-check after 30 min;
reading tab closed mid-job -> new tab, no cooldown; service worker killed mid-job -> resumes the stored job; budget
120/h enforced by the extension and refused by the server; a compromised server's bad handle/pk/query/page link ->
`bad_job` with zero requests anywhere; ISOLATED world (a page that wraps `window.fetch` never sees the call); profile
lookup; discovery search + check (one page of 12 per candidate, nothing stored as reels); debug samples once per
endpoint with values stripped; `page_read` permission tab and "Not now"; 20 ticks + a real 30 s alarm while idle ->
zero requests to Instagram/CDN/Skool/LinkedIn; no request to ReelVault ever carried a cookie or the session/CSRF
values; web UI Disconnect -> 401 -> popup "Connect again"; Skool `__NEXT_DATA__` (only `pageProps.{currentGroup,
currentUser}`), LinkedIn `/in/me/` text, Skool login page -> paused "Sign in to Skool...".

## Interfaces for other packages

`reelvault.server.jobs_ig` (all take/return ids as strings; each opens its own short transaction):
- `start_pull(user_id, account_id, mode, *, run_after=None, priority=10) -> job_id` (WP4). mode `first|next|refresh`.
  Creates the `pulls` row + `ig_pull` job (executor extension, dedupe `ig_pull:<account_id>`); returns the running
  job's id when one is active. 400 when `next` and `cursor_done`, or the account is private/not found. The daily cap
  is WP4's: pass `run_after` for later days (an account in status `new` becomes `scheduled`).
- `request_profile(user_id, handle, purpose, account_id=None, source_id=None) -> job_id` (WP4 `competitor`, WP7
  `member_bio`; with `source_id` the source goes to `waiting`/`via=extension`).
- `request_videos(user_id, reel_ids) -> job_id | None` (WP3): batches of <= 30 into `ig_videos`, marks the reels
  `fetching`; skips reels in an active `ig_videos` job or already `have`/`deleted`.
- `schedule_need_extension(user_id) -> job_id | None`: `media_state=need_extension` reels -> one `ig_videos` job
  (priority tier first). Also run by every extension lease that may take videos.
- `request_discover(user_id, run_id, mode, *, queries=None, seeds=None) -> job_id` (WP4; mode `niche` = `search`;
  queries are cleaned to the validate.js charset, <= 3; seeds <= 2 `{"handle", "pk"?}`).
- `request_check(user_id, run_id, candidates) -> job_id` (<= 15 `{"cand_id", "handle", "pk"?}`). Chained
  automatically after `ig_discover` (top 15 by followers, unless WP4 defines `jobs_score.pick_check_candidates`).
- `request_page_read(user_id, source_id, kind, url) -> job_id` (WP7; kind `skool_profile|skool_group|linkedin_self`;
  5 per member per day).
- `ig_health() -> dict` (also served at `GET /api/admin/instagram-health`, see below).
- `upsert_reels(conn, user_id, acc, reels_in, *, seen_at, remaining=None, refresh=False) -> counters` (shared ingest).
- Constants: `PACING`, `EXT_KINDS`, `IG_KINDS`, `MSG` (the 5.7 member copy), `PULL_TARGET`, `PAGE_CAP`.

Hooks WP2 calls (all optional; WP2 has a fallback while they raise `NotImplementedError`):
- `jobs_score.ingest_candidate(user_id, cand_id, account: dict, items: list[dict])` (WP4) for each `ig_check`
  upload. `account` = `igparse.AccountIn.to_dict()`, items = `ReelIn.to_dict()` (<= 12). Fallback: profile numbers
  + `status=checked`.
- `jobs_score.pick_check_candidates(user_id, run_id) -> [{"cand_id","handle","pk"?}] | None`, `jobs_score.after_check(
  user_id, run_id)` (WP4, both optional, looked up with `getattr`).
- `importers.instagram.save_ig_bio(user_id, source_id, IgUser dict)` (WP7). Fallback writes plain text.
- `importers.skool.parse_next_data(next_data) -> str | {"text","meta"} | obj(.text,.meta)` and
  `importers.linkedin.clean_page(text)` (same return shapes) (WP7). Fallback: a small built-in Skool reader.
- `pipeline.advance(user_id, reel_id)` (WP5) after each uploaded video.
- After a pull ends (done, final failure, or last lease expiry with new reels): `score_account` is enqueued
  (dedupe `score_account:<account_id>`), nothing per reel.

`reelvault.igparse`: `normalize(IgMedia, account_pk) -> ReelIn | None`, `parse_user(IgUser|raw) -> AccountIn`,
`shortcode_to_pk`, `pk_to_shortcode`, `cdn_expiry(url)`, plus test twins `quote_bigints`, `parse_ig_json`,
`trim_media`, `trim_user`, `page_items(page, source)`. `IgParseError` for broken shapes (JSON-number ids).

`reelvault.server.api_ext`: `new_pairing_code(user_id, kind, *, conn=None) -> (code, expires_at)` (WP5 uses it
for `POST /api/ai/desktop/pair`), `extension_zip() -> bytes` (WP1's download route), `latest_version()`.

Fixtures (`tests/fixtures/ig/`, regenerate with `python tests/fixtures/ig/make_fixtures.py`): `clips_user_page1.json`
(50), `page2_last.json`, `feed_user.json` (with a photo and a carousel to drop), `media_info.json`,
`web_profile_info.json`, `web_profile_info_private.json`, `user_info.json`, `current_user.json`, `topsearch.json`,
`top_serp.json`, `chaining.json`, `profile_page.html`, `bigint_pk.json`, `collab.json`, `with_suggested.json`,
`pinned.json`, `hidden_counts.json`, `injection.json`, `fail_feedback_required.json`. `make_fixtures.account_timeline(n)`
gives a deterministic timeline (TARGET pk 25025320) for WP4 scoring tests.

## Deviations from DESIGN-SAAS.md (please fold in)

- New endpoints: `POST /api/ext/try-again` (the web UI's Try again after `signed_out`, 5.7; 400 for other pauses) and
  `GET /api/admin/instagram-health` (admin only; 5.9 data: `samples`, `fails`, `recoveries`, `pk_conflicts`,
  `fields_seen`). Both are in `api_ext.py` because section 4 names no route for them.
- `/api/ext/status` adds `device.ig_user_id`, `signed_in`, `cooldown_text`, `update_needed`, `update_text`,
  `budget.cdn_day_*` and `min_version`. `hello` adds `ig_username` and `cooldown_reason`. `ExtJob` =
  `{"id","kind","payload","lease_until","attempt"}`.
- Lease reason `"update"` (new): an extension older than `EXTENSION_MIN` gets no work (`poll_after_s: 300`); the
  badge shows "!" and the popup says "This extension is too old for ReelVault. Update it to keep reading."
- Fail code `budget` (new, extension side): the extension's own 5.6 limit stopped a job. The job waits
  `cooldown_s` (60 s - 24 h) without a device cooldown and without spending an attempt.
- `items.paging.request_max_id` (optional, new): the cursor the page was requested with, so the server can save
  the exact resume point when it cuts a page at the 100th new reel. Without it the server uses what it handed out.
- Pacing gets `retry_ms` (30 s / 120 s for 5xx retries) and `page_read_day_max` (5). `RV_TEST_PACING=1` makes the
  server send 50-100 ms gaps; only an extension whose `config.js` says `BUILD = "test"` takes them (made only by the
  e2e test; `build_extension.py` refuses that build). Store/ZIP builds always keep the stricter numbers.
- Sign-in re-check for a different account: when the lease says `confirm_account` and work is waiting, the
  extension re-checks the account at most every 30 min (same rule as the `signed_out` re-check). Without it, a
  member who switched back in Chrome would wait forever.
- When the stop of 5.7 can't be reported (network error or 5xx), the extension keeps its own cooldown
  (`local_only`) and a `hello` without a cooldown does not clear it before it ends.
- At lease time a `next` pull's cursor (`max_id`, `cursor_source`, `oldest_pk`) is always rewritten from the
  account row: a stored payload can't steer a pull to another cursor.
- After the LAST attempt's lease expires (`@jobs.on_lease_expired`), WP2 tidies up: the pull is closed (`error`,
  score queued when it stored reels), `fetching` reels go back to `need_extension`, discovery runs `failed`, sources
  still `waiting` become `needs_paste`.
- Server-side Instagram counters: the server counts the calls it can infer per device (`usage_counters` scope =
  device id, keys `ig_calls` hour/day and `cdn` day) and leases with the stricter of these and the extension's
  report. The discovery cap (60/day) and `mediaInfo` cap (30/job) are enforced by the extension only.
- The pair-claim per-client limit uses `ratelimit.client_key` (one IPv6 /64 = one client).
- Debug samples are stored in `app_settings` under `internal.debug_sample.<device>.<endpoint>` (WP1's suggestion).
- Reels pulled from `ig_check` are never stored; `ig_check` hands its 12 items to WP4 only.

## Not built or not tested here, and why

- Chrome's native permission prompt (`chrome.permissions.request`) can't be clicked in headless Chromium. The e2e
  suite tests the grant tab, its copy and "Not now", and tests the upload path with a copy of the extension whose
  manifest already holds the two optional hosts (the state after "Allow"). The Allow click itself is untested.
- The design's "10 minutes idle" check runs as 20 loop ticks (what 10 minutes of alarms do) plus one real 30 s alarm,
  to keep the suite near 75 s.
- The service-worker kill uses CDP `Target.closeTarget` and wakes the worker with a popup message (the real 30 s
  alarm also resumes it; seen while building the test, about 27 s).
- Real Instagram: nothing here talks to instagram.com. Field names come from the research fixtures; the first live
  run (14.2) and the debug samples (5.9) settle them. `readingPage` `/robots.txt` is unconfirmed on the real site
  (fallback `/accounts/edit/` is noted in `endpoints.js`).
- Chrome Web Store build and listing: owner task (14.2 step 1). `deploy/build_extension.py` makes the ZIP.
- The web UI screens for Settings > Chrome extension (pair button, confirm box, Try again) are WP7's; the API exists.
- Run the suites from the project root (`cd reelvault && python -m pytest -q tests`): 292 passed, 7 skipped (WP1
  deploy kit, WP3 models). From the repo root (`/home/user/linkedin`) the desktop tests fail with "cannot import
  name '__version__'": the `reelvault/` project folder on `sys.path` shadows the package. That happens with or without
  WP2 code.
