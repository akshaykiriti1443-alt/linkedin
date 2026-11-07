# Playbook: running ReelVault for the owner (no command line, no API key)

The owner does not use a terminal. Claude runs everything inside a Claude Code session,
which runs on the owner's Claude subscription. Instagram blocks cloud machines that are not
logged in, so reels are watched through the owner's **vidIQ connector** instead of downloaded.

## When the owner pastes reel links

1. **Restore the vault first** if `vault/` is empty. Ask for the dashboard link if you don't have it,
   read it with the Artifact tool (`action: "read"`), save the HTML, then run
   `reelvault restore <file>` from this folder.
2. For each link, call `vidiq_watch_shortform_content` (10 credits each; check `vidiq_balance`
   first if the batch is large) with this prompt:
   > Give a scene-by-scene walkthrough with timestamps. For each scene include: the exact spoken
   > words (full verbatim transcript), the exact on-screen text, what is shown visually, and the
   > editing (cuts, zooms, b-roll, captions, sound effects). Clearly mark where the hook ends,
   > every beat of the body, and the call to action.

   Poll `vidiq_job_poll` until done. Save the returned cover image as `work/<code>/cover.jpg`.
3. Write `work/<code>/record.json` in the shape described in `reelvault/manual.py`. The
   `analysis` object must match `reelvault/schema.py` (`ReelAnalysis`). Use `profile.md` for the
   hook variations, script ideas and ready script. Quote words exactly; do not invent.
4. `reelvault add work/<code>/record.json --cover work/<code>/cover.jpg`
5. `reelvault site && reelvault kb`
6. Republish `vault/artifact.html` to the **same** Artifact URL (pass `url`), so the owner keeps one link.

## Batches

- The owner can send Instagram's "Download your information" JSON instead of links:
  `python -c "from reelvault.fetch import urls_from_export; ..."` lists every saved link.
- Work in batches of 10-20 and republish after each batch so progress is never lost.

## Never

- Never commit `vault/`, `work/`, `profile.md` or `reelvault.toml`: this repo is public.
- Never ask the owner to paste a password or API key into chat.
