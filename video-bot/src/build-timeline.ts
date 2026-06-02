#!/usr/bin/env tsx
/**
 * build-timeline.ts — Assembles the full 3-track smart FCP XML for Premiere.
 *
 * MODE A segments (talking head only):
 *   V1 = talking head (bottom half)     V2 = Vox 3D .mov     V3 = text overlays
 * MODE B segments (screen recording):
 *   V1 = screen recording (full)        V2 = talking head PiP (already in timeline_cut.xml)
 *
 * Reads workspace/segments_meta.json written by generate_xml.py.
 */

import fs from 'fs';
import path from 'path';
import { loadSFXEvents } from './utils/sfx-matcher.js';

const TIMEBASE = 30;
const ticks = (sec: number) => Math.round(sec * TIMEBASE);

// ── Minimal XML builder ────────────────────────────────────────────────────
function el(tag: string, attrs: Record<string, string> = {}, children: string[] = [], text = ''): string {
  const attrStr = Object.entries(attrs).map(([k, v]) => ` ${k}="${v}"`).join('');
  const inner = text || children.join('\n');
  if (!inner) return `<${tag}${attrStr}/>`;
  return `<${tag}${attrStr}>\n${inner}\n</${tag}>`;
}

function rateEl(tb = TIMEBASE) {
  return el('rate', {}, [el('timebase', {}, [], String(tb)), el('ntsc', {}, [], 'FALSE')]);
}

function fileEl(id: string, filePath: string) {
  const absPath = path.resolve(filePath);
  const url = 'file://localhost' + (absPath.startsWith('/') ? absPath : '/' + absPath).replace(/\\/g, '/');
  return el('file', { id }, [el('pathurl', {}, [], url), rateEl()]);
}

function clipItem(
  id: string, name: string,
  tlStart: number, tlEnd: number,
  inPt: number, outPt: number,
  fileId: string,
  isAudio = false,
  motionParams: Record<string, string> = {}
): string {
  const dur = outPt - inPt;
  const parts = [
    el('name', {}, [], name),
    el('start', {}, [], String(ticks(tlStart))),
    el('end', {}, [], String(ticks(tlEnd))),
    el('in', {}, [], String(ticks(inPt))),
    el('out', {}, [], String(ticks(outPt))),
    el('file', { id: fileId }),
  ];
  if (isAudio) parts.push(el('channelcount', {}, [], '2'));
  if (Object.keys(motionParams).length > 0) {
    const paramEls = Object.entries(motionParams).map(([k, v]) =>
      el('parameter', {}, [el('name', {}, [], k), el('value', {}, [], v)])
    );
    parts.push(el('filters', {}, [el('filter', {}, [el('name', {}, [], 'Motion'), ...paramEls])]));
  }
  return el('clipitem', { id }, parts);
}

// ── Paths ──────────────────────────────────────────────────────────────────
const workspace   = path.resolve('workspace');
const importsDir  = path.resolve('premiere_imports');
const graphicsDir = path.join(importsDir, 'graphics');
const overlaysDir = path.join(importsDir, 'overlays');

const v1XmlPath    = path.join(workspace, 'timeline_cut.xml');
const jsonPath     = path.join(workspace, 'transcript.json');
const metaPath     = path.join(workspace, 'meta.json');
const segMetaPath  = path.join(workspace, 'segments_meta.json');
const outputPath   = path.join(workspace, 'final_timeline.xml');

if (!fs.existsSync(v1XmlPath)) {
  console.error('workspace/timeline_cut.xml not found. Run /produce first.');
  process.exit(1);
}

// ── Load segment metadata ─────────────────────────────────────────────────
interface SegMeta {
  tl_start: number; tl_end: number;
  src_start: number; src_end: number;
  mode: 'vox' | 'screen';
}

const segMeta: SegMeta[] = fs.existsSync(segMetaPath)
  ? JSON.parse(fs.readFileSync(segMetaPath, 'utf8'))
  : [];

const voxSegs    = segMeta.filter(s => s.mode === 'vox');
const screenSegs = segMeta.filter(s => s.mode === 'screen');
const totalDuration = segMeta.length > 0
  ? segMeta[segMeta.length - 1].tl_end
  : 0;

// ── Scan .mov assets ──────────────────────────────────────────────────────
function findMovFiles(dir: string): string[] {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir)
    .filter(f => f.endsWith('.mov'))
    .sort()
    .map(f => path.join(dir, f));
}

const graphicsFiles = findMovFiles(graphicsDir);
const overlayFiles  = findMovFiles(overlaysDir);

// ── SFX — single mixed WAV from engine/build_sfx_track.py ────────────────
const sfxWavPath   = path.join(workspace, 'sfx_track.wav');
const sfxEventsPath = path.join(workspace, 'sfx_events.json');
const hasSFXWav    = fs.existsSync(sfxWavPath);

// If sfx_events.json missing, write a draft from transcript for Claude to fill
const sfxDraftPath = path.join(workspace, 'sfx_events_draft.json');
if (!fs.existsSync(sfxEventsPath) && fs.existsSync(jsonPath) && !fs.existsSync(sfxDraftPath)) {
  const { parseWhisper } = await import('./utils/parse-whisper.js');
  const { writeSFXDraft } = await import('./utils/sfx-matcher.js');
  const words = parseWhisper(JSON.parse(fs.readFileSync(jsonPath, 'utf8')));
  writeSFXDraft(words, sfxDraftPath);
  console.log('📋 SFX draft written → workspace/sfx_events_draft.json');
  console.log('   Ask Claude to fill the events array, save as sfx_events.json, then:');
  console.log('   python engine/build_sfx_track.py');
}

// ── V2: Vox 3D .mov placed at MODE A segment timecodes ───────────────────
// If we have one graphics file, repeat it across all vox segments.
// If we have multiple, distribute them across vox segments in order.
let v2Track = '';
let v2Files = '';

if (graphicsFiles.length > 0 && voxSegs.length > 0) {
  const v2Parts: string[] = [];
  const v2FileParts: string[] = [];
  voxSegs.forEach((seg, i) => {
    const fileIdx = Math.min(i, graphicsFiles.length - 1);
    const f = graphicsFiles[fileIdx];
    const name = path.basename(f);
    const dur = seg.tl_end - seg.tl_start;
    v2FileParts.push(fileEl(`v2f${i}`, f));
    v2Parts.push(clipItem(`v2_${i}`, name, seg.tl_start, seg.tl_end, 0, dur, `v2f${i}`));
  });
  v2Track = v2Parts.join('\n');
  v2Files = v2FileParts.join('\n');
} else if (graphicsFiles.length > 0) {
  // No metadata — fall back to evenly distributed
  graphicsFiles.forEach((f, i) => {
    const startRatio = graphicsFiles.length > 1 ? i / graphicsFiles.length : 0;
    const tlStart = startRatio * totalDuration;
    const dur = Math.min(5, totalDuration / Math.max(graphicsFiles.length, 1));
    v2Files += fileEl(`v2f${i}`, f) + '\n';
    v2Track += clipItem(`v2_${i}`, path.basename(f), tlStart, tlStart + dur, 0, dur, `v2f${i}`) + '\n';
  });
}

// ── V3: Overlays (only on MODE A vox segments) ────────────────────────────
let v3Track = '';
let v3Files = '';

if (overlayFiles.length > 0 && voxSegs.length > 0) {
  const targetSegs = voxSegs;
  overlayFiles.forEach((f, i) => {
    const seg = targetSegs[Math.min(i, targetSegs.length - 1)];
    const dur = seg.tl_end - seg.tl_start;
    v3Files += fileEl(`v3f${i}`, f) + '\n';
    v3Track += clipItem(`v3_${i}`, path.basename(f), seg.tl_start, seg.tl_end, 0, dur, `v3f${i}`) + '\n';
  });
}

// ── A2: Single mixed SFX WAV (built by engine/build_sfx_track.py) ─────────
let sfxTrack = '';
let sfxFiles = '';
if (hasSFXWav) {
  sfxFiles = fileEl('sfxf0', sfxWavPath);
  sfxTrack = clipItem('sfx_0', 'sfx_track.wav', 0, totalDuration, 0, totalDuration, 'sfxf0', true);
}

// ── Extract V1 track + A1 audio from timeline_cut.xml ────────────────────
const v1Xml = fs.readFileSync(v1XmlPath, 'utf8');
const v1TrackMatch = v1Xml.match(/<video>[\s\S]*?(<track>[\s\S]*?<\/track>)/);
const v1TrackXml   = v1TrackMatch?.[1] ?? '<!-- V1 track not found -->';
const a1AudioMatch = v1Xml.match(/<audio>([\s\S]*?)<\/audio>/);
const a1AudioXml   = a1AudioMatch?.[1] ?? '';

// ── Sequence dimensions from meta ─────────────────────────────────────────
const meta = fs.existsSync(metaPath) ? JSON.parse(fs.readFileSync(metaPath, 'utf8')) : {};
const isYoutube = meta.layout === 'youtube';
const seqW = isYoutube ? 1920 : 1080;
const seqH = isYoutube ? 1080 : 1920;

// ── Assemble full XML ─────────────────────────────────────────────────────
const xml = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
<sequence>
  <name>AI Smart Timeline — Vox + Screen Hybrid</name>
  <rate><timebase>${TIMEBASE}</timebase><ntsc>FALSE</ntsc></rate>
  <duration>${ticks(totalDuration)}</duration>
  <media>

    <video>
      <format>
        <samplecharacteristics>
          <width>${seqW}</width>
          <height>${seqH}</height>
          <rate><timebase>${TIMEBASE}</timebase><ntsc>FALSE</ntsc></rate>
        </samplecharacteristics>
      </format>

      <!--
        V1: Talking head (MODE A = bottom half) OR Screen recording (MODE B = full frame)
            MODE B also has talking-head PiP baked into this track from generate_xml.py
      -->
      ${v1TrackXml}

      <!-- V2: Vox 3D motion graphics — placed only at MODE A (vox) segment timecodes -->
      <track>
        ${v2Files}
        ${v2Track || '<!-- No graphics rendered yet — run: npm run render-top -->'}
      </track>

      <!-- V3: Transparent text overlays — only on MODE A segments -->
      <track>
        ${v3Files}
        ${v3Track || '<!-- No overlays rendered yet — run: npm run render-overlay -->'}
      </track>

    </video>

    <audio>
      <!-- A1: Voice (always from talking head, regardless of mode) -->
      ${a1AudioXml}

      <!-- A2: SFX at keyword timecodes -->
      <track>
        ${sfxFiles}
        ${sfxTrack || '<!-- No SFX cues matched -->'}
      </track>
    </audio>

  </media>
</sequence>
</xmeml>`;

fs.writeFileSync(outputPath, xml, 'utf8');

const modeASec = voxSegs.reduce((a, s) => a + (s.tl_end - s.tl_start), 0);
const modeBSec = screenSegs.reduce((a, s) => a + (s.tl_end - s.tl_start), 0);

console.log(`
✅ Final timeline written → workspace/final_timeline.xml

Mode breakdown:
  MODE A  Vox + talking head  : ${voxSegs.length} segments  (${modeASec.toFixed(1)}s)
  MODE B  Screen + face PiP   : ${screenSegs.length} segments  (${modeBSec.toFixed(1)}s)

Tracks:
  V1  Talking head / Screen recording   ${segMeta.length} clips
  V2  Vox 3D graphics (MODE A only)     ${graphicsFiles.length} file(s)
  V3  Text overlays  (MODE A only)      ${overlayFiles.length} file(s)
  A1  Voice audio
  A2  SFX track                          ${hasSFXWav ? 'workspace/sfx_track.wav' : 'not built yet — run: python engine/build_sfx_track.py'}

▶  Premiere: File > Import > workspace/final_timeline.xml
`);
