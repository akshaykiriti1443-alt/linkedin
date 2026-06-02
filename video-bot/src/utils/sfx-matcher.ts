import fs from 'fs';
import path from 'path';
import type { WhisperWord } from './parse-whisper.js';

export interface SFXEvent {
  sfx: string;    // filename stem e.g. "digital_readout"
  at_ms: number;  // milliseconds into final timeline
  reason?: string;
}

// Available SFX stems (must match sfx/*.wav)
export const SFX_LIBRARY = [
  'whoosh',          // transitions, scene changes
  'swoosh_down',     // endings, dismissals
  'impact',          // big reveals, dramatic moments
  'ding',            // success, key points confirmed
  'keyboard',        // typing, code, commands
  'mouse_click',     // UI clicks, selecting items
  'double_click',    // section starts, new topics
  'notification',    // tool names, alerts, pings
  'camera_shutter',  // screenshots, results shown
  'riser',           // build-up before a reveal
  'air_hit',         // punchy intros, action verbs
  'digital_readout', // stats, numbers, tech terms (primary)
];

/**
 * Writes workspace/sfx_events_draft.json with word-level context for Claude to fill in.
 * Claude reads this draft, picks 35–50 placements, and writes sfx_events.json.
 * build_sfx_track.py then reads sfx_events.json and mixes the WAV.
 */
export function writeSFXDraft(words: WhisperWord[], outputPath: string): void {
  const draft = {
    instructions: [
      'Fill the events array with 35–50 SFX placements.',
      'Use digital_readout for numbers, stats, and tech terms (max 1 per 7s).',
      'Use whoosh for transitions between topics.',
      'Use impact for dramatic reveals or strong claims.',
      'Min 1.5s gap between any two events.',
      'at_ms = word.start * 1000 (milliseconds).',
      `Available SFX: ${SFX_LIBRARY.join(', ')}`,
    ],
    transcript_words: words.map(w => ({
      word: w.word,
      start_ms: Math.round(w.start * 1000),
      end_ms:   Math.round(w.end   * 1000),
    })),
    events: [] as SFXEvent[],
  };

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, JSON.stringify(draft, null, 2));
}

/**
 * Reads workspace/sfx_events.json (Claude-filled). Returns events for build-timeline.
 */
export function loadSFXEvents(eventsPath: string): SFXEvent[] {
  if (!fs.existsSync(eventsPath)) return [];
  const data = JSON.parse(fs.readFileSync(eventsPath, 'utf8'));
  // Support both raw array and {events:[...]} shape
  return Array.isArray(data) ? data : (data.events ?? []);
}
