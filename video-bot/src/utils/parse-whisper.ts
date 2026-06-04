export interface WhisperWord {
  word: string;
  start: number;
  end: number;
}

export interface WhisperSegment {
  id: number;
  start: number;
  end: number;
  text: string;
  words?: WhisperWord[];
}

export interface WhisperOutput {
  text: string;
  segments: WhisperSegment[];
  language: string;
}

export interface Silence {
  start: number;
  end: number;
  duration: number;
}

export function parseWhisper(json: WhisperOutput): WhisperWord[] {
  const words: WhisperWord[] = [];
  for (const seg of json.segments) {
    if (seg.words) {
      for (const w of seg.words) {
        words.push({ word: w.word.trim(), start: w.start, end: w.end });
      }
    }
  }
  return words;
}

export function detectSilences(words: WhisperWord[], threshold = 0.5): Silence[] {
  const silences: Silence[] = [];
  for (let i = 1; i < words.length; i++) {
    const gap = words[i].start - words[i - 1].end;
    if (gap > threshold) {
      silences.push({ start: words[i - 1].end, end: words[i].start, duration: gap });
    }
  }
  return silences;
}

export function toSRT(segments: WhisperSegment[]): string {
  return segments.map((seg, i) => {
    const fmt = (s: number) => {
      const h = Math.floor(s / 3600).toString().padStart(2, '0');
      const m = Math.floor((s % 3600) / 60).toString().padStart(2, '0');
      const sec = Math.floor(s % 60).toString().padStart(2, '0');
      const ms = Math.round((s % 1) * 1000).toString().padStart(3, '0');
      return `${h}:${m}:${sec},${ms}`;
    };
    return `${i + 1}\n${fmt(seg.start)} --> ${fmt(seg.end)}\n${seg.text.trim()}\n`;
  }).join('\n');
}

export function toPlainText(segments: WhisperSegment[]): string {
  return segments.map(s => s.text.trim()).join(' ');
}
