# Video Bot — AI Video Editor

Automatically edits talking head videos with:
- Silence cutting
- 3D Vox-style motion graphics (when you're just talking)
- Screen recording + face PiP switching (when you show your screen)
- Premiere Pro XML export (3-track timeline, ready to export)

---

## First Time Setup

### Requirements
- Windows 10/11
- [Git](https://git-scm.com/download/win)
- [Node.js LTS](https://nodejs.org)
- [Python 3.8+](https://python.org/downloads) — check "Add to PATH"
- [FFmpeg](https://ffmpeg.org/download.html) — extract to `C:\ffmpeg`, add `C:\ffmpeg\bin` to PATH

### Install

```
git clone https://github.com/akshaykiriti1443-alt/linkedin
cd linkedin\video-bot
```

Double-click **`install.bat`**

That's it. Takes ~5 minutes.

---

## Every Time You Edit a Video

Double-click **`run.bat`**

It will ask you:
1. What type of video? (Talking head / YouTube / Shorts / Floating cam)
2. Drag in your talking head video
3. Do you have a screen recording? (optional — drag it in if yes)

Then walk away. When it finishes:
- Open Premiere Pro
- `File → Import → workspace\final_timeline.xml`
- Export as H.264

---

## How the smart layout works

```
You're just talking?              You're showing your screen?
┌─────────────────────────┐       ┌─────────────────────────┐
│  3D Vox animations      │       │                         │
│  ─────────────────────  │       │   Your screen           │
│                         │       │   (full frame)          │
│  Your face (bottom)     │       │              [face PiP] │
└─────────────────────────┘       └─────────────────────────┘
      Auto: Vox mode                    Auto: Screen mode
```

The system detects this automatically per segment — no manual tagging needed.

---

## Profiles

| Profile | Best for |
|---|---|
| `talking-head` | Any talking head — auto-switches Vox/Screen |
| `shorts` | Instagram Reels / TikTok only |
| `youtube` | 16:9 YouTube essays |
| `floating-cam` | B-roll with small face PiP |

---

## Advanced: manual commands

```powershell
# Talking head only
python auto_edit.py workspace/raw.mp4 --profile talking-head

# With screen recording (started 5 seconds into talking head)
python auto_edit.py workspace/raw.mp4 --screen workspace/screen.mp4 --screen-offset 5.0

# Force YouTube layout
python auto_edit.py workspace/raw.mp4 --profile youtube
```

---

## Track layout in Premiere

```
V3  Text overlays       (Vox segments only)
V2  3D Vox graphics     (when talking) / Face PiP (when showing screen)
V1  Talking head        (bottom half)  / Screen recording (full frame)
A1  Voice
A2  Sound effects
```
