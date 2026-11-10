"""Profile importers (DESIGN-SAAS.md 9.2). Owner: WP7.

- website.fetch_site(url)            homepage + up to 4 same-site pages -> markdown text (trafilatura, SSRF-safe)
- skool.parse_next_data(next_data)   Skool group About / @profile -> {"text", "meta"} (server fetch or page_read)
- skool.parse_html(html), skool.fetch_page(url)
- linkedin.clean_page(text), linkedin.pdf_text(data), linkedin.clean_paste(text)
- instagram.save_ig_bio(user_id, source_id, ig_user)   called by WP2 after an ig_profile (member_bio) job
- merge.QUESTIONS, merge.build_digest(...), merge.draft_sources(...)

Every importer returns plain text capped at TEXT_MAX characters. The text is untrusted third-party data: prompts
wrap it in <untrusted> blocks (ai/untrusted.py, 7.6); importers never interpret it.
"""
from __future__ import annotations

TEXT_MAX = 20_000
