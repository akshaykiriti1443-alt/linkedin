"""LinkedIn importer (DESIGN-SAAS.md 9.2). Owner: WP7.

LinkedIn is NEVER fetched by the server. Text arrives three ways:
- `page_read` of https://www.linkedin.com/in/me/ in the member's own browser (WP2 calls `clean_page`): the visible
  main text, whitespace already collapsed into one line by the extension;
- the profile PDF the member saves ("Me > View profile > Resources > Save to PDF") -> `pdf_text` (pypdf);
- a paste -> `clean_paste`.
"""
from __future__ import annotations

import io
import re

from . import TEXT_MAX

PDF_MAX = 5 * 1024 * 1024
SECTIONS = ("About", "Featured", "Activity", "Experience", "Education", "Licenses & certifications", "Skills",
            "Recommendations", "Projects", "Volunteering", "Honors & awards", "Languages", "Services",
            "Courses", "Publications", "Interests", "Top skills", "Summary", "Certifications")
# Buttons and screen-reader labels LinkedIn puts in <main>; none of them describe the member.
NOISE = [
    r"Skip to main content", r"\d+ notifications? total", r"Open to work", r"Add profile section", r"Enhance profile",
    r"Add custom button", r"Show all \d+ [a-z]+", r"Show all", r"…\s*see more", r"\.\.\.\s*see more", r"see more",
    r"see less", r"Show more", r"Show less", r"Edit intro", r"Endorse", r"Contact info", r"Private to you",
    r"Profile language", r"Public profile & URL", r"Who your viewers also viewed", r"People you may know",
    r"You might like", r"Pages for you", r"Add a button", r"Show credential", r"Show translation",
    r"Report this profile", r"Saved items", r"Talent solutions", r"Open to", r"Resources(?= About)",
]
NOISE_RE = re.compile(r"(?<![\w-])(?:" + "|".join(NOISE) + r")(?![\w-])", re.I)
SECTION_RE = re.compile(r"\s*(?<![\w&])(" + "|".join(re.escape(s) for s in SECTIONS) + r")(?:\s+\1)?(?![\w])\s*")


def _squash(text: str) -> str:
    text = text.replace(" ", " ").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _dedupe_phrases(text: str) -> str:
    # LinkedIn renders many labels twice (visible + visually hidden): "Senior ads buyer Senior ads buyer".
    return re.sub(r"\b((?:[\w&'’.,:/-]+ ){0,8}[\w&'’.,:/-]+) \1\b", r"\1", text)


def clean_page(text: str) -> dict:
    """The extension's one-line LinkedIn text -> {"text": readable text with a line per section, "meta"}."""
    t = _squash(text or "")
    t = NOISE_RE.sub(" ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    for _ in range(2):
        t = _dedupe_phrases(t)
    t = SECTION_RE.sub(lambda m: f"\n\n{m.group(1)}:\n", t)
    t = re.sub(r"\s*·\s*", " · ", t)
    t = _squash(t)
    found = [s for s in SECTIONS if re.search(rf"(^|\n){re.escape(s)}:\n", t)]
    return {"text": t[:TEXT_MAX], "meta": {"sections": found}}


def clean_paste(text: str) -> str:
    """Pasted text (any source): normalize whitespace, keep line breaks, cap at 20,000 characters."""
    return _squash(text or "")[:TEXT_MAX]


class PdfProblem(ValueError):
    """Plain-words reason a PDF can't be read."""


def pdf_text(data: bytes) -> str:
    """Text of the LinkedIn 'Save to PDF' file (or any text PDF), cleaned and capped. Raises PdfProblem."""
    if not data or not data.startswith(b"%PDF"):
        raise PdfProblem("That file is not a PDF. On LinkedIn, open your profile, click Resources, then Save to PDF.")
    if len(data) > PDF_MAX:
        raise PdfProblem("That PDF is bigger than 5 MB.")
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                raise PdfProblem("That PDF has a password. Save it again without one.") from None
        parts = []
        total = 0
        for page in reader.pages[:30]:
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total > TEXT_MAX * 2:
                break
    except PdfProblem:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, OSError):
        raise PdfProblem("We couldn't read that PDF. Paste your LinkedIn About and Experience instead.") from None
    text = _squash("\n".join(parts))
    # LinkedIn's PDF ends every page with "Page 1 of 3".
    text = re.sub(r"\n?Page \d+ of \d+\n?", "\n", text)
    text = _squash(text)
    if len(text) < 40:
        raise PdfProblem("That PDF has no text we can read. Paste your LinkedIn About and Experience instead.")
    return text[:TEXT_MAX]
