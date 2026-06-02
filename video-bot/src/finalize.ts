#!/usr/bin/env tsx
import fs from 'fs';
import path from 'path';
import { checkFFmpeg, run } from './utils/ffmpeg.js';
import { loadSFXEvents } from './utils/sfx-matcher.js';

if (!checkFFmpeg()) {
  console.error('FFmpeg not found. Install from https://ffmpeg.org/download.html');
  process.exit(1);
}

const workspace = path.resolve('workspace');
const outDir = path.resolve('out');
fs.mkdirSync(outDir, { recursive: true });

// Required inputs
const roughCut = path.join(workspace, 'rough_cut.mp4');
const topAnim = path.join(outDir, 'top_animations.mov');
const overlay = path.join(outDir, 'overlay.mov');
const srtFile = path.join(workspace, 'transcript.srt');
const jsonPath = path.join(workspace, 'transcript.json');
const finalOut = path.join(outDir, 'final_reel.mp4');

// Check required files
const missing = [roughCut, srtFile, jsonPath].filter(f => !fs.existsSync(f));
if (missing.length) {
  console.error('Missing required files:', missing.map(f => path.basename(f)).join(', '));
  console.error('Run the preceding steps first.');
  process.exit(1);
}

const hasTopAnim = fs.existsSync(topAnim);
const hasOverlay = fs.existsSync(overlay);

console.log('\n🎬 Finalizing video...\n');
console.log(`  Base:       rough_cut.mp4`);
console.log(`  Top anim:   ${hasTopAnim ? '✅' : '⚠️  missing (skipping)'}`);
console.log(`  Overlay:    ${hasOverlay ? '✅' : '⚠️  missing (skipping)'}`);
console.log(`  Captions:   ✅ transcript.srt\n`);

// SFX: use pre-mixed WAV from engine/build_sfx_track.py
const sfxWav = path.join(workspace, 'sfx_track.wav');
const hasSFXWav = fs.existsSync(sfxWav);

// Build FFmpeg command dynamically
const inputs = [`-i "${roughCut}"`];
if (hasTopAnim) inputs.push(`-i "${topAnim}"`);
if (hasOverlay)  inputs.push(`-i "${overlay}"`);
if (hasSFXWav)   inputs.push(`-i "${sfxWav}"`);

const filters: string[] = [];
let currentVideo = '0:v';
let inputOffset = 1;

if (hasTopAnim) {
  filters.push(`[${currentVideo}][${inputOffset}:v]overlay=0:0[v_top]`);
  currentVideo = 'v_top';
  inputOffset++;
}

if (hasOverlay) {
  filters.push(`[${currentVideo}][${inputOffset}:v]overlay=0:0[v_overlay]`);
  currentVideo = 'v_overlay';
  inputOffset++;
}

// Mix voice + SFX WAV if present
const sfxFilter = hasSFXWav
  ? `[0:a][${inputOffset}:a]amix=inputs=2:duration=first:dropout_transition=0[aout]`
  : '';
const audioOut = hasSFXWav ? '[aout]' : '0:a';

const filterParts = [...filters];
if (sfxFilter) filterParts.push(sfxFilter);

const subtitleFilter = `subtitles='${srtFile.replace(/'/g, "\\'")}':force_style='FontName=Arial,FontSize=20,Bold=1,PrimaryColour=&Hffffff,OutlineColour=&H000000,BorderStyle=3,Alignment=2,MarginV=40'`;

let ffmpegCmd = `ffmpeg -y ${inputs.join(' ')}`;

if (filterParts.length > 0) {
  const fc = filterParts.join(';\n');
  ffmpegCmd += ` -filter_complex "${fc.replace(/\n/g, ' ')}"`;
  ffmpegCmd += ` -map "[${currentVideo}]" -map "${audioOut}"`;
} else {
  ffmpegCmd += ` -map 0:v -map 0:a`;
}

ffmpegCmd += ` -vf "${subtitleFilter}"`;
ffmpegCmd += ` -c:v libx264 -preset fast -crf 18 -c:a aac -b:a 192k`;
ffmpegCmd += ` -s 1080x1920 "${finalOut}"`;

run(ffmpegCmd);

const stats = fs.statSync(finalOut);
const sizeMB = (stats.size / 1024 / 1024).toFixed(1);

console.log(`
✅ Final reel ready!

📁 Output: out/final_reel.mp4
   Size: ${sizeMB} MB
   Resolution: 1080×1920 (9:16)

🚀 Ready to post to Instagram Reels / TikTok / YouTube Shorts
`);
