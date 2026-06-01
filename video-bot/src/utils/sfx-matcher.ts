import path from 'path';
import type { WhisperWord } from './parse-whisper.js';

export interface SFXCue {
  time: number;   // seconds in the final timeline
  file: string;   // path to SFX file
  volume: number; // dB, e.g. -18
}

const TRANSITION_WORDS = ['next', 'now', 'so', 'alright', 'okay', 'moving', 'finally', 'first', 'second', 'third', 'last', 'here'];
const CLICK_WORDS = ['click', 'clicking', 'tap', 'open', 'launch', 'select', 'choose', 'press', 'hit', 'button'];
const TYPING_WORDS = ['type', 'typing', 'write', 'code', 'command', 'terminal', 'run', 'install', 'npm', 'git'];

const SFX_DIR = path.resolve('assets/sfx');

export function matchSFX(words: WhisperWord[]): SFXCue[] {
  const cues: SFXCue[] = [];
  const lastCueTime: Record<string, number> = {};
  const MIN_GAP = 3; // minimum seconds between same SFX type

  for (const word of words) {
    const w = word.word.toLowerCase().replace(/[^a-z]/g, '');

    const check = (keywords: string[], sfxFile: string) => {
      if (!keywords.includes(w)) return;
      const last = lastCueTime[sfxFile] ?? -999;
      if (word.start - last < MIN_GAP) return;
      cues.push({ time: word.start, file: path.join(SFX_DIR, sfxFile), volume: -18 });
      lastCueTime[sfxFile] = word.start;
    };

    check(TRANSITION_WORDS, 'whoosh.mp3');
    check(CLICK_WORDS, 'click.mp3');
    check(TYPING_WORDS, 'typing.mp3');
  }

  return cues;
}

// Builds FFmpeg adelay+amix filter for SFX cues
export function buildSFXFilter(cues: SFXCue[], inputOffset = 1): string {
  if (cues.length === 0) return '';

  const parts = cues.map((cue, i) => {
    const idx = inputOffset + i;
    const delayMs = Math.round(cue.time * 1000);
    return `[${idx}:a]volume=${cue.volume}dB,adelay=${delayMs}|${delayMs}[sfx${i}]`;
  });

  const sfxLabels = cues.map((_, i) => `[sfx${i}]`).join('');
  const mix = `[0:a]${sfxLabels}amix=inputs=${cues.length + 1}:duration=first:dropout_transition=0[aout]`;

  return [...parts, mix].join(';\n');
}
