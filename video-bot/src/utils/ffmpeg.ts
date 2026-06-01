import { execSync, spawnSync } from 'child_process';

export function checkFFmpeg(): boolean {
  const result = spawnSync('ffmpeg', ['-version'], { stdio: 'pipe' });
  return result.status === 0;
}

export function getVideoDuration(filePath: string): number {
  const result = execSync(
    `ffprobe -v quiet -print_format json -show_format "${filePath}"`,
    { encoding: 'utf8' }
  );
  const info = JSON.parse(result);
  return parseFloat(info.format.duration);
}

export interface TimeSegment {
  start: number;
  end: number;
}

// Builds an FFmpeg concat filter from a list of keep segments
export function buildConcatFilter(segments: TimeSegment[]): { filterComplex: string; inputs: string } {
  const parts = segments.map((seg, i) => {
    const dur = seg.end - seg.start;
    return `[0:v]trim=start=${seg.start.toFixed(3)}:duration=${dur.toFixed(3)},setpts=PTS-STARTPTS[v${i}];\n` +
           `[0:a]atrim=start=${seg.start.toFixed(3)}:duration=${dur.toFixed(3)},asetpts=PTS-STARTPTS[a${i}]`;
  });
  const vParts = segments.map((_, i) => `[v${i}]`).join('');
  const aParts = segments.map((_, i) => `[a${i}]`).join('');
  const concat = `${vParts}concat=n=${segments.length}:v=1:a=0[vout];\n${aParts}concat=n=${segments.length}:v=0:a=1[aout]`;

  return {
    filterComplex: [...parts, concat].join(';\n'),
    inputs: '-map "[vout]" -map "[aout]"',
  };
}

// Reformat 16:9 input to 9:16 with face in bottom half, black top half
export function build916Filter(): string {
  return [
    // Crop centre of frame (handles most talking head setups)
    `[0:v]crop=in_h*(9/16):in_h,scale=1080:960[face]`,
    // Black top half
    `color=black:s=1080x960:r=30[top]`,
    // Stack: top (animations zone) above face
    `[top][face]vstack=inputs=2[out]`,
  ].join(';\n');
}

export function run(cmd: string): void {
  console.log(`\n$ ${cmd}\n`);
  execSync(cmd, { stdio: 'inherit' });
}
