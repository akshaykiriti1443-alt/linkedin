#!/usr/bin/env tsx
import fs from 'fs';
import path from 'path';
import { parseWhisper, detectSilences, type WhisperWord } from './utils/parse-whisper.js';
import { detectRetakes } from './utils/detect-retakes.js';
import { checkFFmpeg, buildConcatFilter, build916Filter, run, getVideoDuration } from './utils/ffmpeg.js';

if (!checkFFmpeg()) {
  console.error('FFmpeg not found. Install from https://ffmpeg.org/download.html');
  process.exit(1);
}

const workspace = path.resolve('workspace');
const jsonPath = path.join(workspace, 'transcript.json');

if (!fs.existsSync(jsonPath)) {
  console.error('workspace/transcript.json not found. Run /transcribe first.');
  process.exit(1);
}

// Find the raw video from analysis
const analysisPath = path.join(workspace, 'analysis.json');
const videoArgIdx = process.argv.findIndex(a => a.startsWith('--video='));
let rawVideo = videoArgIdx >= 0 ? process.argv[videoArgIdx].split('=').slice(1).join('=') : '';

if (!rawVideo) {
  // Try to find the last used video path
  const metaPath = path.join(workspace, 'meta.json');
  if (fs.existsSync(metaPath)) rawVideo = JSON.parse(fs.readFileSync(metaPath, 'utf8')).videoPath;
}

if (!rawVideo || !fs.existsSync(rawVideo)) {
  console.error('Video path not found. Pass --video="path/to/video.mp4"');
  process.exit(1);
}

// Save meta
fs.writeFileSync(path.join(workspace, 'meta.json'), JSON.stringify({ videoPath: rawVideo }), 'utf8');

console.log('\n✂️  Building rough cut...\n');

const whisperOutput = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
const words: WhisperWord[] = parseWhisper(whisperOutput);
const silences = detectSilences(words, 0.5);
const retakes = detectRetakes(words, 3);

const originalDuration = words[words.length - 1]?.end ?? 0;

// Build keep segments by inverting the cut list
interface Cut { start: number; end: number }
const cuts: Cut[] = [
  ...silences.map(s => ({ start: s.start + 0.05, end: s.end - 0.05 })),
  ...retakes.map(r => ({ start: r.original.start, end: r.keepStart })),
].sort((a, b) => a.start - b.start);

// Merge overlapping cuts
const merged: Cut[] = [];
for (const cut of cuts) {
  if (merged.length && cut.start <= merged[merged.length - 1].end) {
    merged[merged.length - 1].end = Math.max(merged[merged.length - 1].end, cut.end);
  } else {
    merged.push({ ...cut });
  }
}

// Invert to get keep segments
const keeps: { start: number; end: number }[] = [];
let cursor = 0;
for (const cut of merged) {
  if (cut.start > cursor + 0.05) keeps.push({ start: cursor, end: cut.start });
  cursor = cut.end;
}
if (cursor < originalDuration - 0.05) keeps.push({ start: cursor, end: originalDuration });

if (keeps.length === 0) {
  console.error('No keep segments found — check transcript.');
  process.exit(1);
}

const finalDuration = keeps.reduce((a, k) => a + (k.end - k.start), 0);
const timeSaved = originalDuration - finalDuration;

// Step 1: concat cut → temp file
const concatOut = path.join(workspace, 'concat_cut.mp4');
const { filterComplex, inputs } = buildConcatFilter(keeps);
const escapedFilter = filterComplex.replace(/\n/g, ' ');

run(`ffmpeg -y -i "${rawVideo}" -filter_complex "${escapedFilter}" ${inputs} -c:v libx264 -preset fast -crf 18 -c:a aac "${concatOut}"`);

// Step 2: reformat to 9:16 with face in bottom 50%
const roughOut = path.join(workspace, 'rough_cut.mp4');
const filter916 = build916Filter().replace(/\n/g, ' ');

run(`ffmpeg -y -i "${concatOut}" -filter_complex "${filter916}" -map "[out]" -map 0:a -c:v libx264 -preset fast -crf 18 -c:a aac "${roughOut}"`);

// Cleanup temp
fs.unlinkSync(concatOut);

// Write edit report
const report = {
  originalDuration: originalDuration.toFixed(2),
  finalDuration: finalDuration.toFixed(2),
  timeSaved: timeSaved.toFixed(2),
  silencesCut: silences.length,
  retakesRemoved: retakes.length,
  keepSegments: keeps.length,
  outputFile: roughOut,
};
fs.writeFileSync(path.join(workspace, 'edit_report.json'), JSON.stringify(report, null, 2), 'utf8');

console.log(`
✅ Rough cut complete!

📊 Edit Report:
   Original:    ${report.originalDuration}s
   Final:       ${report.finalDuration}s
   Time saved:  ${report.timeSaved}s
   Silences cut:   ${report.silencesCut}
   Retakes removed: ${report.retakesRemoved}

📁 Output: workspace/rough_cut.mp4 (9:16, face = bottom 50%)

▶️  Next: run /new-series to generate top-half animations
`);
