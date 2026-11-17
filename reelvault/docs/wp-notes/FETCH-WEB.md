# FETCH-WEB notes: the member and admin screens of the own fetch engine

Contract: `DESIGN-FETCH.md` section 5 (and 6 for the routes). This package owns `reelvault/web/*`,
`reelvault/members/*.md` and `tests/web/*`. The server routes already exist (FETCH-SERVER), so the screens run
against the real server as well as against mocks.

## What is here

| file | what |
|---|---|
| `web/static/views/fetch.js` (new) | `readCard()` = Settings card "How ReelVault reads Instagram"; `loadReads()` + `readLine()` = the grey "how this pull was read" line; `testConnection()` = page `#test-connection` |
| `web/static/views/settings.js` | the card sits on **Settings > Chrome extension**, under the extension status |
| `web/static/views/competitors.js` | one grey line per competitor (newest pull) on the list; per pull in the Pulls table and under a running pull on the detail page |
| `web/static/views/admin.js` | card "Fetch health" on **Admin > Health**: per-way table with on/off switch, layer totals, members' Chrome, last 20 blocks, settings + doc_id editor |
| `web/static/app.js` | route `#test-connection` (nav: Settings); `ago()` says "1 day ago", not "1 days ago" |
| `web/static/views/extension.js`, `onboarding.js` | copy: the extension reads without the login first |
| `web/static/app.css` | radio rows, layer lamps, verdict box, phone layout of the test table (rows become cards under 620 px), switches, doc_id grid |
| `members/MEMBER-GUIDE.md` | "How ReelVault reads Instagram" and "Test my connection" in step 3; the "What you need" line |
| `members/OWNER-GUIDE.md` | section 6: Safest first, honest limits, the owner's one test on his PC, Fetch health; section 7 step 3 runs the test first |
| `tests/web/fetch.test.mjs` (new) | 10 Playwright tests with every API answer mocked (contract shapes), static files from a tiny server on a free port |
| `tests/web/ui.test.mjs` | `#test-connection` in the screen sweep + one test on the real seeded server |
| `tests/web/test_web_ui.py` | `test_fetch_web_ui_mocked` runs `fetch.test.mjs` from pytest (about 25 s) |

## Choices where the contract left room (smallest sensible option)

1. **Where the Settings card lives.** Settings has tabs and no single page. The card sits on the
   **Chrome extension** tab, because reading is what the extension does. `#test-connection` links back there.
2. **"competitors and working views".** `working.js` (What works) shows no pull status, so it gets no line. The
   lines are on the competitors list, the competitor page (per pull) and under a running pull.
3. **Older server.** When `/api/fetch/*` answers 404 the card and the lines stay hidden and the pages work as before.
   Tested.
4. **Admin card placement.** "Fetch health" is a card on the existing **Admin > Health** tab, not a new tab.
   Only logged-out ways have a switch (the server refuses the others); logged-in rows show "—".
5. **doc_id editor.** One box per strategy that uses doc_ids (`lo_posts_gql`, `lo_reels_gql`, `lo_post_gql`,
   `lo_media_gql`), comma separated. The page checks 10-24 digits before it sends. An empty box sends `[]`, which the
   server reads as "use the built-in id".
6. **Read mode Save.** The choice applies on Save (not on click), as 5.1 shows a Save button. After Save the layer
   lines reload from `/api/fetch/status`.
7. **Probe polling.** Every 3 s while the page is open and the test is `waiting` or `running`; it stops on
   `done`/`failed` or when the member leaves the page (`every()` is page-scoped).
8. **Time column.** Under 100 ms shows "under 0.1 s"; skipped rows show "—".

## Evidence

- `node --test tests/web/fetch.test.mjs`: 10 of 10 pass. 1280 and 390 px, light and dark; no console errors, no
  sideways scroll, no stray "null"/"undefined"; Save sends `{"read_mode": ...}`; waiting -> running (4 of 11, bar
  36%) -> done with the green verdict; amber and red verdicts; failed; 409 and 429 messages; switch-off posts
  `{"strategy", "enabled": false}`; a bad doc_id is refused in the page.
- `tests/web/ui.test.mjs` (real seeded server, `RV_WEB_PORT_BASE=9650`): 8 of 8 pass, including the new real-server
  test (read mode saves and shows "You turned this off in your settings.", the test page opens, a read line shows on
  the competitors list, the admin sees Fetch health) and `#test-connection` in the 4-way screen sweep.
- Full suite `python -m pytest -q tests` (with `RV_WEB_PORT_BASE=9650 RV_E2E_PORT_BASE=9655`): green, 0 failed
  (about 739 passed, 2 skipped).
- Screenshots: `RV_WEB_SHOTS=<dir> node --test tests/web/fetch.test.mjs` writes 20 PNGs.

## Unverified live

The screens only show what the server says. Nothing ran against real Instagram. The owner's "Test my connection"
on his PC is the first live check (DESIGN-FETCH.md section 10).
