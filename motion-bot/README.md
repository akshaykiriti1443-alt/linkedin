# Motion Graphics Bot

Generate and edit motion graphics using **Claude Code** (your Pro subscription) + **Remotion** (React video).

## Workflow

1. Tell Claude what motion graphic you want in the chat
2. Claude writes the code directly into `src/compositions/Scene.tsx`
3. You run `npm run render` to produce the MP4

No API key needed — Claude Code handles the AI part.

## Setup

```bash
cd motion-bot
npm install
```

## Render a scene

```bash
npm run render
# → out/video.mp4
```

## Live preview

```bash
npm run preview
# Opens Remotion Studio in browser
```

## Output

Videos render to `out/video.mp4` at 1920×1080, 30fps, 5 seconds by default.
