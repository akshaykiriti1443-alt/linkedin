# Motion Graphics Bot

Generate and edit motion graphics with plain English, powered by **Claude** (AI) + **Remotion** (React video).

## How it works

```
You (chat prompt) → Claude API → Remotion TSX composition → rendered MP4
```

The bot keeps a conversation history so you can iteratively refine scenes:
- "Make a glowing blue logo reveal"
- "Add a subtitle that slides in from the left"
- "Make it faster and use a purple gradient instead"

## Setup

```bash
cd motion-bot
npm install
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env
```

## Usage

```bash
npm run bot
```

**Example session:**
```
You > neon text reveal for the word LAUNCH on a dark background
⏳ Generating...
✅ Composition written → src/compositions/neon-text-reveal-for-the-word-la.tsx

You > make the text pulse after it appears
⏳ Generating...
✅ Composition written → src/compositions/neon-text-reveal-for-the-word-la.tsx

You > render
🎬 Rendering...
✅ Rendered → out/neon-text-reveal-for-the-word-la.mp4
```

## Preview (live)

```bash
npm run preview
```
Opens Remotion Studio in your browser for a real-time preview.

## Commands

| Input | Action |
|-------|--------|
| Any text | Generate/revise the composition |
| `render` | Render current scene to `out/*.mp4` |
| `exit` | Quit |

## Requirements

- Node.js 18+
- `ANTHROPIC_API_KEY` environment variable
