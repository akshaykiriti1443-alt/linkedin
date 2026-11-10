# ReelVault

ReelVault turns your saved Instagram reels into a library you can learn from.

For every saved video it:

1. Downloads the video into a folder.
2. Writes a transcript (local Whisper, free).
3. Reads the frames to capture on-screen text and what the visuals say.
4. Asks Claude for a full breakdown:
   - **Summary** and key takeaways
   - **Plan of action** (steps you can take today)
   - **Skeleton**: the **hook**, the **meat** (beat by beat, with timestamps) and the **CTA**
   - Visual style, editing tricks and retention devices
   - A fill-in-the-blank **template**, 5 hook variations, 3 script ideas and one **ready-to-shoot script** for *your* niche
5. Files it under a category folder and adds it to a search index.
6. Builds a **dashboard** (`vault/index.html`) and a Markdown **knowledge base** (hooks swipe file, CTA library, structures, templates, action plans).

Then `reelvault remix` writes new scripts from any mix of saved skeletons.

## Windows app (no command line, no API key)

1. Open the Start menu, type **PowerShell**, and open it.
2. Paste this one line and press Enter:

   ```powershell
   irm https://raw.githubusercontent.com/akshaykiriti1443-alt/linkedin/claude/kind-dijkstra-0o03in/reelvault/install.ps1 | iex
   ```

3. Wait for "All done" (5-10 minutes the first time). ReelVault opens in your browser,
   and a **ReelVault** icon appears on your desktop.
4. In the app: **Sign in to Claude** (uses your Claude Pro/Max subscription), then
   **Connect Instagram**. The first time, the app walks you through adding the small
   ReelVault Connector to Chrome (about a minute). After that, Connect opens a new
   Instagram tab in your normal Chrome and connects in seconds.
   Fill in **About you**, then click **Get new saved reels**.
5. Turn on **Run every day** and it keeps your library up to date by itself.

The app runs on your computer. Breakdowns use the Claude Code app signed in to your
subscription (`brain = "claude-code"`), so there is no API key and no per-reel bill.
Everything is stored in `C:\Users\<you>\ReelVault`.

## ReelVault Members (server)

ReelVault Members is the online version for a Skool community. Members sign up with an invite, add the Instagram
accounts that win in their niche, and their own Chrome reads those reels through the ReelVault Members extension
(their Instagram login never leaves the browser). ReelVault finds the outliers, shows what works (formats, hooks,
sound), and writes scripts, Meta ads, 30-day plans and competitor reports. Each member brings their own AI: their
Claude plan through the Claude connector, an API key, or this desktop app. No payments.

- **Owner guide** (host it, invite and approve members, limits, risks, what to test first):
  [members/OWNER-GUIDE.md](members/OWNER-GUIDE.md)
- **Member guide** (sign up, extension, AI, competitors, outliers, playbooks, outputs, editor brief):
  [members/MEMBER-GUIDE.md](members/MEMBER-GUIDE.md)
- **Put it online:** [deploy/DEPLOY.md](deploy/DEPLOY.md)
- **Technical blueprint:** [DESIGN-SAAS.md](DESIGN-SAAS.md)

The desktop app below keeps working on its own; Members is a separate product that shares some code.

## What you get on disk

```
vault/
  index.html                      dashboard: filter by category, hook type, format, CTA, structure
  knowledge-base/                 hooks.md, ctas.md, structures.md, templates.md, action-plans.md, categories/
  scripts/                        scripts written by `reelvault remix`
  library/<Category>/<date>_<code>_<title>/
      video.mp4
      frames/                     stills Claude looked at
      transcript.txt              plain transcript
      transcript_timed.txt        transcript with timestamps
      analysis.json               the full breakdown
      note.md                     the readable card (opens great in Obsidian)
  index.sqlite                    search index
```

## Command line setup (advanced)

You need Python 3.11+, [ffmpeg](https://ffmpeg.org/download.html) and a [Claude API key](https://console.anthropic.com).

```bash
cd reelvault
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env          # paste your ANTHROPIC_API_KEY
reelvault init                # makes reelvault.toml and profile.md
```

1. Put your Instagram handle in `reelvault.toml` (`instagram_username`).
2. Fill in `profile.md`: your niche, audience, offer and voice. Claude uses it to write scripts that fit you.

## Get your saved reels in

Instagram's official API has no way to read saved posts. Pick one of these three routes.

**A. Log in once (fastest, fully automatic)**

```bash
reelvault login               # asks for password + 2FA in your terminal; stores a session, not your password
reelvault run --limit 20      # fetch 20 new saves, analyze them, rebuild dashboard + KB
```

Run `reelvault run` again any time. It only picks up new saves.

**B. Use Instagram's data export (your login stays in the browser)**

1. Instagram app → Settings → Accounts Center → *Your information and permissions* → *Download your information*. Choose JSON.
2. Unzip it, then:

```bash
reelvault run --from-export ~/Downloads/instagram-export --cookies-from-browser chrome
```

**C. You already have the videos**

```bash
reelvault run --folder ~/Videos/saved-reels     # a .txt next to a video is read as its caption
```

## Daily use

```bash
reelvault status                                   # how many done / waiting / failed
reelvault process --retry-failed                   # retry anything that failed
reelvault search "pricing objection"               # full-text search across transcripts, hooks, captions
reelvault remix --topic "why my clients churn" --category "Marketing & Sales" --count 3
reelvault remix --topic "my morning routine" --source C8xYz12AbCd DAbC9xYzQ1
reelvault site && reelvault kb                     # rebuild dashboard + knowledge base
```

Open `vault/index.html` in your browser. Click a reel to see the skeleton, plan, visuals, scripts and timed transcript. Click a timestamp to jump the video there.

Open the `vault` folder in [Obsidian](https://obsidian.md) to use it as a linked knowledge base.

## Settings (`reelvault.toml`)

| Setting | Default | What it does |
|---|---|---|
| `model` | `claude-opus-5-5` | Claude model for breakdowns and scripts |
| `effort` | `medium` | `low` is cheaper and faster; `high` digs deeper |
| `whisper_model` | `small` | `tiny`/`base` are faster; `medium`/`large-v3` are more accurate (good for Hindi, Spanish, etc.) |
| `max_frames` | `12` | Stills per reel sent to Claude. More frames = better visual read, higher cost |
| `categories` | 13 defaults | Edit the list to match how you think |
| `include_images` | `false` | Also keep photo posts |

## Cost and safety notes

- Transcription runs on your machine. Only frames, transcript and caption go to Claude.
- A typical 30–60s reel at the default settings is one Claude call with ~12 small images.
- Requests opt into Claude's server-side fallback: if a safety filter declines a reel, the API retries it on a fallback model.
- Automated access to Instagram can break their terms and can trigger rate limits or a checkpoint on your account. Keep `--limit` small, use your own account only, and use route B if you want zero risk to your login. Respect creators' rights: this tool is for private study, not reposting.

## Test

```bash
pip install pytest
python -m pytest -q tests/       # fake Claude server; no API key or Instagram login needed
```
