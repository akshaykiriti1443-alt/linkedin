# Project brief for Claude Code

Read this first. It replaces the chat history of the cloud session that built this repo.

## The owner
- Non-technical agency owner (paid ads), brand Scale It Ads (scaleitads.com), runs a Skool community
  for agency owners. Uses Windows + Chrome. Does NOT use the command line: you run commands for him,
  or give him ONE line to paste into PowerShell and say exactly where to paste it.
- Writing style he wants: present tense, active voice, short sentences (max one comma), simple words,
  no filler, positive phrasing.
- Never put secrets in this repo: it is PUBLIC on GitHub.

## What is in this repo
- `main.py`: an old Instagram comment auto-reply bot (FastAPI). Not active work.
- `reelvault/`: the main product. Two parts share one Python package (`reelvault/reelvault/`):

### 1. ReelVault desktop app (version 0.4.x, in daily use by the owner)
- Windows app: local web panel at http://127.0.0.1:8765, launched from a desktop icon.
- Pulls his SAVED Instagram reels (Chrome extension in `reelvault/reelvault/extension/` hands the
  instagram.com login to the app), transcribes (faster-whisper, CPU fallback), extracts frames, and
  Claude (Claude Code CLI headless, his subscription; no API key) writes a breakdown.
- Four purposes per reel: Learn it / Borrow the hook / Use the format / Remake it better; Hook Bank,
  Format Bank, Studio, self-update from GitHub.
- Blueprint: `reelvault/DESIGN.md`. Installer: `reelvault/install.ps1`. Main files: `app.py`, `ui/`,
  `brain.py`, `analyze.py`, `igweb.py`, `library.py`, `update.py`.
- Lessons learned on his PC: Windows 8191-char command limit (system prompt goes in a file, compact
  schema), old Claude Code versions (auto `claude update`), NVIDIA without CUDA libs (CPU fallback),
  an old copy still running after updates (installer and app replace it).

### 2. ReelVault Members (SaaS for his Skool members only, no payments)
- Spec: `reelvault/DESIGN-SAAS.md` (decisions log at the end). Notes per work package:
  `reelvault/docs/wp-notes/`. Guides: `reelvault/members/OWNER-GUIDE.md`, `MEMBER-GUIDE.md`,
  `reelvault/deploy/DEPLOY.md`.
- Server: `reelvault/reelvault/server/` (FastAPI + SQLite, job worker). Member web app:
  `reelvault/reelvault/web/`. Chrome extension (reads competitor reels with the member's own login,
  100 per pull, paced): `reelvault/extension-members/`. Signals (cuts, sound map, visuals):
  `reelvault/reelvault/signals/`. AI providers + Claude connector (MCP + OAuth): `reelvault/reelvault/ai/`,
  `server/mcp_server.py`, `server/oauth.py`. Outputs: scripts + editor brief with sound cue sheet,
  Meta ads, 30-day plan, competitor report.
- Runs on his PC for now: `reelvault/reelvault/members_local.py` at http://localhost:8800, installed by
  `reelvault/install-members.ps1` into `%USERPROFILE%\ReelVault-Members`. Owner login is
  `owner@reelvault.local` with the password in `ReelVault-Members\login.txt`. AI work runs through his
  local Claude Code (paired automatically).
- Hosting plan (later): inside his OneX app at app.scaleitads.com (Next.js on Vercel; its code is NOT in
  this repo). Vercel cannot run the engine, so the engine needs a small always-on server (Docker kit in
  `reelvault/deploy/`), shown at app.scaleitads.com/reelvault with single sign-on from OneX.

## Status and open items
- Done: everything above, 645+ tests (`cd reelvault && python -m pytest -q tests`), GitHub Actions:
  `ReelVault on Windows` (desktop + members localhost installer) and `ReelVault Members server`
  (tests, Docker build + boot + backup/restore).
- Not yet tested live: Instagram with a real login (members extension), real hosting, Chrome Web Store
  listing, detectors on real reels (needs 20-30 labelled reels from the owner).
- Known gap: optional Postgres mode has a daily-jobs scheduling failure in CI (non-blocking; SQLite is
  the default).
- OneX one-click sign-in is built on the ReelVault side: `GET /sso/onex?t=<ticket>` (in
  `server/api_auth.py`, tests in `tests/server/test_sso_onex.py`), secret `ONEX_SSO_SECRET`
  (local: `ReelVault-Members\onex-sso-secret.txt`). The OneX side is described in
  `reelvault/INTEGRATE-ONEX.md` and is done inside the OneX project folder (named like "0to1x" on his PC).
- Next likely asks: let the owner log in with his own email; host ReelVault at a subdomain and wire OneX to it.

## How to work here
- Git branch with all of this: `claude/kind-dijkstra-0o03in`.
- Keep the desktop app's Windows CI green; test UI changes with screenshots.
- Ask before big product decisions; act on small ones and say what you chose.
