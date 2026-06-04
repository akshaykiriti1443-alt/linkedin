#!/usr/bin/env tsx
/**
 * /new-series — Scaffolds the TopHalf Remotion composition.
 * Claude Code writes the actual scene content after reading the transcript.
 * This script creates the skeleton and prints the transcript for Claude to plan.
 */
import fs from 'fs';
import path from 'path';
import { toPlainText } from './utils/parse-whisper.js';

const workspace = path.resolve('workspace');
const jsonPath = path.join(workspace, 'transcript.json');
const reportPath = path.join(workspace, 'edit_report.json');

if (!fs.existsSync(jsonPath)) {
  console.error('workspace/transcript.json not found. Run /transcribe first.');
  process.exit(1);
}

const whisperOutput = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
const report = fs.existsSync(reportPath) ? JSON.parse(fs.readFileSync(reportPath, 'utf8')) : null;
const duration = parseFloat(report?.finalDuration ?? whisperOutput.segments.at(-1)?.end ?? 30);
const totalFrames = Math.round(duration * 30);

// Ensure scenes directory exists
fs.mkdirSync(path.resolve('src/compositions/scenes'), { recursive: true });

// Print transcript for Claude to plan scenes
const transcript = whisperOutput.segments.map((s: any) =>
  `[${s.start.toFixed(2)}s–${s.end.toFixed(2)}s] ${s.text.trim()}`
).join('\n');

console.log(`
🎬 New Series — Top-Half Animation Planner
==========================================
Video duration: ${duration.toFixed(1)}s (${totalFrames} frames @ 30fps)

TRANSCRIPT (for Claude to plan scenes):
----------------------------------------
${transcript}
----------------------------------------

📌 Instructions for Claude Code:
1. Read the transcript above
2. Divide into 3–6 segments (Hook / Main Points / CTA)
3. For each segment, write a React/Remotion scene to src/compositions/scenes/
4. ALL elements must stay within y: 0–960px (top 50% of 1080×1920)
5. Update src/compositions/TopHalf.tsx with <Sequence> tags
6. Run: npm run render-top
`);

// Write a skeleton TopHalf.tsx if it doesn't exist
const topHalfPath = path.resolve('src/compositions/TopHalf.tsx');
if (!fs.existsSync(topHalfPath)) {
  fs.writeFileSync(topHalfPath, `import React from 'react';
import { AbsoluteFill, Sequence } from 'remotion';

// Claude Code will fill this in after reading the transcript
export default function TopHalf() {
  return (
    <AbsoluteFill style={{ background: 'transparent', width: 1080, height: 960 }}>
      {/* Scenes go here as <Sequence from={frame} durationInFrames={n}> */}
    </AbsoluteFill>
  );
}
`, 'utf8');
  console.log('✅ Created skeleton src/compositions/TopHalf.tsx');
}
