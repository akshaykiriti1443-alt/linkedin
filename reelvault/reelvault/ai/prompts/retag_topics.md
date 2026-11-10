{{include _shared.md}}

Your job now: sort reels into the member's topics. Only topics change; everything else about the reels stays.

Rules:
- A topic describes the SUBJECT the reel SHOWS (use what is on screen). A reel that shows design work gets the
  design topic even if the voice talks about sales.
- Use only slugs from the list below, 0-3 per reel. When none fits, return [] (Unsorted) and, if useful, one
  new_topic label. Never squeeze a reel into the nearest topic.
- Return exactly one entry per reel id, each id once, copied exactly.

The member's active topics (slug: what it covers):
{{topics}}

Field guide (what each field means, with limits):
{{field_guide}}
---task---
{{data}}
