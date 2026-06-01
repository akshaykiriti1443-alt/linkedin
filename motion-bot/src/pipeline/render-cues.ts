import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';

const CUES_DIR = path.resolve('src/compositions/cues');
const OUT_DIR = path.resolve('out/cues');
const BROWSER = '/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell';

export interface RenderedCue {
  id: string;
  file: string;
  startTime: number;
  duration: number;
}

function loadCueMeta(cueFile: string): { startTime: number; duration: number } | null {
  const content = fs.readFileSync(cueFile, 'utf8');
  const startMatch = content.match(/\/\/ META_START:([\d.]+)/);
  const durMatch = content.match(/\/\/ META_DURATION:([\d.]+)/);
  if (!startMatch || !durMatch) return null;
  return { startTime: parseFloat(startMatch[1]), duration: parseFloat(durMatch[1]) };
}

export function renderAllCues(): RenderedCue[] {
  if (!fs.existsSync(CUES_DIR)) {
    console.log('No cues directory found. Generate compositions first.');
    return [];
  }

  const cueFiles = fs.readdirSync(CUES_DIR).filter(f => f.endsWith('.tsx')).sort();
  if (cueFiles.length === 0) {
    console.log('No cue compositions found in src/compositions/cues/');
    return [];
  }

  fs.mkdirSync(OUT_DIR, { recursive: true });

  // Re-generate Root.tsx to register all cues
  generateRoot(cueFiles);

  const results: RenderedCue[] = [];

  for (const file of cueFiles) {
    const id = file.replace('.tsx', '');
    const compId = id.replace(/[-]/g, '_');
    const outFile = path.join(OUT_DIR, `${id}.mov`);
    const meta = loadCueMeta(path.join(CUES_DIR, file));

    console.log(`\n🎬 Rendering ${id}...`);

    try {
      const browserFlag = fs.existsSync(BROWSER)
        ? `--browser-executable="${BROWSER}"`
        : '';

      execSync(
        `npx remotion render src/index.ts ${compId} "${outFile}" --codec=prores --prores-profile=4444 ${browserFlag}`,
        { stdio: 'inherit' }
      );

      results.push({
        id,
        file: outFile,
        startTime: meta?.startTime ?? 0,
        duration: meta?.duration ?? 3,
      });
      console.log(`✅ ${id} → ${outFile}`);
    } catch (e) {
      console.error(`❌ Failed to render ${id}:`, e);
    }
  }

  return results;
}

function generateRoot(cueFiles: string[]) {
  const imports = cueFiles.map((f, i) => {
    const id = f.replace('.tsx', '');
    const compId = id.replace(/[-]/g, '_');
    return `import ${compId} from './compositions/cues/${id}';`;
  }).join('\n');

  const compositions = cueFiles.map((f) => {
    const id = f.replace('.tsx', '');
    const compId = id.replace(/[-]/g, '_');
    const content = fs.readFileSync(path.join(CUES_DIR, f), 'utf8');
    const framesMatch = content.match(/durationInFrames[^\d]*(\d+)/);
    const frames = framesMatch ? framesMatch[1] : '90';
    return `  <Composition id="${compId}" component={${compId}} durationInFrames={${frames}} fps={30} width={1920} height={1080} />`;
  }).join('\n');

  const root = `import React from 'react';
import { Composition } from 'remotion';
import AIAutomation from './compositions/AIAutomation';
${imports}

export const RemotionRoot: React.FC = () => (
  <>
    <Composition id="AIAutomation" component={AIAutomation} durationInFrames={240} fps={30} width={1920} height={1080} />
${compositions}
  </>
);
`;

  fs.writeFileSync(path.resolve('src/Root.tsx'), root, 'utf8');
}
