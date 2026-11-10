# Fix round 2: notes and deviations from DESIGN-SAAS.md (2026-11-10)

DESIGN-SAAS.md is not edited in this round. Where the code now differs from it, the change is listed here so the
design can be brought in line later.

## Security (11.1, 11.6, 7.3.2)
- **CIMD fetch guard** (`oauth.py`): CIMD fetches run in their own pool of 4 threads, never in the shared request
  threadpool. A request waits at most 5 s (`CIMD_WAIT`); httpx per-operation timeout 4 s (was 10 s). Failed fetches are
  cached for 3 minutes. Cache-miss fetches are limited to 30 per client (IP or IPv6 /64; x10 for Anthropic's range)
  and 300 server-wide per hour, counted before any session lookup. When all slots are busy a new request gets the
  page "Too many apps are connecting right now" (HTTP 503) at once. 7.3.2 step 1 says 10 s; it is now 4-5 s.
- **Password reset/change** (`api_auth.py`, `security.sign_out_apps`): a reset revokes every Claude connector token
  and every paired device (extension and desktop) of the member, and says "We signed out Claude and your devices.
  Connect them again." A password change does the same unless the member unticks "Also sign out Claude, the Chrome
  extension and the desktop app" (on by default). Jobs are kept. Audit rows carry `signed_out_apps`.
- **Allowlist** (11.1, D17 deviation): an allowlisted email no longer gets in at once. With email set up, the signup
  gets a one-time link (`#verify/<token>`, 24 h, stored in `reset_links` with `created_by = "verify-email"`; a reset
  never accepts it) and becomes active when the link is opened (`POST /api/auth/verify-email`). Without email it stays
  pending and Admin > Members shows an "On the allowlist" badge; "Say no" deletes the signup so the real member can
  sign up. Single-use invites and `signup_mode = active` are unchanged. The Zapier hook still approves a pending
  signup with that email (left as designed).
- **Login lockout** (11.1): the strict 5 wrong passwords per 15 min is now per (email, client); 20 per client for any
  email stays; a per-email limit of 50 per 15 min applies only from addresses the member never signed in from (audit
  log, 90 days). Nobody can lock a member or the admin out from another address.
- **DCR** (11.6): a plain 30 per client per hour (x10 Anthropic range), whatever the name or redirect, before the
  composite key; DCR clients never used at `/oauth/token` are deleted after 24 h (was 30 days).
- **Refresh grace** (7.3.2): reusing a rotated refresh token within 60 s, while the token it was rotated into is still
  unused, returns a fresh pair in the same family (a retry after a lost answer). Later reuse, or reuse after the
  successor was used, still revokes the family and writes `oauth_reuse`.
- **Consent page** (7.3.2 step 4): now also says "It can also add competitors and ask your Chrome extension to read
  their reels, within your daily limits."

## Instagram safety (5.3, 5.7)
- A 5.7 stop during the sign-in check outside a job (pairing, the confirm-account re-check) is stored as a local 24 h
  cooldown (15 min for http_error) and reported in `POST /api/ext/v2/hello` as `"stop": {"code", "http_status"?,
  "message"?}` (new optional field, 4.x/5.x contract). The server sets the device cooldown as `/fail` does.
  `ig.error` is stored with the failed check; a non-signed_out error never marks the device signed out. The re-check
  never runs during a cooldown. The popup shows the 5.7 text for the real reason.
- `http_error` no longer refunds the job attempt. `jobs.result.http_error_rounds` counts rounds in a row (reset by an
  accepted page); after 3 the job fails for good ("Instagram kept giving errors for this. ReelVault stopped trying.
  Try again later."), the pull keeps its cursor.
- ReelVault 5xx on an upload: the extension keeps the job but waits 1, 5, then 15 minutes (`job.resume_after`,
  `job.server_fails`) and drops it after the 4th failure (the server's lease expiry takes over).
- Every Instagram API call sends a same-origin Referer: `https://www.instagram.com/<handle>/reels/` for pulls,
  `https://www.instagram.com/` otherwise (5.3/5.4 only covered CDN downloads). e2e asserts it.
- `classify()` applies the 401/403 and "HTML instead of JSON" rules to `/web/` endpoints too (topsearch).
- The extension version stays 2.0.0 (nothing has shipped yet).

## Analysis
- `insights.sub_formats`: a group that does not become a format row (too small or no outlier) goes back into the pool
  for the parent level (6.9 said only "< 3 units merge").
- `insights.format_cards`: formats with lift < 1 sort after every winner; What works shows them only in a closed
  "Formats to avoid" list without the Write-scripts button (same rule as Home, D85).
- `insights.compute`: the recent window uses the same hit rule as the full window.
- `insights.music_rows(outliers_only=True)` for the snapshot; outputs also filter `outliers >= 1`; generic
  "Original audio" titles are skipped for reused audio too.
- `signals/sound.py`: same-label ML runs less than 1 s apart are one event (a ding's tail); a clearly stronger
  (>= 2x) DSP onset earlier in the first window that heard the sound is its time. Test with dings under a -20 dB bed.
- Outputs: every script source carries what was measured (SoundMap cue list, music and Instagram audio, EditStats);
  "Use this format" sources are the outliers among the format's examples (normal reels only when there is none) and
  a non-outlier source is labelled; the brief says "set format to 'VFxx' and sub_format to 'fmt_...'" and
  `_check_script` rejects another format; output briefs use their own topics line; the competitor report keeps
  only tracked handles and asks the AI again when none match.

## Connector and member copy
- `connector_steps` follow Claude's current flow (+ Add, two Continue clicks, Authentication "Sign in now",
  per-chat Connectors switch, "Always allow"). 6 steps, not 5; the 14.2 live screenshot check still applies.
- `start_competitor_pull` with no paired extension queues nothing (mode first only adds the account) and says so;
  paired but offline: "Queued. It starts when Chrome is open with the ReelVault extension on." `list_competitors`
  returns `extension` and `extension_note`.
- Admin > Copy has the desktop install steps and command. Settings > AI shows provider names and plain status pills.
  Queued jobs say "Starts in a minute" / "Starts at 14:05"; housekeeping jobs have no Stop button.
- `/pair` is styled with the hashed `<style>` of the OAuth pages (the site CSP blocked inline styles).

## Deploy and backups (11.7, 12.5)
- `storage.restore_db` refuses while a process in the container has the database open (/proc) or the web answers
  on PORT. PaaS restore: `RV_RESTORE_FROM=<file in /data/backups>` is restored by `reelvault-start` before web and
  worker start (once per file version, marker `.restored-<name>-<mtime>`); `paas-with-volume.md` says how.
- Restore carries today's `daily:*` counters into the restored file, so today's backup is not taken again from older
  data.
- The `storage`, `security` and `bench` CLIs re-run themselves as uid 10001 (setpriv) when started as root on a data
  folder owned by 10001 (`RV_ALLOW_ROOT_CLI=1` turns this off). `entrypoint.sh` gives back any file in /data not
  owned by 10001 at every root start.
- The CLI backup (`backup.sh`, `setup.sh update`) rebuilds this week's member-files tar (temp name, then rename); no
  weekly tar is made while `users/` holds no file.
- Admin > Health: last backup with its size, red after 36 h; a failed backup job is shown in red.
- `setup.sh` warns below 60 GB free (was 80 GB, the recommended disk size). DEPLOY.md: the bench step goes back one
  folder before `setup.sh update`; step 4 names the real panel and buttons.

## Not done / open
- The MCP tests start their app on a free port in 9220-9239 (their own fixture), outside this round's port range.
