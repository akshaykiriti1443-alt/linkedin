import type { WhisperWord } from './parse-whisper.js';

export interface Retake {
  original: { start: number; end: number; text: string };
  retake: { start: number; end: number; text: string };
  keepStart: number; // timestamp to resume from (after the retake)
}

// Finds repeated n-gram sequences — a simple heuristic for retake detection.
// Claude Code can also inspect the transcript text directly for higher accuracy.
export function detectRetakes(words: WhisperWord[], minMatchWords = 3): Retake[] {
  const retakes: Retake[] = [];
  const n = minMatchWords;

  for (let i = 0; i < words.length - n; i++) {
    const phrase = words.slice(i, i + n).map(w => w.word.toLowerCase().replace(/[^a-z]/g, '')).join(' ');

    for (let j = i + 1; j < words.length - n; j++) {
      const candidate = words.slice(j, j + n).map(w => w.word.toLowerCase().replace(/[^a-z]/g, '')).join(' ');

      if (phrase === candidate) {
        // Found a repeat — the earlier occurrence (i) is the flub, j is the clean take
        const originalEnd = words[i + n - 1].end;
        const retakeStart = words[j].start;

        // Only flag if the repeat is within 30 seconds (likely a correction, not a true repeat)
        if (retakeStart - originalEnd < 30) {
          retakes.push({
            original: {
              start: words[i].start,
              end: originalEnd,
              text: words.slice(i, i + n).map(w => w.word).join(' '),
            },
            retake: {
              start: retakeStart,
              end: words[j + n - 1].end,
              text: words.slice(j, j + n).map(w => w.word).join(' '),
            },
            keepStart: retakeStart,
          });
        }
        break;
      }
    }
  }

  return retakes;
}
