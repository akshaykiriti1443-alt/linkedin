"""Competitor aggregation: what works, with lift (DESIGN-SAAS.md 6.7, 6.9). WP4 owns this file.

Pure functions only (no database). The DB-reading wrapper is `server/jobs_score.py` (build_units, refresh).

    groups = sub_formats(units)                       # 6.9 grouping -> list[FormatGroup] (stable recipe keys)
    data = compute(units, formats={fmt_id: {...}})    # the Insights JSON of 6.7.5 (minus DB-only counts)
    can_make("VF01", digest["capabilities"]) -> {"ok": False, "reason": "Needs you on camera. You said voice only."}

A unit is a scored, tagged reel of a competitor account inside the window, with a weight w_r (1 for priority
reels, stratum size / tagged sample reels in the stratum for sample reels). Lift uses weighted counts; evidence uses
unweighted counts, so a heavily weighted sample reel can't fake it.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

# ---------------------------------------------------------------------------------------------- labels (section 10)

VF_LABELS = {
    "VF01": "Talking head", "VF02": "Talking head + B-roll", "VF03": "Voiceover + B-roll (faceless)",
    "VF04": "Green screen over image", "VF05": "Screen recording", "VF06": "Split screen / reaction",
    "VF07": "Before / after", "VF08": "Text on B-roll (no voice)", "VF09": "Slideshow", "VF10": "Skit / POV",
    "VF11": "Podcast / interview clip", "VF12": "Street interview", "VF13": "Hands-only tutorial",
    "VF14": "Whiteboard / notebook", "VF15": "List / countdown overlay", "VF16": "Day-in-the-life / vlog",
    "VF17": "Product demo / unboxing", "VF18": "UGC ad style / testimonial", "VF19": "Animation / motion graphics",
    "VF20": "Meme format", "other": "Other",
}
VF_NEEDS = {
    "VF01": ["on_camera"], "VF02": ["on_camera"], "VF03": ["broll", "voice"], "VF04": ["on_camera"],
    "VF05": ["screen"], "VF06": ["on_camera"], "VF07": [], "VF08": ["broll"], "VF09": [], "VF10": ["on_camera"],
    "VF11": ["on_camera"], "VF12": ["on_camera", "location", "second_person"], "VF13": ["hands"], "VF14": ["hands"],
    "VF15": [], "VF16": ["on_camera", "location"], "VF17": ["hands"], "VF18": ["on_camera"], "VF19": ["editor"],
    "VF20": [], "other": [],
}
SD_LABELS = {
    "SD01": "Voice only", "SD02": "Voice + steady music bed", "SD03": "Voice + ducked music",
    "SD04": "Music / trending audio only", "SD05": "Trending audio + voiceover", "SD06": "SFX on every cut",
    "SD07": "SFX on words or text", "SD08": "Riser into reveal", "SD09": "Silence beat before the punchline",
    "SD10": "Cuts on the beat", "SD11": "ASMR / foley", "SD12": "Natural location sound", "SD13": "Several voices",
    "SD14": "AI / TTS voice", "SD15": "Hit or music drop on the hook", "SD16": "Meme stings",
}
HOOK_LABELS = {
    "question": "Question", "bold_claim": "Bold claim", "curiosity_gap": "Curiosity gap", "pain_point": "Pain point",
    "stat_or_number": "Stat or number", "contrarian": "Contrarian", "story_open": "Story open",
    "mistake_callout": "Mistake callout", "how_to_promise": "How-to promise", "list_promise": "List promise",
    "before_after": "Before / after", "pattern_interrupt": "Pattern interrupt", "other": "Other hook",
}
CTA_LABELS = {
    "follow": "Follow", "comment_keyword": "Comment a keyword", "dm": "DM me", "link_in_bio": "Link in bio",
    "save": "Save this", "share": "Share this", "buy": "Buy", "watch_next": "Watch the next one",
    "none": "No call to action",
}
CAPTION_LABELS = {
    "word_by_word_highlight": "Word-by-word captions", "phrase_by_phrase": "Phrase-by-phrase captions",
    "full_subtitles": "Full subtitles", "keyword_only": "Keyword-only captions", "none": "No captions",
}
CAPTION_ABBR = {"word_by_word_highlight": "word", "phrase_by_phrase": "phrase", "full_subtitles": "full",
                "keyword_only": "keyword", "none": "none"}
BROLL_LABELS = {"none": "No B-roll", "light": "Light B-roll", "medium": "Some B-roll", "heavy": "Heavy B-roll"}
SETTING_LABELS = {
    "studio": "Studio", "desk": "Desk", "car": "Car", "walk_and_talk": "Walk and talk", "kitchen": "Kitchen",
    "stage": "Stage", "event": "Event", "outdoors": "Outdoors", "screen_only": "Screen only", "other": "Other setting",
}
FRAMING_LABELS = {"close": "Close-up", "medium": "Medium shot", "wide": "Wide shot", "none": "No person"}
PACE_LABELS = {"slow": "Slow cuts", "medium": "Medium cuts", "fast": "Fast cuts", "hyper": "Very fast cuts"}
LENGTH_LABELS = {"<15": "Under 15 s", "15-30": "15-30 s", "30-60": "30-60 s", "60-90": "60-90 s", "90+": "Over 90 s"}
NEED_WORDS = {"on_camera": "you on camera", "voice": "your voice", "screen": "screen recording",
              "broll": "B-roll footage", "hands": "your hands on camera", "second_person": "a second person",
              "location": "filming on location", "editor": "an editor"}
CAPABILITY_ALIASES = {"voice_only": "voice", "screen_recording": "screen", "film_broll": "broll",
                      "hands_demo": "hands", "go_on_location": "location"}

DIMENSIONS = ("visual_format", "sub_format", "headline_box", "caption_style", "face_bubble", "broll_share", "setting",
              "sound", "hook_type", "cta_type", "topic", "length", "cut_pace", "text_hook")
VISUAL_DIMENSIONS = ("visual_format", "sub_format", "headline_box", "caption_style", "face_bubble", "broll_share",
                     "setting")
BOOL_DIMENSIONS = ("headline_box", "face_bubble", "text_hook")
COMBO_KINDS = (("visual_format", "hook_type", "format×hook"), ("visual_format", "sound", "format×sound"),
               ("hook_type", "cta_type", "hook×cta"), ("sub_format", "hook_type", "sub_format×hook"))
EVIDENCE_WORD = {"strong": "Strong", "promising": "Promising", "thin": "Thin"}

SAVES_LINE = ("Instagram hides saves, and usually shares, for accounts you don't own. "
              "We rank on views, likes and comments.")


def sample_line(per_account: int) -> str:
    return (f"Normal reels are a random sample of {per_account} per account, weighted to stand for all of them.")


_TAX: dict | None = None


def _taxonomy() -> dict:
    """`needs` from reelvault/data/taxonomy.json (the source of truth, WP5); labels: the short built-in labels of
    6.7.5 ("Green screen over image"), taxonomy labels only for ids added later."""
    global _TAX
    if _TAX is None:
        vf_l, vf_n, sd_l = dict(VF_LABELS), {k: list(v) for k, v in VF_NEEDS.items()}, dict(SD_LABELS)
        try:
            from . import taxonomy

            data = taxonomy.load() or {}
            for it in data.get("visual_format") or []:
                if isinstance(it, dict) and it.get("id"):
                    if it.get("label") and it["id"] not in vf_l:  # short built-in labels win (6.7.5 copy)
                        vf_l[it["id"]] = it["label"]
                    if isinstance(it.get("needs"), list):
                        vf_n[it["id"]] = [n for n in it["needs"] if isinstance(n, str)]
            for it in data.get("sound_design") or []:
                if isinstance(it, dict) and it.get("id") and it.get("label") and it["id"] not in sd_l:
                    sd_l[it["id"]] = it["label"]
        except Exception:  # noqa: BLE001 - taxonomy is optional here
            pass
        _TAX = {"vf_labels": vf_l, "vf_needs": vf_n, "sd_labels": sd_l}
    return _TAX


def vf_label(vf: str | None) -> str:
    return _taxonomy()["vf_labels"].get(vf or "", vf or "Unknown format")


def vf_needs(vf: str | None) -> list[str]:
    return list(_taxonomy()["vf_needs"].get(vf or "", []))


def sd_label(sd: str | None) -> str:
    return _taxonomy()["sd_labels"].get(sd or "", sd or "Unknown sound")


def label(dimension: str, value: str, *, topic_labels: dict | None = None, formats: dict | None = None) -> str:
    """Plain label for a dimension value (LiftRow.label)."""
    if dimension == "visual_format":
        return vf_label(value)
    if dimension == "sub_format":
        f = (formats or {}).get(value) or {}
        return f.get("name") or fallback_name(f.get("vf"), f.get("recipe") or {})
    if dimension == "sound":
        return sd_label(value)
    if dimension == "hook_type":
        return HOOK_LABELS.get(value, value.replace("_", " ").capitalize())
    if dimension == "cta_type":
        return CTA_LABELS.get(value, value.replace("_", " ").capitalize())
    if dimension == "topic":
        if value == "unsorted":
            return "Unsorted"
        return (topic_labels or {}).get(value) or value.replace("-", " ").capitalize()
    if dimension == "caption_style":
        return CAPTION_LABELS.get(value, value)
    if dimension == "broll_share":
        return BROLL_LABELS.get(value, value)
    if dimension == "setting":
        return SETTING_LABELS.get(value, value)
    if dimension == "length":
        return LENGTH_LABELS.get(value, value)
    if dimension == "cut_pace":
        return PACE_LABELS.get(value, value)
    if dimension in BOOL_DIMENSIONS:
        what = {"headline_box": "Headline text box", "face_bubble": "Face bubble",
                "text_hook": "Text on screen in the first 2 s"}[dimension]
        return what if value == "yes" else f"No {what[0].lower()}{what[1:]}"
    return str(value)


def fallback_name(vf: str | None, recipe: dict) -> str:
    """A readable sub-format name from its recipe, until the AI names it (name_formats)."""
    base = vf_label(vf)
    setting = (recipe or {}).get("setting")
    if setting in ("desk", "car", "studio", "kitchen", "stage", "event", "outdoors") and vf in (
            "VF01", "VF02", "VF04", "VF11", "VF18"):
        base = f"{SETTING_LABELS[setting]} {base[0].lower()}{base[1:]}"
    elif setting == "walk_and_talk" and vf in ("VF01", "VF02"):
        base = f"Walk-and-talk {base[0].lower()}{base[1:]}"
    parts = [base]
    if (recipe or {}).get("headline_box") is True:
        parts.append("top headline box")
    if (recipe or {}).get("face_bubble") is True:
        parts.append("face bubble")
    cap = (recipe or {}).get("caption_style")
    if cap and cap != "none":
        parts.append(CAPTION_LABELS.get(cap, cap).lower())
    br = (recipe or {}).get("broll_share")
    if br in ("medium", "heavy") and vf not in ("VF02", "VF03", "VF08"):
        parts.append(BROLL_LABELS[br].lower())
    while len(" + ".join(parts)) > 70 and len(parts) > 1:  # FormatGuide.name is Short[70]: drop whole parts
        parts.pop()
    name = " + ".join(parts)
    return name if len(name) <= 70 else name[:70].rsplit(" ", 1)[0]


def length_bucket(duration: float | None) -> str | None:
    if duration is None:
        return None
    d = float(duration)
    if d < 15:
        return "<15"
    if d < 30:
        return "15-30"
    if d < 60:
        return "30-60"
    if d < 90:
        return "60-90"
    return "90+"


# ---------------------------------------------------------------------------------------------- can the member make it

def _caps(capabilities: dict | None) -> dict | None:
    if not isinstance(capabilities, dict) or not capabilities:
        return None
    out = {}
    for k, v in capabilities.items():
        out[CAPABILITY_ALIASES.get(k, k)] = v
    return out


def can_make(vf: str | None, capabilities: dict | None) -> dict:
    """Checks a visual format's `needs` against ProfileDigest.capabilities (q12). Unknown capabilities = ok."""
    caps = _caps(capabilities)
    if caps is None or not vf:
        return {"ok": True, "reason": None}
    missing = [n for n in vf_needs(vf) if not caps.get(n)]
    if not missing:
        return {"ok": True, "reason": None}
    words = [NEED_WORDS.get(n, n) for n in missing]
    need = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    reason = f"Needs {need}."
    if "on_camera" in missing and caps.get("voice"):
        reason += " You said voice only."
    elif "editor" in missing:
        reason += " You said you have no editor."
    return {"ok": False, "reason": reason}


# ---------------------------------------------------------------------------------------------- units

@dataclass
class Unit:
    """One reel as insights sees it. `tags` holds EFFECTIVE values (member override ?? deep ?? quick)."""

    reel_id: str
    account_id: str
    handle: str = ""
    score: float | None = None          # O
    weight: float = 1.0                 # w_r
    tier: str = "priority"
    taken_at: datetime | None = None
    is_self: bool = False
    is_collab: bool = False
    is_top10: bool = False
    tagged: bool = True
    visual_ok: bool = True              # tag_quality vision/member, or measured that agrees with local_vf p >= 0.8
    tags: dict = field(default_factory=dict)
    duration: float | None = None
    url: str | None = None
    thumb: str | None = None
    audio: dict = field(default_factory=dict)

    @property
    def log2(self) -> float | None:
        return math.log2(self.score) if self.score is not None and self.score > 0 else None


@dataclass
class FormatGroup:
    recipe_key: str
    vf: str
    recipe: dict                        # the kept recipe fields (FormatRecipe subset)
    reel_ids: list[str]
    n: int
    k: int
    hits: int
    example_reels: list[str]            # 3-6 outliers, best first
    dropped: list[str]                  # recipe fields merged away (most specific first)

    def to_dict(self) -> dict:
        return asdict(self)


def _yn(v) -> str | None:
    if v is None:
        return None
    return "yes" if bool(v) else "no"


def values_of(u: Unit, dim: str) -> list[str]:
    """Dimension values of a unit ([] = not counted in that dimension)."""
    t = u.tags or {}
    if dim in VISUAL_DIMENSIONS and not u.visual_ok:
        return []
    if dim == "visual_format":
        v = t.get("visual_format")
        return [v] if v else []
    if dim == "sub_format":
        v = t.get("sub_format")
        return [v] if v else []
    if dim in ("headline_box", "face_bubble"):
        v = _yn(t.get(dim))
        return [v] if v else []
    if dim in ("caption_style", "broll_share", "setting"):
        v = t.get(dim)
        return [v] if v else []
    if dim == "sound":
        v = t.get("sound") or t.get("sound_primary")
        return [v] if v else []
    if dim in ("hook_type", "cta_type", "cut_pace"):
        v = t.get(dim)
        return [v] if v else []
    if dim == "topic":
        tp = [x for x in (t.get("topics") or []) if isinstance(x, str) and x]
        return list(dict.fromkeys(tp)) or ["unsorted"]
    if dim == "length":
        v = t.get("length") or length_bucket(u.duration)
        return [v] if v else []
    if dim == "text_hook":
        v = _yn(t.get("text_hook", t.get("has_text_hook")))
        return [v] if v else []
    return []


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _wmean(pairs: list[tuple[float, float]]) -> float | None:
    sw = sum(w for _, w in pairs)
    if sw <= 0:
        return None
    return sum(x * w for x, w in pairs) / sw


def sample_weights(stratum_sizes: dict, tagged: dict) -> dict[str, float]:
    """w_r for tagged sample reels (6.7): stratum size / tagged sample reels in that stratum.
    stratum_sizes: {stratum: eligible non-priority reels}; tagged: {stratum: [reel_id, ...]}.
    Strata with no tagged reel yet hand their reels to the tagged strata of the same account (pro rata), so the
    sample still stands for every normal reel."""
    out: dict[str, float] = {}
    covered = 0
    for k, ids in tagged.items():
        if ids:
            w = (stratum_sizes.get(k) or len(ids)) / len(ids)
            for rid in ids:
                out[rid] = w
            covered += stratum_sizes.get(k) or len(ids)
    total = sum(stratum_sizes.values())
    if out and covered and total > covered:
        f = total / covered
        out = {k: v * f for k, v in out.items()}
    return {k: round(v, 6) for k, v in out.items()}


# ---------------------------------------------------------------------------------------------- sub-formats (6.9)

KEY_FIELDS = (("headline_box", "hb"), ("caption_style", "cap"), ("face_bubble", "fb"), ("broll_share", "br"),
              ("setting", "set"), ("framing", "fr"))
DROP_ORDER = ("framing", "setting", "broll_share", "face_bubble", "caption_style", "headline_box")


def _enc(fld: str, v) -> str:
    if v is None:
        return "?"
    if isinstance(v, bool):
        return "1" if v else "0"
    if fld == "caption_style":
        return CAPTION_ABBR.get(v, str(v))
    return str(v)


def recipe_key(vf: str, recipe: dict, dropped: Iterable[str] = ()) -> str:
    drop = set(dropped)
    parts = [vf]
    for fld, ab in KEY_FIELDS:
        if fld not in drop:
            parts.append(f"{ab}={_enc(fld, recipe.get(fld))}")
    return "|".join(parts)


def _is_hit(u: Unit, hit: float, any_hit: bool) -> bool:
    if any_hit:
        return u.score is not None and u.score >= hit
    return bool(u.is_top10)


def sub_formats(units: list[Unit], *, hit: float = 3.0, min_units: int = 3) -> list[FormatGroup]:
    """Group competitor reels by format recipe (6.9). Deterministic: same units -> same groups and keys.

    Start with the full key (VF + headline_box, caption_style, face_bubble, broll_share, setting, framing). Groups
    with >= 3 units containing >= 1 outlier are returned (each unit is in at most one group); every other group
    (< 3 units, or no outlier) merges into its parent by dropping fields in DROP_ORDER."""
    pool = [u for u in units if not u.is_self and not u.is_collab and u.tagged and u.visual_ok
            and (u.tags or {}).get("visual_format") and u.weight > 0]
    pool.sort(key=lambda u: u.reel_id)
    any_hit = any(u.score is not None and u.score >= hit for u in pool)
    left = pool
    out: list[FormatGroup] = []
    for level in range(len(DROP_ORDER) + 1):
        dropped = DROP_ORDER[:level]
        groups: dict[str, list[Unit]] = {}
        for u in left:
            key = recipe_key(u.tags["visual_format"], u.tags, dropped)
            groups.setdefault(key, []).append(u)
        nxt = []
        for key in sorted(groups):
            g = groups[key]
            hits = [u for u in g if _is_hit(u, hit, any_hit)]
            if len(g) >= min_units and hits:
                vf = g[0].tags["visual_format"]
                recipe = {}
                for fld, _ in KEY_FIELDS:
                    if fld in dropped:
                        continue
                    recipe[fld] = g[0].tags.get(fld)
                ex = sorted(hits, key=lambda u: (-(u.score or 0), u.reel_id))[:6]
                if len(ex) < 3:
                    more = [u for u in sorted(g, key=lambda u: (-(u.score or 0), u.reel_id)) if u not in ex]
                    ex += more[:3 - len(ex)]
                out.append(FormatGroup(recipe_key=key, vf=vf, recipe=recipe, reel_ids=sorted(u.reel_id for u in g),
                                       n=len(g), k=len({u.account_id for u in g}), hits=len(hits),
                                       example_reels=[u.reel_id for u in ex], dropped=list(dropped)))
            else:
                # Not a format row (too small, or no outlier): the units go back into the pool for the parent level,
                # so look-alike normal reels can never hide a sibling group's outliers (fix round 2).
                nxt.extend(g)
        left = sorted(nxt, key=lambda u: u.reel_id)
        if not left:
            break
    return out


# ---------------------------------------------------------------------------------------------- lift core (6.7)

def _evidence(n: int, k: int) -> str:
    if n >= 10 and k >= 3:
        return "strong"
    if n >= 5 and k >= 2:
        return "promising"
    return "thin"


def _lift_table(units: list[Unit], dim: str, *, hit: float, alpha: float, any_hit: bool) -> tuple[dict, dict]:
    """Per value: weighted shrunk lift, within-creator typical, evidence. Returns (rows by value, base)."""
    pairs = [(u, values_of(u, dim)) for u in units]
    if dim == "sub_format":
        # Only reels in a named group carry a sub_format. The base rate must still be ALL visual-ok reels (the
        # visual_format base), else a group holding every outlier reads 1.0x (fix round 1). Ungrouped reels count
        # in W/H0 and as "not this format" for each creator, but get no row.
        pairs = [(u, vs) for u, vs in pairs if vs or values_of(u, "visual_format")]
    else:
        pairs = [(u, vs) for u, vs in pairs if vs]
    du = [(u, vs) for u, vs in pairs if vs]
    W = sum(u.weight for u, _ in pairs)
    hw = sum(u.weight for u, _ in pairs if _is_hit(u, hit, any_hit))
    h0 = hw / W if W > 0 else 0.0
    base = {"W": W, "H0": h0, "n": len(pairs)}
    by_val: dict[str, list[Unit]] = {}
    for u, vs in du:
        for v in vs:
            by_val.setdefault(v, []).append(u)
    by_creator: dict[str, list[tuple[Unit, list[str]]]] = {}
    for u, vs in pairs:
        by_creator.setdefault(u.account_id, []).append((u, vs))
    rows = {}
    for v, mem in by_val.items():
        wt = sum(u.weight for u in mem)
        hits_w = sum(u.weight for u in mem if _is_hit(u, hit, any_hit))
        n = len(mem)
        k = len({u.account_id for u in mem})
        lift = None
        if h0 > 0:
            lift = ((hits_w + alpha * h0) / (wt + alpha)) / h0
        ds = []
        for c, items in by_creator.items():
            t_items = [(u.log2, u.weight) for u, vs in items if v in vs and u.log2 is not None]
            not_t = [u for u, vs in items if v not in vs]
            if len(t_items) >= 2 and len(not_t) >= 2:
                all_items = [(u.log2, u.weight) for u, _ in items if u.log2 is not None]
                a, b = _wmean(t_items), _wmean(all_items)
                if a is not None and b is not None:
                    ds.append(a - b)
        typical = 2 ** (sum(ds) / len(ds)) if ds else None
        best = sorted(mem, key=lambda u: (not _is_hit(u, hit, any_hit), -(u.score or 0), u.reel_id))[:3]
        rows[v] = {"value": v, "n": n, "k": k, "hits": sum(1 for u in mem if _is_hit(u, hit, any_hit)),
                   "hits_w": hits_w, "W_t": wt, "hit_rate": round(hits_w / wt, 3) if wt > 0 else 0.0,
                   "lift": round(lift, 2) if lift is not None else None,
                   "typical": round(typical, 2) if typical is not None else None,
                   "evidence": _evidence(n, k), "usage": round(wt / W, 3) if W > 0 else 0.0,
                   "best_reels": [u.reel_id for u in best], "_creators": len(ds)}
    return rows, base


def _grid(rows: list[dict]) -> dict:
    g = {"working_common": [], "opportunity": [], "overused": []}
    for r in rows:
        lift, u, ev, n, k = r.get("lift"), r["usage"], r["evidence"], r["n"], r["k"]
        if lift is None:
            continue
        if u >= 0.15 and lift >= 1.2 and ev != "thin":
            g["working_common"].append(r["value"])
        if u >= 0.15 and lift < 1.0:
            g["overused"].append(r["value"])
        if lift >= 1.5 and (k <= 2 or u < 0.10) and n >= 3:
            g["opportunity"].append(r["value"])
    return g


def _trend(lift: float | None, lift_recent: float | None, n_recent: int) -> str | None:
    if lift is None or lift_recent is None or n_recent < 5:
        return None
    if lift_recent >= 1.5 and lift_recent >= 1.3 * lift:
        return "rising"
    if lift >= 1.5 and lift_recent <= 0.7 * lift:
        return "fading"
    return None


def _in_window(u: Unit, now: datetime, days: int | None) -> bool:
    if not days:
        return True
    t = _aware(u.taken_at)
    return t is not None and t >= now - timedelta(days=days)


def _comp_units(units: Iterable[Unit]) -> list[Unit]:
    return [u for u in units if not u.is_self and not u.is_collab and u.tagged and u.weight > 0
            and u.score is not None]


# ---------------------------------------------------------------------------------------------- compute

def compute(units: list[Unit], *, hit: float = 3.0, alpha: int = 10, window_days: int | None = 365,
            recent_days: int = 90, now: datetime | None = None, formats: dict | None = None,
            capabilities: dict | None = None, topic_labels: dict | None = None, self_scored: int = 0,
            per_account: int = 25) -> dict:
    """The Insights JSON (6.7.5) for these units. `formats` = {fmt_id: {"name", "vf", "recipe"}} for sub-format
    labels and FormatCards; units carry tags["sub_format"] = fmt_id. `window_days` None/0 = all time."""
    now = _aware(now) or datetime.now(timezone.utc)
    formats = formats or {}
    all_comp = _comp_units(units)
    comp = [u for u in all_comp if _in_window(u, now, window_days)]
    recent = [u for u in comp if _in_window(u, now, recent_days)]
    any_hit = any(u.score is not None and u.score >= hit for u in comp)
    hit_rule = f"score >= {hit:g}" if any_hit or not comp else "top 10%"
    W = sum(u.weight for u in comp)
    H0 = (sum(u.weight for u in comp if _is_hit(u, hit, any_hit)) / W) if W > 0 else 0.0
    accounts = sorted({u.account_id for u in comp})

    rows: dict[str, list[dict]] = {}
    grid: dict[str, dict] = {}
    for dim in DIMENSIONS:
        table, _ = _lift_table(comp, dim, hit=hit, alpha=alpha, any_hit=any_hit)
        # The recent window uses the SAME hit rule as the full window (fix round 2): with no 3x reel lately, the
        # recent base rate is 0, lift_recent is None and no Rising/Fading chip is shown.
        rtable, _ = _lift_table(recent, dim, hit=hit, alpha=alpha, any_hit=any_hit)
        out = []
        for v, r in table.items():
            rr = rtable.get(v)
            lift_recent = rr["lift"] if rr else None
            n_recent = rr["n"] if rr else 0
            row = {"value": v, "label": label(dim, v, topic_labels=topic_labels, formats=formats),
                   "n": r["n"], "k": r["k"], "hits": r["hits"], "hit_rate": r["hit_rate"], "lift": r["lift"],
                   "typical": r["typical"], "evidence": r["evidence"], "usage": r["usage"],
                   "lift_recent": lift_recent, "n_recent": n_recent,
                   "trend": _trend(r["lift"], lift_recent, n_recent), "best_reels": r["best_reels"]}
            if dim == "visual_format" or dim == "sub_format":
                vf = v if dim == "visual_format" else (formats.get(v) or {}).get("vf")
                row["can_make"] = can_make(vf, capabilities)
            out.append(row)
        out.sort(key=lambda r: (r["lift"] is None, -(r["lift"] or 0), -r["n"], r["value"]))
        rows[dim] = out
        grid[dim] = _grid(out)

    combos = _combos(comp, hit=hit, alpha=alpha, any_hit=any_hit, formats=formats, topic_labels=topic_labels)
    by_id = {u.reel_id: u for u in comp}
    cards = _cards(rows, by_id, accounts=len(accounts), capabilities=capabilities, topic_labels=topic_labels,
                   formats=formats)
    fcards = format_cards(rows.get("sub_format", []), formats, by_id, capabilities=capabilities)
    per_comp = signatures(comp, hit=hit, any_hit=any_hit, formats=formats)
    yvt = you_vs_them(units, rows, now=now, window_days=window_days, self_scored=self_scored)
    n_unverified = sum(1 for u in comp if not u.visual_ok)
    older = sum(1 for u in all_comp if not _in_window(u, now, window_days))
    note = {"tagged": len(comp), "excluded": {"unverified_visual": n_unverified, "older_than_window": older},
            "saves_line": SAVES_LINE, "sample_line": sample_line(per_account)}
    if not any_hit and comp:
        note["hit_rule_line"] = ("None of these reels reached 3x normal yet, so we compare each account's top 10% "
                                 "with the rest.")
    return {"N": len(comp), "W": round(W, 3), "accounts": len(accounts), "H0": round(H0, 4), "hit_rule": hit_rule,
            "window_days": window_days or None, "rows": rows, "grid": grid, "combos": combos,
            "per_competitor": per_comp, "cards": cards, "formats": fcards, "you_vs_them": yvt, "music": [],
            "data_note": note}


def _combos(comp: list[Unit], *, hit: float, alpha: float, any_hit: bool, formats: dict,
            topic_labels: dict | None) -> list[dict]:
    out = []
    for da, db_, kind in COMBO_KINDS:
        both = []
        for u in comp:
            va, vb = values_of(u, da), values_of(u, db_)
            if va and vb:
                both.append((u, [(a, b) for a in va for b in vb]))
        if not both:
            continue
        W = sum(u.weight for u, _ in both)
        h0 = sum(u.weight for u, _ in both if _is_hit(u, hit, any_hit)) / W if W > 0 else 0.0
        pairs: dict[tuple, list[Unit]] = {}
        for u, ps in both:
            for p in ps:
                pairs.setdefault(p, []).append(u)
        for (a, b), mem in pairs.items():
            if len(mem) < 5 or not any(_is_hit(u, hit, any_hit) for u in mem) or h0 <= 0:
                continue
            wt = sum(u.weight for u in mem)
            hw = sum(u.weight for u in mem if _is_hit(u, hit, any_hit))
            lift = ((hw + alpha * h0) / (wt + alpha)) / h0
            out.append({"a": a, "b": b, "kind": kind, "n": len(mem), "lift": round(lift, 2),
                        "a_label": label(da, a, topic_labels=topic_labels, formats=formats),
                        "b_label": label(db_, b, topic_labels=topic_labels, formats=formats),
                        "hits": sum(1 for u in mem if _is_hit(u, hit, any_hit))})
    out.sort(key=lambda c: (-c["lift"], -c["n"], c["kind"], c["a"], c["b"]))
    return out[:20]


def _reel_ref(u: Unit) -> dict:
    return {"id": u.reel_id, "url": u.url, "thumb": u.thumb, "score": round(u.score, 1) if u.score else None}


def lift_phrase(lift: float) -> str:
    """'2.6x more likely to be an outlier', or for lift < 1 'Less likely to be an outlier (0.3x)'. Integration finding:
    a slideshow at 0.3x read '0.3x more likely to be an outlier' on Home."""
    if lift < 0.05:
        return "Rarely an outlier"
    if lift < 1:
        return f"Less likely to be an outlier ({lift:.1f}x)"
    return f"{lift:.1f}x more likely to be an outlier"


def card_line(lift: float, n: int, k: int, accounts: int, evidence: str, trend: str | None = None) -> str:
    who = f"{k} of your {accounts} competitors" if accounts > 1 else f"{k} competitor{'s' if k != 1 else ''}"
    line = (f"{lift:.1f}x more likely to be an outlier · {n} reels from {who} · "
            f"{EVIDENCE_WORD.get(evidence, evidence)} evidence")
    if trend:
        line += f" · {trend.capitalize()}"
    return line


def _cards(rows: dict, by_id: dict, *, accounts: int, capabilities, topic_labels, formats) -> list[dict]:
    cands = []
    for dim, rs in rows.items():
        if dim == "sub_format":
            continue
        for r in rs:
            if r["lift"] is None or r["lift"] < 1.5 or r["evidence"] not in ("strong", "promising"):
                continue
            if r["value"] == "unsorted" or (dim in BOOL_DIMENSIONS and r["value"] != "yes"):
                continue
            cm = can_make(r["value"], capabilities) if dim == "visual_format" else {"ok": True, "reason": None}
            cands.append((dim, r, cm))
    cands.sort(key=lambda x: (not x[2]["ok"], -x[1]["lift"], -x[1]["n"], x[0], x[1]["value"]))
    out = []
    for dim, r, cm in cands[:6]:
        out.append({"title": r["label"], "dimension": dim, "value": r["value"],
                    "line": card_line(r["lift"], r["n"], r["k"], accounts, r["evidence"]),
                    "lift": r["lift"], "evidence": r["evidence"], "trend": r["trend"], "can_make": cm,
                    "best_reels": [_reel_ref(by_id[x]) for x in r["best_reels"] if x in by_id]})
    return out


def format_cards(sub_rows: list[dict], formats: dict, by_id: dict, *, capabilities=None) -> list[dict]:
    """FormatCards (6.7.5) for every sub-format row: lift >= 1 first, then can-make, then lift."""
    out = []
    for r in sub_rows:
        f = formats.get(r["value"]) or {}
        vf = f.get("vf")
        cm = can_make(vf, capabilities)
        lift = r.get("lift")
        line = (lift_phrase(lift) + " · " if lift is not None else "") + \
            f"{r['n']} reels from {r['k']} competitor{'s' if r['k'] != 1 else ''}"
        if r.get("trend"):
            line += f" · {r['trend'].capitalize()}"
        ex_ids = [x for x in (f.get("example_reels") or []) if x in by_id] or r["best_reels"]
        out.append({"id": r["value"], "name": f.get("name") or fallback_name(vf, f.get("recipe") or {}),
                    "named": bool(f.get("name")), "vf": vf, "vf_label": vf_label(vf),
                    "recipe": f.get("recipe") or {}, "line": line, "lift": lift, "typical": r.get("typical"),
                    "evidence": r["evidence"], "trend": r.get("trend"), "can_make": cm, "n": r["n"], "k": r["k"],
                    "examples": [_reel_ref(by_id[x]) for x in ex_ids[:6] if x in by_id],
                    "href": f"#format/{r['value']}"})
    # Formats that beat normal first (fix round 2: can-make no longer lifts a 0.5x format above an 11x one), then
    # can-make, then lift.
    out.sort(key=lambda c: (c["lift"] is not None and c["lift"] < 1, not c["can_make"]["ok"], c["lift"] is None,
                            -(c["lift"] or 0), -c["n"], c["id"]))
    return out


def signatures(comp: list[Unit], *, hit: float, any_hit: bool, formats: dict) -> list[dict]:
    """Per-competitor signature (6.7 step 7)."""
    out = []
    by_acc: dict[str, list[Unit]] = {}
    for u in comp:
        by_acc.setdefault(u.account_id, []).append(u)
    for acc in sorted(by_acc):
        us = by_acc[acc]
        handle = us[0].handle

        def top_of(dim: str, pool: list[Unit]) -> tuple[str | None, float]:
            cnt: dict[str, float] = {}
            tot = 0.0
            for u in pool:
                vs = values_of(u, dim)
                if not vs:
                    continue
                tot += u.weight
                for v in vs:
                    cnt[v] = cnt.get(v, 0.0) + u.weight
            if not cnt or tot <= 0:
                return None, 0.0
            v = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            return v[0], v[1] / tot

        vf, share = top_of("visual_format", us)
        typ = None
        if vf:
            t_items = [(u.log2, u.weight) for u in us if vf in values_of(u, "visual_format") and u.log2 is not None]
            all_items = [(u.log2, u.weight) for u in us if values_of(u, "visual_format") and u.log2 is not None]
            if len(t_items) >= 2 and len(all_items) - len(t_items) >= 2:
                a, b = _wmean(t_items), _wmean(all_items)
                if a is not None and b is not None:
                    typ = round(2 ** (a - b), 2)
        sub, _ = top_of("sub_format", us)
        hooks: dict[str, list[float]] = {}
        for u in us:
            for v in values_of(u, "hook_type"):
                if u.log2 is not None:
                    hooks.setdefault(v, []).append(u.log2)
        best_hook = None
        good = [(st.mean(x), k) for k, x in hooks.items() if len(x) >= 3]
        if good:
            best_hook = sorted(good, key=lambda t: (-t[0], t[1]))[0][1]
        durs = [u.duration for u in us if u.duration]
        pace, _ = top_of("cut_pace", us)
        sound, _ = top_of("sound", us)
        hit_units = [u for u in us if _is_hit(u, hit, any_hit)]
        hit_sound, _ = top_of("sound", hit_units)
        tracks: dict[tuple, int] = {}
        for u in us:
            a = u.audio or {}
            if a.get("type") == "licensed" and a.get("title"):
                key = (a.get("title"), a.get("artist"))
                tracks[key] = tracks.get(key, 0) + 1
        top_tracks = [{"title": t, "artist": ar} for (t, ar), _ in
                      sorted(tracks.items(), key=lambda kv: (-kv[1], str(kv[0])))[:3]]
        line = f"@{handle}"
        if vf:
            line += f" mostly posts {vf_label(vf)} ({round(share * 100)}%)."
        else:
            line += " has no tagged format yet."
        if hit_sound:
            line += f" Their outliers lean on {sd_label(hit_sound)[0].lower()}{sd_label(hit_sound)[1:]}."
        elif best_hook:
            line += f" Their best hook type is {HOOK_LABELS.get(best_hook, best_hook).lower()}."
        out.append({"account_id": acc, "handle": handle, "top_format": vf,
                    "top_format_label": vf_label(vf) if vf else None,
                    "top_format_share": round(share, 3) if vf else None, "top_format_typical": typ,
                    "top_sub_format": sub, "top_sub_format_name": (
                        (formats.get(sub) or {}).get("name") or fallback_name(
                            (formats.get(sub) or {}).get("vf"), (formats.get(sub) or {}).get("recipe") or {}))
                    if sub else None,
                    "best_hook_type": best_hook,
                    "median_length_s": round(st.median(durs)) if durs else None, "cut_pace": pace,
                    "top_sound": sound, "top_tracks": top_tracks, "reels": len(us), "line": line})
    return out


def you_vs_them(units: list[Unit], rows: dict, *, now: datetime, window_days: int | None,
                self_scored: int) -> dict | None:
    """6.7 step 9: the member's own account vs competitors (needs >= 10 scored reels of their own)."""
    if self_scored < 10:
        return None
    mine = [u for u in units if u.is_self and u.tagged and u.score is not None and u.weight > 0
            and _in_window(u, now, window_days)]
    covered = sorted({t for u in units if u.is_self and u.tagged and _in_window(u, now, 180)
                      for t in values_of(u, "topic") if t != "unsorted"})
    vis = [u for u in mine if values_of(u, "visual_format")]
    Wm = sum(u.weight for u in vis)
    them = {r["value"]: r["usage"] for r in rows.get("visual_format", [])}
    vfs = sorted({v for u in vis for v in values_of(u, "visual_format")} | set(them))
    fmts = []
    for vf in vfs:
        you = sum(u.weight for u in vis if vf in values_of(u, "visual_format")) / Wm if Wm > 0 else 0.0
        t_items = [(u.log2, u.weight) for u in vis if vf in values_of(u, "visual_format") and u.log2 is not None]
        all_items = [(u.log2, u.weight) for u in vis if u.log2 is not None]
        yt = None
        if len(t_items) >= 2 and len(all_items) - len(t_items) >= 2:
            a, b = _wmean(t_items), _wmean(all_items)
            if a is not None and b is not None:
                yt = round(2 ** (a - b), 2)
        fmts.append({"vf": vf, "label": vf_label(vf), "you": round(you, 3), "them": round(them.get(vf, 0.0), 3),
                     "your_typical": yt})
    fmts.sort(key=lambda f: (-max(f["you"], f["them"]), f["vf"]))
    tthem = {r["value"]: r["usage"] for r in rows.get("topic", [])}
    Wt = sum(u.weight for u in mine)
    tps = sorted({v for u in mine for v in values_of(u, "topic")} | set(tthem))
    topics = [{"topic": t, "you": round(sum(u.weight for u in mine if t in values_of(u, "topic")) / Wt, 3)
               if Wt > 0 else 0.0, "them": round(tthem.get(t, 0.0), 3)} for t in tps]
    line = ""
    if fmts and Wm > 0:
        top = max(fmts, key=lambda f: (f["you"], f["vf"]))
        line = (f"You post {top['label']} {round(top['you'] * 100)}% of the time; your competitors "
                f"{round(top['them'] * 100)}%.")
        best = [r for r in rows.get("visual_format", []) if r.get("lift") and r["lift"] >= 1.5
                and r["evidence"] != "thin"]
        for r in best:
            mine_share = next((f["you"] for f in fmts if f["vf"] == r["value"]), 0.0)
            if mine_share == 0:
                line += f" Their best format, {vf_label(r['value'])}, you've never tried."
                break
    return {"formats": fmts, "topics": topics, "topics_covered": covered, "line": line or None,
            "reels": len(mine)}


# ---------------------------------------------------------------------------------------------- music (4.9 MusicRow)

GENERIC_AUDIO = ("original audio", "original sound", "audio original", "son original")


def music_rows(reels: Iterable[dict], *, limit: int = 30, outliers_only: bool = False) -> list[dict]:
    """reels: [{"id", "url", "audio", "is_hit": bool}] of priority-set competitor reels. Numbers only, no AI.
    outliers_only: keep only tracks at least one outlier used (the "Music the outliers used" list, fix round 2)."""
    agg: dict[tuple, dict] = {}
    for r in reels:
        a = r.get("audio") or {}
        typ = a.get("type")
        title = (a.get("title") or "").strip()
        if typ not in ("licensed", "original") or not title:
            continue
        kind = "licensed" if typ == "licensed" else ("reused_original" if a.get("is_reused") else "original")
        if kind in ("original", "reused_original") and title.lower() in GENERIC_AUDIO:
            continue
        key = (kind, title.lower(), (a.get("artist") or "").lower())
        row = agg.setdefault(key, {"title": title, "artist": a.get("artist") or None, "type": kind, "outliers": 0,
                                   "reels": 0, "example_reel": None, "_best": -1.0})
        row["reels"] += 1
        if r.get("is_hit"):
            row["outliers"] += 1
        sc = float(r.get("score") or 0)
        if sc > row["_best"]:
            row["_best"] = sc
            row["example_reel"] = {"id": r.get("id"), "url": r.get("url")}
    out = sorted(agg.values(), key=lambda x: (-x["outliers"], -x["reels"], x["title"].lower()))
    if outliers_only:
        out = [x for x in out if x["outliers"] >= 1]
    for x in out:
        x.pop("_best", None)
    return out[:limit]
