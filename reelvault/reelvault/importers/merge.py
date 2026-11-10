"""Questions, answers and the ProfileDigest (DESIGN-SAAS.md 9.1, 9.3, 9.4). Owner: WP7.

- QUESTIONS: the questionnaire with its exact copy (ids are stable), in the `Question` shape of 4.4.
- normalize_answer(qid, incoming, old): validates a member's edit and returns the stored `Answer`.
- build_digest(answers, ...): approve builds the ProfileDigest deterministically from the answers (no AI call).
- draft_sources(rows): sources -> `{source_id, kind, url, via, fetched_at, text}` for the draft_profile task.
"""
from __future__ import annotations

import re
from typing import Any

REQUIRED = ("q1", "q3", "q7", "q12")
ONBOARDING = ("q1", "q3", "q7", "q12")          # the 4 quick questions of onboarding step 4
LIST_QUESTIONS = ("q3", "q6", "q14")
TONES = [("direct", "Direct"), ("friendly", "Friendly"), ("funny", "Funny"), ("calm", "Calm"), ("bold", "Bold")]
MAKES = [("on_camera", "Me on camera"), ("voice_only", "My voice only (no face)"),
         ("screen_recording", "Screen recordings"), ("film_broll", "Film B-roll"),
         ("hands_demo", "My hands showing something"), ("second_person", "A second person on camera"),
         ("go_on_location", "Film on location")]

QUESTIONS: list[dict] = [
    {"id": "q1", "label": "Who do you help?",
     "help": 'Finish this: "I help ___ get ___." Add size, location or industry if it matters.',
     "kind": "text", "required": True, "prefill": "Skool About, website, LinkedIn, Instagram bio"},
    {"id": "q2", "label": "Your best client so far",
     "help": "Who were they, what was going wrong before you, and what changed after?",
     "kind": "long", "required": False, "prefill": "website (case studies), LinkedIn"},
    {"id": "q3", "label": "Their top 3 problems, in their words",
     "help": "What do they say on a sales call or type into Google?",
     "kind": "list", "count": 3, "required": True, "prefill": "Skool membership questions (if you run a group)"},
    {"id": "q4", "label": "What have they already tried that didn't work?",
     "help": "Other agencies, doing it themselves, courses. Why did it fail?",
     "kind": "long", "required": False, "prefill": ""},
    {"id": "q5", "label": "What do they really want?",
     "help": "The dream result in 6-12 months (numbers if you can), and how it would feel.",
     "kind": "long", "required": False, "prefill": "Skool About"},
    {"id": "q6", "label": 'Why do people say "no" or "not now"?',
     "help": 'The top objections: price, trust, timing, "got burned before".',
     "kind": "list", "required": False, "prefill": "Skool membership questions"},
    {"id": "q7", "label": "What do you sell?",
     "help": "Main offer, what's included, how it's delivered, price or price range, any guarantee or contract terms.",
     "kind": "long", "required": True, "prefill": "Skool About, website"},
    {"id": "q8", "label": "Your proof",
     "help": "Your 3 best results (numbers, type of client, how long it took), plus testimonials you're allowed "
             "to show.",
     "kind": "long", "required": False, "prefill": "website, LinkedIn, Skool About"},
    {"id": "q9", "label": "What makes you different?",
     "help": "What do you believe that most people in your industry get wrong?",
     "kind": "long", "required": False, "prefill": "website, LinkedIn"},
    {"id": "q10", "label": "How do you talk?",
     "help": "Pick your tone. Add words you always use, words you never use, and 1-3 creators whose style you like.",
     "kind": "choice", "multi": True, "choices": [{"id": i, "label": lb} for i, lb in TONES], "text": True,
     "required": False, "prefill": ""},
    {"id": "q11", "label": "Where do you post, and what should a viewer do next?",
     "help": "Platforms, followers today, how often you post, your main call to action (book a call, DM a word, "
             "free guide, join my Skool) and the link.",
     "kind": "long", "required": False, "prefill": "Instagram bio, Skool profile links"},
    {"id": "q12", "label": "What can you actually make?",
     "help": "Tick what you can do, then add anything else.",
     "kind": "choice", "multi": True, "choices": [{"id": i, "label": lb} for i, lb in MAKES], "text": True,
     "extras": ["hours", "editor", "languages"], "required": True, "prefill": ""},
    {"id": "q13", "label": "Hard no's",
     "help": "Topics, claims or words to avoid (for example income claims, health or finance rules), and "
             "competitors not to name.",
     "kind": "long", "required": False, "prefill": ""},
    {"id": "q14", "label": "Who are your competitors or role models?",
     "help": "1-2 Instagram accounts whose reels work in your niche. We'll find the rest.",
     "kind": "list", "handles": True, "required": False, "prefill": ""},
]
QIDS = [q["id"] for q in QUESTIONS]
BY_ID = {q["id"]: q for q in QUESTIONS}
TEXT_LIMIT = 5000
ITEM_LIMIT = 500


def empty_answer() -> dict:
    return {"value": "", "source": "member", "confidence": 0, "confirmed": False}


def is_empty(a: Any) -> bool:
    """True when an Answer has no usable value (a suggestion alone does not count)."""
    if not isinstance(a, dict):
        return True
    v = a.get("value")
    if isinstance(v, list):
        return not any(isinstance(x, str) and x.strip() for x in v) and not (a.get("text") or "").strip()
    return not (isinstance(v, str) and v.strip())


def split_items(v: Any, limit: int = 20) -> list[str]:
    """A list answer or free text -> clean items ("- a\\n2) b; c" -> ["a", "b", "c"])."""
    if isinstance(v, list):
        raw = [x for x in v if isinstance(x, str)]
    elif isinstance(v, str):
        raw = re.split(r"\n|;|•", v)
    else:
        raw = []
    out = []
    for x in raw:
        x = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", x).strip()
        if x and x not in out:
            out.append(x[:ITEM_LIMIT])
    return out[:limit]


class BadAnswer(ValueError):
    """Plain-words reason an answer was refused."""


def _text(v: Any, qid: str) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise BadAnswer(f"Question {qid[1:]} needs text.")
    v = v.strip()
    if len(v) > TEXT_LIMIT:
        raise BadAnswer(f"Your answer to question {qid[1:]} is too long. Keep it under {TEXT_LIMIT:,} characters.")
    return v


def normalize_answer(qid: str, incoming: Any, old: dict | None = None) -> dict:
    """A member's edit of one question -> the stored Answer. `incoming` = {"value"?, "confirmed"?, "text"?,
    "hours"?, "editor"?, "languages"?}. A confirm-only edit (no value) keeps the AI value and its source."""
    q = BY_ID.get(qid)
    if q is None:
        raise BadAnswer("Unknown question.")
    if not isinstance(incoming, dict):
        raise BadAnswer(f"Question {qid[1:]}: send an object with a value.")
    old = dict(old or {})
    if "value" not in incoming:
        if incoming.get("confirmed") is True and not is_empty(old):
            old["confirmed"] = True
            return old
        if incoming.get("confirmed") is False:
            old["confirmed"] = False
            return old
        raise BadAnswer(f"Question {qid[1:]} has no value.")
    value = incoming.get("value")
    ans: dict[str, Any] = {"source": "member", "confidence": 1.0,
                           "confirmed": incoming.get("confirmed", True) is not False}
    kind = q["kind"]
    if kind == "list":
        items = split_items(value if isinstance(value, (list, str)) else "", 10)
        if q.get("handles"):
            from .instagram import normalize_handle

            hs = []
            for h in items:
                try:
                    hh = normalize_handle(h)
                except ValueError as e:
                    raise BadAnswer(f"{h[:40]}: {e}") from None
                if hh not in hs:
                    hs.append(hh)
            items = hs[:5]
        ans["value"] = items
    elif kind == "choice":
        allowed = {c["id"] for c in q["choices"]}
        picks = value if isinstance(value, list) else ([] if value in (None, "") else [value])
        bad = [p for p in picks if p not in allowed]
        if bad:
            raise BadAnswer(f"Question {qid[1:]}: unknown choice {str(bad[0])[:30]}.")
        ans["value"] = [c["id"] for c in q["choices"] if c["id"] in picks]
        ans["text"] = _text(incoming.get("text"), qid)
        if qid == "q12":
            hours = incoming.get("hours")
            if hours not in (None, ""):
                try:
                    hours = float(hours)
                except (TypeError, ValueError):
                    raise BadAnswer("Hours per week must be a number.") from None
                if not 0 <= hours <= 80:
                    raise BadAnswer("Hours per week must be between 0 and 80.")
                ans["hours"] = int(hours) if float(hours).is_integer() else round(hours, 1)
            else:
                ans["hours"] = None
            ed = incoming.get("editor")
            if ed not in (None, True, False):
                raise BadAnswer("Editor must be yes or no.")
            ans["editor"] = ed
            langs = incoming.get("languages") or []
            if isinstance(langs, str):
                langs = [x.strip() for x in re.split(r"[,;/]", langs) if x.strip()]
            if not isinstance(langs, list) or len(langs) > 6:
                raise BadAnswer("List up to 6 languages.")
            ans["languages"] = [str(x).strip()[:30] for x in langs if str(x).strip()]
    else:
        ans["value"] = _text(value, qid)
    return ans


def _val(answers: dict, qid: str) -> Any:
    a = answers.get(qid)
    return a.get("value") if isinstance(a, dict) else None


def _str(answers: dict, qid: str) -> str:
    v = _val(answers, qid)
    if isinstance(v, list):
        return "; ".join(x for x in v if isinstance(x, str))
    return (v or "").strip() if isinstance(v, str) else ""


CTA_RE = re.compile(r"(book (?:a|your) call|dm (?:me )?(?:the word )?\S+|comment \S+|free (?:guide|audit|training)|"
                    r"join (?:my|the) skool|link in bio|apply)[^.;\n]*", re.I)


def _voice(answers: dict) -> dict:
    a = answers.get("q10") if isinstance(answers.get("q10"), dict) else {}
    tone = [t for t in (a.get("value") or []) if isinstance(t, str)] if isinstance(a.get("value"), list) else []
    text = a.get("text") or (a.get("value") if isinstance(a.get("value"), str) else "") or ""
    always, never = [], []
    for line in re.split(r"\n|\.(?=\s)", text):
        m = re.match(r"\s*(?:always(?: say| use)?|words i use)\s*[:\-]\s*(.+)", line, re.I)
        n = re.match(r"\s*(?:never(?: say| use)?|words i never use|don'?t say)\s*[:\-]\s*(.+)", line, re.I)
        if m:
            always += [x.strip(" \"'") for x in m.group(1).split(",") if x.strip()]
        if n:
            never += [x.strip(" \"'") for x in n.group(1).split(",") if x.strip()]
    refs = list(dict.fromkeys(h.lower() for h in re.findall(r"@([A-Za-z0-9._]{1,30})", text)))[:3]
    out = {"tone": tone, "always": always[:10], "never": never[:10], "style_refs": [f"@{h}" for h in refs],
           "samples": []}
    if text and not (always or never):
        out["notes"] = text[:500]
    return out


def capabilities(answers: dict) -> dict:
    a = answers.get("q12") if isinstance(answers.get("q12"), dict) else {}
    picks = set(a.get("value") or []) if isinstance(a.get("value"), list) else set()
    langs = [x for x in a.get("languages") or [] if isinstance(x, str)] or ["en"]
    return {"on_camera": "on_camera" in picks, "voice": bool(picks & {"voice_only", "on_camera"}),
            "screen": "screen_recording" in picks, "broll": "film_broll" in picks, "hands": "hands_demo" in picks,
            "second_person": "second_person" in picks, "location": "go_on_location" in picks,
            "editor": bool(a.get("editor")), "hours_per_week": a.get("hours"), "languages": langs,
            **({"notes": a["text"][:300]} if a.get("text") else {})}


def _lang_code(langs: list[str]) -> str:
    first = (langs[0] if langs else "en").strip().lower()
    names = {"english": "en", "spanish": "es", "español": "es", "german": "de", "deutsch": "de", "french": "fr",
             "français": "fr", "portuguese": "pt", "italian": "it", "dutch": "nl", "arabic": "ar", "hindi": "hi"}
    if re.match(r"^[a-z]{2}$", first):
        return first
    return names.get(first, "en")


def build_digest(answers: dict, *, covered_topics: list[str] | None = None, samples: list[str] | None = None,
                 brand_id: str = "me") -> dict:
    """ProfileDigest (9.4) from the answers. Deterministic: the same answers always give the same digest."""
    draft = answers.get("_draft") if isinstance(answers.get("_draft"), dict) else {}
    who = _str(answers, "q1")
    caps = capabilities(answers)
    q11 = _str(answers, "q11")
    cta = CTA_RE.search(q11)
    niche = (draft.get("niche_label") or "").strip() or re.sub(r"^i help\s+", "", who, flags=re.I)[:60].strip()
    terms = [t for t in draft.get("search_terms") or [] if isinstance(t, str) and t.strip()][:8]
    voice = _voice(answers)
    voice["samples"] = [s for s in (samples or []) if isinstance(s, str) and s.strip()][:5]
    comp = [h for h in (_val(answers, "q14") or []) if isinstance(h, str)] if isinstance(_val(answers, "q14"),
                                                                                         list) else []
    return {
        "brand_id": brand_id,
        "niche_label": niche,
        "language": _lang_code(caps.get("languages") or []),
        "search_terms": terms,
        "who_we_help": who,
        "best_client": _str(answers, "q2"),
        "dream_outcome": _str(answers, "q5"),
        "top_pains": split_items(_val(answers, "q3"), 5),
        "failed_attempts": split_items(_val(answers, "q4"), 8),
        "objections": split_items(_val(answers, "q6"), 8),
        "offer": _str(answers, "q7"),
        "proof": split_items(_val(answers, "q8"), 8),
        "difference": _str(answers, "q9"),
        "voice": voice,
        "platforms": q11,
        "main_cta": cta.group(0).strip()[:160] if cta else "",
        "capabilities": caps,
        "covered_topics": list(dict.fromkeys(covered_topics or []))[:20],
        "hard_nos": split_items(_val(answers, "q13"), 12),
        "competitors": comp,
    }


def draft_sources(rows) -> list[dict]:
    """profile_sources rows (status ok) -> the shape every importer hands to draft_profile (9.2)."""
    out = []
    for s in rows:
        if getattr(s, "status", "ok") != "ok" or not (s.text or "").strip():
            continue
        out.append({"source_id": s.id, "kind": s.kind, "url": s.url, "via": s.via,
                    "fetched_at": s.fetched_at.isoformat() if s.fetched_at else None, "text": s.text})
    return out
