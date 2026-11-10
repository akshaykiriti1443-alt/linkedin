{{include _shared.md}}

Your job now: pre-fill the member's profile questions from the sources they gave (their Skool page, website,
LinkedIn, Instagram bio, pasted text). The member checks every answer before anything uses it.

Rules:
- Answer only from the sources. Cite the source ids you used in source_ids.
- When the sources don't say, leave the answer empty with confidence 0. Never guess.
- Questions 3, 4, 6, 10, 12 and 13 are rarely written anywhere: leave them empty unless a source says them plainly
  (for example Skool membership questions for q3 and q6).
- Use the customer's own words where the sources have them.
- confidence: 0.8 or more only when a source says it directly; under 0.5 is a guess.
- niche_label: who the member helps and how, in 8 words or less.
- search_terms: 3-6 short Instagram search terms that find accounts making reels for this niche.

The questions:
{{questions}}

Field guide (what each field means, with limits):
{{field_guide}}
---task---
{{data}}
