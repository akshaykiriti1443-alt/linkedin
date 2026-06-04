#!/usr/bin/env tsx
import { execSync, spawnSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { parseWhisper, detectSilences, toSRT, toPlainText } from './utils/parse-whisper.js';
import { detectRetakes } from './utils/detect-retakes.js';

const args = process.argv.slice(2);
const videoArg = args.find(a => a.startsWith('--video='));
const modelArg = args.find(a => a.startsWith('--model=')) || '--model=base';

if (!videoArg) {
  console.error('Usage: npm run transcribe -- --video="path/to/video.mp4" [--model=base|small|medium]');
  process.exit(1);
}

const videoPath = videoArg.split('=').slice(1).join('=');
const model = modelArg.split('=')[1];

if (!fs.existsSync(videoPath)) {
  console.error(`Video not found: ${videoPath}`);
  process.exit(1);
}

// Check Whisper
const whisperCheck = spawnSync('whisper', ['--help'], { stdio: 'pipe' });
if (whisperCheck.status !== 0) {
  console.error(`
Whisper not found. Install it:
  pip install openai-whisper
  # or for faster version:
  pip install faster-whisper
`);
  process.exit(1);
}

const workspaceDir = path.resolve('workspace');
fs.mkdirSync(workspaceDir, { recursive: true });

console.log(`\n🎙️  Transcribing ${path.basename(videoPath)} with Whisper (model: ${model})...\n`);

const outputBase = path.join(workspaceDir, 'transcript');

execSync(
  `whisper "${videoPath}" --model ${model} --word_timestamps True --output_format all --output_dir "${workspaceDir}" --output_name transcript`,
  { stdio: 'inherit' }
);

// Parse the JSON output
const jsonPath = outputBase + '.json';
if (!fs.existsSync(jsonPath)) {
  console.error('Whisper did not produce transcript.json. Check output above.');
  process.exit(1);
}

const whisperOutput = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
const words = parseWhisper(whisperOutput);
const silences = detectSilences(words, 0.5);
const retakes = detectRetakes(words, 3);

// Write SRT if not already written by Whisper
const srtPath = outputBase + '.srt';
if (!fs.existsSync(srtPath)) {
  fs.writeFileSync(srtPath, toSRT(whisperOutput.segments), 'utf8');
}

// Write plain text
const txtPath = outputBase + '.txt';
fs.writeFileSync(txtPath, toPlainText(whisperOutput.segments), 'utf8');

// Write analysis
const analysis = { silences, retakes, wordCount: words.length, duration: words[words.length - 1]?.end ?? 0 };
fs.writeFileSync(path.join(workspaceDir, 'analysis.json'), JSON.stringify(analysis, null, 2), 'utf8');

console.log(`
✅ Transcription complete!

📊 Summary:
   Words:     ${words.length}
   Duration:  ${analysis.duration.toFixed(1)}s
   Silences:  ${silences.length} gaps > 0.5s (${silences.reduce((a, s) => a + s.duration, 0).toFixed(1)}s total)
   Retakes:   ${retakes.length} detected

📁 Files written to workspace/:
   transcript.json  — word-level timestamps
   transcript.srt   — captions
   transcript.txt   — plain text
   analysis.json    — silences + retakes

▶️  Next: run /produce to cut silences, remove retakes, reformat to 9:16
`);
