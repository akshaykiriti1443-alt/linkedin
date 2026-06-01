#!/usr/bin/env tsx
/**
 * /real-overlay — Scaffolds Overlay.tsx and prints transcript for Claude to plan overlays.
 */
import fs from 'fs';
import path from 'path';

const workspace = path.resolve('workspace');
const jsonPath = path.join(workspace, 'transcript.json');

if (!fs.existsSync(jsonPath)) {
  console.error('workspace/transcript.json not found. Run /transcribe first.');
  process.exit(1);
}

const whisperOutput = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));

// Word-level output for precise overlay placement
const words = whisperOutput.segments.flatMap((s: any) =>
  (s.words ?? [{ word: s.text, start: s.start, end: s.end }]).map((w: any) => ({
    word: w.word.trim(),
    start: w.start,
    end: w.end,
  }))
);

console.log(`
🎨 Real Overlay Planner
=======================
Word-level timestamps (for overlay placement):

${words.map((w: any) => `[${w.start.toFixed(2)}s] ${w.word}`).join('  ')}

📌 Instructions for Claude Code:
1. Identify high-impact words/phrases above
2. Write overlay components to src/compositions/overlays/
3. Each overlay: bold white text + colored box, y < 960px only
4. Update src/compositions/Overlay.tsx with <Sequence> tags
5. Run: npm run render-overlay
6. Check no element bleeds below y=960px
`);

// Ensure overlays dir exists
fs.mkdirSync(path.resolve('src/compositions/overlays'), { recursive: true });

// Skeleton Overlay.tsx
const overlayPath = path.resolve('src/compositions/Overlay.tsx');
if (!fs.existsSync(overlayPath)) {
  fs.writeFileSync(overlayPath, `import React from 'react';
import { AbsoluteFill, Sequence } from 'remotion';

// Claude Code fills this in after reading word timestamps
export default function Overlay() {
  return (
    <AbsoluteFill style={{ background: 'transparent' }}>
      {/* Overlays go here as <Sequence from={frame} durationInFrames={n}> */}
    </AbsoluteFill>
  );
}
`, 'utf8');
  console.log('✅ Created skeleton src/compositions/Overlay.tsx');
}
