#!/usr/bin/env tsx
/**
 * build-timeline.ts — Assembles the full 3-track FCP XML for Premiere:
 *   V1  Talking head (from timeline_cut.xml)
 *   V2  Remotion motion graphics (premiere_imports/graphics/*.mov)
 *   V3  Transparent overlays     (premiere_imports/overlays/*.mov)
 *   A1  Original voice (already in V1 cut)
 *   A2  SFX (from transcript keyword matching)
 */

import fs from 'fs';
import path from 'path';
import { parseWhisper } from './utils/parse-whisper.js';
import { matchSFX } from './utils/sfx-matcher.js';

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

function clipItem(id: string, name: string, tlStart: number, tlEnd: number,
                  inPt: number, outPt: number, fileId: string, isAudio = false): string {
  const parts = [
    el('name', {}, [], name),
    el('start', {}, [], String(ticks(tlStart))),
    el('end', {}, [], String(ticks(tlEnd))),
    el('in', {}, [], String(ticks(inPt))),
    el('out', {}, [], String(ticks(outPt))),
    el('file', { id: fileId }),
  ];
  if (isAudio) parts.push(el('channelcount', {}, [], '2'));
  return el('clipitem', { id }, parts);
}

// ── Load V1 keep segments from generate_xml.py output ────────────────────
interface Segment { start: number; end: number }

function parseV1XML(xmlPath: string): Segment[] {
  if (!fs.existsSync(xmlPath)) return [];
  const xml = fs.readFileSync(xmlPath, 'utf8');
  const segs: Segment[] = [];
  const re = /<clipitem[^>]*id="v\d+"[\s\S]*?<in>([\d.]+)<\/in>[\s\S]*?<out>([\d.]+)<\/out>/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(xml)) !== null) {
    segs.push({ start: parseInt(m[1]) / TIMEBASE, end: parseInt(m[2]) / TIMEBASE });
  }
  return segs;
}

// ── Scan for rendered .mov files ──────────────────────────────────────────
function findMovFiles(dir: string): string[] {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir)
    .filter(f => f.endsWith('.mov'))
    .sort()
    .map(f => path.join(dir, f));
}

// ── Main ──────────────────────────────────────────────────────────────────
const workspace = path.resolve('workspace');
const importsDir = path.resolve('premiere_imports');
const graphicsDir = path.join(importsDir, 'graphics');
const overlaysDir = path.join(importsDir, 'overlays');

const v1XmlPath  = path.join(workspace, 'timeline_cut.xml');
const jsonPath   = path.join(workspace, 'transcript.json');
const metaPath   = path.join(workspace, 'meta.json');
const outputPath = path.join(workspace, 'final_timeline.xml');

if (!fs.existsSync(v1XmlPath)) {
  console.error('workspace/timeline_cut.xml not found. Run /produce first.');
  process.exit(1);
}

const v1Segments = parseV1XML(v1XmlPath);
const totalDuration = v1Segments.reduce((a, s) => a + (s.end - s.start), 0);

const graphicsFiles = findMovFiles(graphicsDir);
const overlayFiles  = findMovFiles(overlaysDir);

// SFX cues from transcript
let sfxCues: { time: number; file: string }[] = [];
if (fs.existsSync(jsonPath)) {
  const words = parseWhisper(JSON.parse(fs.readFileSync(jsonPath, 'utf8')));
  sfxCues = matchSFX(words).filter(c => fs.existsSync(c.file));
}

// Raw video path for linking audio
const rawVideo = fs.existsSync(metaPath)
  ? JSON.parse(fs.readFileSync(metaPath, 'utf8')).videoPath
  : '';

// ── Build tracks ──────────────────────────────────────────────────────────

// V1 — re-link existing XML clips (include the already-generated timeline_cut.xml content)
const v1Xml = fs.readFileSync(v1XmlPath, 'utf8');

// V2 — graphics clips, evenly distributed across timeline if no timestamp metadata
const v2Clips = graphicsFiles.map((f, i) => {
  const name = path.basename(f);
  const startRatio = graphicsFiles.length > 1 ? (i / graphicsFiles.length) : 0;
  const tlStart = startRatio * totalDuration;
  const dur = Math.min(5, totalDuration / Math.max(graphicsFiles.length, 1));
  return clipItem(`v2_${i}`, name, tlStart, tlStart + dur, 0, dur, `v2f${i}`);
}).join('\n');

const v2FileEls = graphicsFiles.map((f, i) =>
  fileEl(`v2f${i}`, f)
).join('\n');

// V3 — overlay clips
const v3Clips = overlayFiles.map((f, i) => {
  const name = path.basename(f);
  const tlStart = (i / Math.max(overlayFiles.length, 1)) * totalDuration;
  const dur = Math.min(3, totalDuration / Math.max(overlayFiles.length, 1));
  return clipItem(`v3_${i}`, name, tlStart, tlStart + dur, 0, dur, `v3f${i}`);
}).join('\n');

const v3FileEls = overlayFiles.map((f, i) =>
  fileEl(`v3f${i}`, f)
).join('\n');

// A2 — SFX audio clips
const sfxClips = sfxCues.map((c, i) => {
  const dur = 0.5;
  return clipItem(`sfx_${i}`, path.basename(c.file), c.time, c.time + dur, 0, dur, `sfxf${i}`, true);
}).join('\n');

const sfxFileEls = sfxCues.map((c, i) =>
  fileEl(`sfxf${i}`, c.file)
).join('\n');

// ── Assemble full XML ─────────────────────────────────────────────────────
const xml = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
<sequence>
  <name>AI Final Timeline — 3 Track</name>
  ${rateEl()}
  <duration>${ticks(totalDuration)}</duration>
  <media>

    <video>
      <format>
        <samplecharacteristics>
          <width>1080</width>
          <height>1920</height>
          ${rateEl()}
        </samplecharacteristics>
      </format>

      <!-- V1: Talking head (rough cut) -->
      ${v1Xml.match(/<track>[\s\S]*?<\/track>/)?.[0] ?? '<track/>'}

      <!-- V2: Motion graphics (top 50%) -->
      <track>
        ${v2FileEls}
        ${v2Clips || '<!-- No graphics rendered yet — run /new-series -->'}
      </track>

      <!-- V3: Transparent overlays -->
      <track>
        ${v3FileEls}
        ${v3Clips || '<!-- No overlays rendered yet — run /real-overlay -->'}
      </track>
    </video>

    <audio>
      <!-- A1: Voice (linked from V1) -->
      ${v1Xml.match(/<audio>[\s\S]*?<\/audio>/)?.[0]?.replace(/<audio>|<\/audio>/g, '') ?? ''}

      <!-- A2: SFX -->
      <track>
        ${sfxFileEls}
        ${sfxClips || '<!-- No SFX cues matched -->'}
      </track>
    </audio>

  </media>
</sequence>
</xmeml>`;

fs.writeFileSync(outputPath, xml, 'utf8');

console.log(`
✅ Final timeline XML written → workspace/final_timeline.xml

Tracks assembled:
  V1  Talking head — ${v1Segments.length} segments, ${totalDuration.toFixed(1)}s
  V2  Graphics     — ${graphicsFiles.length} clip(s)
  V3  Overlays     — ${overlayFiles.length} clip(s)
  A2  SFX          — ${sfxCues.length} cue(s)

▶  Premiere: File > Import > workspace/final_timeline.xml
   Then apply the Shorts_Bottom_Half preset to all V1 clips:
     Scale: ~180%  |  Position Y: 1440
`);
