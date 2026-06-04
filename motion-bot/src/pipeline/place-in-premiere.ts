import fs from 'fs';
import path from 'path';
import { v4 as uuidv4 } from 'uuid';
import type { RenderedCue } from './render-cues.js';

const TEMP_DIR = process.env.PREMIERE_TEMP_DIR || 'C:\\Temp\\premiere-editor';
const TIMEOUT_MS = 60_000;

async function writeCommand(cmd: Record<string, unknown>): Promise<unknown> {
  const id = uuidv4();
  const cmdFile = path.join(TEMP_DIR, `command-${id}.json`);
  const resFile = path.join(TEMP_DIR, `response-${id}.json`);

  fs.writeFileSync(cmdFile, JSON.stringify({ id, ...cmd, timestamp: new Date().toISOString() }));

  const start = Date.now();
  while (Date.now() - start < TIMEOUT_MS) {
    if (fs.existsSync(resFile)) {
      const raw = JSON.parse(fs.readFileSync(resFile, 'utf8'));
      fs.unlinkSync(cmdFile);
      fs.unlinkSync(resFile);
      return raw.result ?? raw;
    }
    await new Promise(r => setTimeout(r, 200));
  }

  throw new Error(`Premiere bridge timeout for command ${id}. Is the MCP Bridge panel running?`);
}

async function executeScript(script: string): Promise<unknown> {
  return writeCommand({ script });
}

export async function placeInPremiere(cues: RenderedCue[], sequenceId?: string): Promise<void> {
  if (!fs.existsSync(TEMP_DIR)) {
    throw new Error(`Premiere temp dir not found: ${TEMP_DIR}\nOpen the MCP Bridge panel in Premiere and set temp dir to this path.`);
  }

  // Get active sequence if not provided
  if (!sequenceId) {
    const info = await executeScript(`
      var seq = app.project.activeSequence;
      if (!seq) return JSON.stringify({ success: false, error: "No active sequence" });
      return JSON.stringify({ success: true, id: seq.sequenceID, name: seq.name });
    `) as any;
    if (!info?.success) throw new Error(info?.error || 'No active sequence in Premiere');
    sequenceId = info.id;
    console.log(`📺 Using active sequence: ${info.name}`);
  }

  // Add a graphics track (V2)
  await executeScript(`
    try {
      var seq = null;
      for (var i = 0; i < app.project.sequences.numSequences; i++) {
        if (app.project.sequences[i].sequenceID === "${sequenceId}") { seq = app.project.sequences[i]; break; }
      }
      if (!seq) return JSON.stringify({ success: false, error: "Sequence not found" });
      if (seq.videoTracks.numTracks < 2) seq.videoTracks.addTrack();
      return JSON.stringify({ success: true });
    } catch(e) { return JSON.stringify({ success: false, error: e.toString() }); }
  `);

  for (const cue of cues) {
    const winPath = cue.file.replace(/\//g, '\\');
    console.log(`\n📌 Placing ${path.basename(cue.file)} at ${cue.startTime.toFixed(2)}s...`);

    // Import the .mov
    const imported = await executeScript(`
      try {
        var file = new File(${JSON.stringify(winPath)});
        if (!file.exists) return JSON.stringify({ success: false, error: "File not found: ${winPath}" });
        app.project.importFiles([file.fsName], true, app.project.rootItem, false);
        var item = app.project.rootItem.children[app.project.rootItem.children.numItems - 1];
        return JSON.stringify({ success: true, id: item.nodeId, name: item.name });
      } catch(e) { return JSON.stringify({ success: false, error: e.toString() }); }
    `) as any;

    if (!imported?.success) {
      console.error(`❌ Import failed: ${imported?.error}`);
      continue;
    }

    // Place on V2 at correct timecode, no audio link
    const placed = await executeScript(`
      try {
        var seq = null;
        for (var i = 0; i < app.project.sequences.numSequences; i++) {
          if (app.project.sequences[i].sequenceID === "${sequenceId}") { seq = app.project.sequences[i]; break; }
        }
        if (!seq) return JSON.stringify({ success: false, error: "Sequence not found" });
        var item = null;
        function walk(p) {
          for (var i = 0; i < p.children.numItems; i++) {
            var c = p.children[i];
            if (c.nodeId === "${imported.id}") { item = c; return; }
            if (c.children) walk(c);
          }
        }
        walk(app.project.rootItem);
        if (!item) return JSON.stringify({ success: false, error: "Item not found" });
        var track = seq.videoTracks[1];
        track.overwriteClip(item, ${cue.startTime});
        return JSON.stringify({ success: true });
      } catch(e) { return JSON.stringify({ success: false, error: e.toString() }); }
    `) as any;

    if (placed?.success) {
      console.log(`✅ Placed ${path.basename(cue.file)}`);
    } else {
      console.error(`❌ Place failed: ${placed?.error}`);
    }
  }

  console.log('\n🎉 All cues placed in Premiere!');
}
