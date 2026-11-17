# FETCH-EXT notes: the extension side of the own fetch engine (version 2.1.0)

Contract: `DESIGN-FETCH.md` sections 2, 3 and 9. This package owns `extension-members/*` and `tests/ext/*`.
Nothing here ran against real Instagram. Every logged-out answer comes from `tests/ext/fake_ig_server.py`, which
builds them with `tests/fixtures/ig/logged_out/make_lo_fixtures.py`.

## What is here

| file | what |
|---|---|
| `reader.js` | `igAnonFetch`: one logged-out read from the reading tab with `credentials: "omit"`, `cache: "no-store"`. It drops any cookie, CSRF or claim header it is handed and never reads `document.cookie` |
| `strategies.js` (new) | the 8 strategies of 3.2: request specs (paths only from validated values), pure parsers, `classifyAnon` (table 2.3), `loInterpret`, probe field words, doc ids (server first, built-in fallback) |
| `ladder.js` (new) | the logged-out stage of a pull (2.1 steps A-E) with injected calls, so node runs every path |
| `sw.js` | `loCall` (logged-out budget + gap, then the read, then table 2.3 at once), local `lo` state, `fetch_log`, stage 1 -> `stage_done` -> stage 2 top-up, `lo_blocked`, the probe job, logged-out `ig_profile` and `ig_videos` (P2), lease body `lo` + kind `ig_probe` |
| `pacing.js` | `take("lo")` / `take("lo_page")`: own gaps, own hour/day budget, also counted in the 120/400; layer gap before the first logged-in call; 3 probes per day |
| `validate.js`, `trim.js`, `endpoints.js` | `read` + `plan` + `ig_probe` checks, `trimGql`, strategy ids |
| `popup.*`, `manifest.json` | "without login: N of 200", "Reading without your login." / "Reading with your Instagram login.", the logged-out wait line; version 2.1.0, new description, no new permission |
| `tests/ext/fake_ig_server.py` | every 3.2 request, the cookie detector (HTTP 400 `cookie_leak` for any logged-out request with a Cookie or CSRF header) and the scenarios of 3.5 plus a few more |
| `tests/ext/fetch_unit.test.mjs` | 30 node tests: every parser on every fixture, `classifyAnon`, pacing, every ladder path |
| `tests/ext/fetch_e2e.mjs` | 22 Chromium tests against a small contract server (4.1/4.2) in the test file |
| `tests/ext/ext_e2e.mjs` | the 39 logged-in tests, plus 3 new "real server" cases: auto pull logged out, partial + top-up, Test my connection |
| `tests/ext/test_ext_e2e.py` | pytest runs all four node files |

## Notes where the build adds to the contract (smallest sensible option)

1. **The gap starts when the reading tab is ready.** The first call of a job may have to open the reading tab.
   `loCall` now opens it before it takes the random gap, so the gap sits right before the request.
2. **`ig_videos` has no handle, so no page of its own for the `lsd` token.** The worker keeps the newest `lsd` in
   `chrome.storage.session` (memory only, gone when Chrome closes, never uploaded) and uses it for up to 6 hours.
   Without one, or after the first block, the login reads the video URLs as before (`mediaInfo`, with the sign-in
   check right before the first one). In `logged_out_only` a reel without a URL is reported as `login_needed`
   (not a "for good" reason, so the server tries it again later). When nothing works at all the job fails with
   `lo_blocked` / `login_wall`. A `rate` or `challenge` answer fails it with `lo_blocked` as in a pull.
3. **A working second doc id is kept.** After `changed`, the retry with the next id that works becomes this
   device's first choice (`lo.doc_ok`). The ladder then asks without a bump. (The first attempt bumped twice and
   went back to the dead id.)
4. **Popup texts.** The reading line shows as soon as a job starts (from its plan), not only after the first call.
   The logged-out wait line says "Instagram asks ReelVault to wait. ..." after `rate` or `challenge`, and
   "Instagram asks for a login right now. ..." after a login wall.
5. **The 4th probe of a day** posts every row as `skipped` / `budget`, the logged-in rows too, and makes no call.
6. **The reading tab itself.** Logged-out reads run in the existing reading tab, as 3.1 says. Opening that tab
   loads `https://www.instagram.com/robots.txt` as a normal page, and Chrome sends the member's cookies with that
   one page load (as in 2.0.0). No logged-out DATA request ever carries a cookie: the detector proves it in every
   e2e run. If the owner wants zero cookie traffic in `logged_out_only` mode, that needs a separate tab type and a
   contract change.

## Evidence (this machine, fake Instagram only)

- `node --test tests/ext/unit.test.mjs tests/ext/fetch_unit.test.mjs`: 40 pass.
- `tests/ext/fetch_e2e.mjs`: 22 pass. Logged-out success (100 reels, zero cookies, zero logged-in calls), partial +
  logged-in top-up to 100, login wall -> fallback, rate limit -> `lo_blocked` and no call during the wait,
  `logged_out_only` and `logged_in_only`, auto `next`, refresh, rotated doc id, logged-out `ig_profile` and
  `ig_videos`, the probe (11 rows), a blocked probe, a signed-out probe. The cookie detector never fired.
- `tests/ext/ext_e2e.mjs`: 42 pass with the real app, including the 3 real-server fetch cases.
- The 2.0.0 extension (git HEAD) against the new server: 38 of 39 logged-in tests pass. The one difference is the
  popup budget line that 2.1.0 extends.
- Seen once in five runs of `ext_e2e.mjs`: "302 to /challenge/" and "another Instagram account" failed, then passed
  in the next four runs. The account test now allows 2 minutes, since the server may hold a job for a minute.

## Unverified live
All of DESIGN-FETCH.md section 10. In particular: whether a cookie-less `fetch` (not a page load) of `/<handle>/`
gets the embedded data, whether GraphQL answers with only the `lsd` token, and whether logged-out detail calls
carry view counts. The owner's "Test my connection" run on his PC answers these.
