# Final guides: notes (2026-11-10)

Written: `members/OWNER-GUIDE.md`, `members/MEMBER-GUIDE.md`, and a short "ReelVault Members (server)" section in
`README.md`. Every button and screen name in the guides was checked against the web UI code
(`reelvault/web/static/views/*.js`), the extension popup and `web/copy/connector.json`. DESIGN-SAAS.md is not edited.

## Mismatches found while writing (not fixed here: other packages own these files)
- **Pair button name.** The extension popup hint (`extension-members/popup.html`) and DESIGN-SAAS.md 5.2 say
  "click **Connect the extension**", but the web UI button is **Get my pair link** (`views/extension.js`). The member
  guide uses "Get my pair link". Fix: change the popup hint to "Get my pair link" (WP2), or rename the button (WP7).
- **Web Store link setting is undocumented.** `api_ext.py` reads `EXTENSION_STORE_URL` (shows "Add to Chrome from
  the Web Store" and the update link), but it is not in `deploy/.env.example` or `deploy/DEPLOY.md`. The owner guide
  tells the owner to add it to `deploy/.env` and run `./deploy/setup.sh update`. Fix: add a commented placeholder to
  `.env.example` and one line to DEPLOY.md, "The Chrome extension" (WP1).
- **"Reject" vs "Say no".** DEPLOY.md, "After the first sign-in" step 3, says **Approve** or **Reject**; the Admin >
  Members button is **Say no**. The owner guide uses "Say no".
- **No "Process everything" button.** DESIGN-SAAS.md 1.3/6.0 offer "Process everything" to API-key members, and the
  server honours `users.settings.process_all` (`jobs_score._process_all`), but no screen sets it. The owner guide
  says so plainly instead of pointing members at a missing switch.
- **Claude plan wording.** Settings > AI says "Your own Claude (Pro, Max, Team)" while the connector notes say "Free
  Claude plans allow one custom connector". The member guide says any plan works and free plans allow one custom
  connector (owner live test 14.2 step 6c should confirm).

## Journey harness: stale fake AI after fix round 2
`tests/journey/journey.mjs` on the real tree stops at step "output: competitor_report" (22 passed, 2 failed). This is
not a product bug. Fix round 2 made `outputs._check_report` reject a report whose `competitors` uses none of the
member's tracked handles. The fake AI in `tests/server/_wp5_support.py::report()` always answers with the handle
`alexhormozi`, so in the journey (handles `dental.growth.co`, `smile.marketing`, `clinic.ads.lab`) both tries fail
with `invalid_output`. The pytest suite still passes because its seeded member tracks `alexhormozi`.
Fix (WP5 owns the file, one line): take the handle from the brief, e.g.
`"handle": (re.findall(r'"handle": "([^"]+)"', text) or ["alexhormozi"])[0],`. A copy of the tree with only this
line changed was run end to end: 36 of 36 steps passed, including the four outputs and their downloads, Remake this, the Claude connector (MCP client) and the desktop app with the real Claude Code CLI.

## Test runs for the final report
- `tests/e2e/extension_e2e.js` (the desktop app's own extension e2e, run by hand) was not run: it needs the desktop
  app on port 8765, which this round may not use.
- `python -m pytest -q tests` (project root; desktop, server, MCP, extension unit + e2e, web UI): 636 passed,
  11 skipped. 10 of the skips need the media models; `tests/server/test_signals.py` with `RV_MODELS_DIR` set to the
  fetched models: 61 passed, 0 skipped. The other skip is `test_deploy_kit.py:113` (covered by WP2's own tests).
