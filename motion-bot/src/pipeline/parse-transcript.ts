import fs from 'fs';

export interface Cue {
  index: number;
  startTime: number; // seconds
  endTime: number;   // seconds
  text: string;
}

function srtTimeToSeconds(t: string): number {
  // "00:01:23,456" → seconds
  const [hms, ms] = t.trim().split(',');
  const [h, m, s] = hms.split(':').map(Number);
  return h * 3600 + m * 60 + s + Number(ms) / 1000;
}

export function parseTranscript(filePath: string): Cue[] {
  const raw = fs.readFileSync(filePath, 'utf8').replace(/\r\n/g, '\n');
  const blocks = raw.trim().split(/\n\n+/);
  const cues: Cue[] = [];

  for (const block of blocks) {
    const lines = block.trim().split('\n');
    if (lines.length < 3) continue;
    const index = parseInt(lines[0], 10);
    const timeParts = lines[1].split(' --> ');
    if (timeParts.length !== 2) continue;
    const startTime = srtTimeToSeconds(timeParts[0]);
    const endTime = srtTimeToSeconds(timeParts[1]);
    const text = lines.slice(2).join(' ').replace(/<[^>]+>/g, '').trim();
    cues.push({ index, startTime, endTime, text });
  }

  return cues;
}

export function transcriptToText(cues: Cue[]): string {
  return cues.map(c => `[${c.startTime.toFixed(2)}s] ${c.text}`).join('\n');
}
