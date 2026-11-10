// Media pks are 19-digit numbers above 2^53 and Instagram sends some as JSON numbers; JSON.parse would round them
// silently (DESIGN-SAAS.md 5.5). Quote every 16+ digit integer that is the value of an object key BEFORE parsing.
// Key-anchored: inside a JSON string the quotes are escaped as \", so text in captions cannot match.
export const quoteBigInts = t => t.replace(/("[A-Za-z0-9_]+"\s*:\s*)(-?\d{16,})(?=\s*[,}\]])/g, '$1"$2"');

// The only way the extension turns an Instagram response into data. Never call a bare JSON.parse on one.
export function parseIg(text) {
  return JSON.parse(quoteBigInts(text));
}
