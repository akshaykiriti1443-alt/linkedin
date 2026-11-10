{{include _shared.md}}

Your job now: tag a batch of competitor reels by what is SEEN, so ReelVault can learn which formats work.

How to tag each reel:
- Look at its contact sheet. Tiles are numbered in reading order; the text maps each tile number to its time.
- Visual format and recipe come from the frames and the VisualStats numbers, never from the script. Apply the
  precedence list below. The local format guess is a hint from measurements; trust the frames when they disagree.
- Topics describe the SUBJECT the reel SHOWS (use visual_subject). A reel that shows design work gets the design
  topic even if the voice talks about sales. Use only slugs from the member's active topic list. When none fits,
  return topics [] (Unsorted) and, if useful, one new_topic label. Never squeeze a reel into the nearest topic.
- Sound: confirm or override the local pattern hints using the sound summary and the transcript.
- hook_line: the first spoken sentence or the on-screen hook text, as heard or seen.
- Return exactly one tag per reel id in the batch, each id once, copied exactly.

{{taxonomy_vf}}

{{taxonomy_sd}}

{{taxonomy_lists}}

The member's active topics (slug: what it covers):
{{topics}}

Field guide (what each field means, with limits):
{{field_guide}}
---task---
{{data}}
