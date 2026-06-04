#!/usr/bin/env tsx
/**
 * Orchestrator — runs the full pipeline:
 *   1. Parse transcript (SRT)
 *   2. Print formatted transcript → Claude Code writes cue compositions
 *   3. Render all cues to out/cues/*.mov
 *   4. Place in Premiere via MCP bridge
 *
 * Usage:
 *   npm run produce -- --transcript="captions.srt"
 *   npm run render-cues          (after Claude writes compositions)
 *   npm run place-in-premiere
 */

import path from 'path';
import { parseTranscript, transcriptToText } from './parse-transcript.js';
import { renderAllCues } from './render-cues.js';
import { placeInPremiere } from './place-in-premiere.js';

const args = process.argv.slice(2);
const command = args[0] || 'help';

if (command === 'parse') {
  const transcriptArg = args.find(a => a.startsWith('--transcript='));
  if (!transcriptArg) { console.error('Usage: npm run produce -- parse --transcript=path/to/file.srt'); process.exit(1); }
  const file = transcriptArg.split('=').slice(1).join('=');
  const cues = parseTranscript(file);
  console.log(`\n📄 Parsed ${cues.length} cues from ${path.basename(file)}\n`);
  console.log('=== TRANSCRIPT (paste to Claude Code) ===\n');
  console.log(transcriptToText(cues));
  console.log('\n=========================================');
  console.log('\nNext step: Tell Claude Code what style you want (vox/ali/both)');
  console.log('Claude will write compositions to src/compositions/cues/');
  console.log('Then run: npm run render-cues');
}

else if (command === 'render-cues') {
  console.log('🎬 Rendering all cue compositions...\n');
  const rendered = renderAllCues();
  console.log(`\n✅ Rendered ${rendered.length} cues to out/cues/`);
  console.log('\nNext step: npm run place-in-premiere');
}

else if (command === 'place-in-premiere') {
  import('../pipeline/render-cues.js').then(async ({ renderAllCues: _r }) => {
    // Load cue metadata from out/cues/ without re-rendering
    const fs = await import('fs');
    const outDir = 'out/cues';
    if (!fs.default.existsSync(outDir)) {
      console.error('No rendered cues found. Run: npm run render-cues first.');
      process.exit(1);
    }
    const files = fs.default.readdirSync(outDir).filter(f => f.endsWith('.mov'));
    const cues = files.map(f => {
      const srcFile = `src/compositions/cues/${f.replace('.mov', '.tsx')}`;
      let startTime = 0;
      let duration = 3;
      if (fs.default.existsSync(srcFile)) {
        const content = fs.default.readFileSync(srcFile, 'utf8');
        const sm = content.match(/META_START:([\d.]+)/);
        const dm = content.match(/META_DURATION:([\d.]+)/);
        if (sm) startTime = parseFloat(sm[1]);
        if (dm) duration = parseFloat(dm[1]);
      }
      return { id: f.replace('.mov', ''), file: `${outDir}/${f}`, startTime, duration };
    });
    await placeInPremiere(cues);
  });
}

else {
  console.log(`
🎬 AI Video Production Pipeline

Commands:
  npm run produce -- parse --transcript=file.srt
      → Parses SRT and prints transcript for Claude to plan

  npm run render-cues
      → Renders all compositions in src/compositions/cues/ to out/cues/*.mov

  npm run place-in-premiere
      → Places rendered .mov files into Premiere at correct timecodes

Workflow:
  1. Export captions from Premiere as .srt
  2. Run: npm run produce -- parse --transcript=your-file.srt
  3. Paste the output here in Claude Code and say: "make vox/ali graphics"
  4. Claude writes compositions to src/compositions/cues/
  5. Run: npm run render-cues
  6. Open Premiere, start MCP Bridge panel
  7. Run: npm run place-in-premiere
`);
}
