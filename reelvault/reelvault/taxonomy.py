"""Loads data/taxonomy.json: ids, labels, cues, needs, legacy map and prompt blocks (DESIGN-SAAS.md section 10).

taxonomy.json is the single source of truth. Adding an id = edit the JSON and bump `version`; old analyses keep their
ids. WP3 owns the `local_rules` block inside it; WP4 reads `needs`.

    VF_IDS, SD_IDS, EP_IDS, HOOK_TYPES, CTA_TYPES, CAPTION_STYLES, CUE_KINDS, NEEDS   tuples of ids
    VFId, SDId, HookType, CTAType, CaptionStyle, CueKind, Need                        typing.Literal types
    label(id) -> str            "VF04" -> "Green screen over image/tweet/article"
    needs(vf) -> [str]          what the member must be able to do to make that format
    legacy_vf(content_format)   desktop content_format -> VF id
    prompt_block(kind) -> str   "visual" | "sound" | "edit" | "lists" | "all": ids, labels, cues and precedence
    sfx_source(cue_kind) -> str where an editor finds a sound like that
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

PATH = Path(__file__).parent / "data" / "taxonomy.json"


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else {}


def _ids(key: str) -> tuple[str, ...]:
    return tuple(it["id"] for it in load().get(key) or [])


VF_IDS: tuple[str, ...] = _ids("visual_format")
VF_CHOICES: tuple[str, ...] = VF_IDS + ("other",)
SD_IDS: tuple[str, ...] = _ids("sound_design")
EP_IDS: tuple[str, ...] = _ids("edit_patterns")
HOOK_TYPES: tuple[str, ...] = tuple(load().get("hook_types") or ())
CTA_TYPES: tuple[str, ...] = tuple(load().get("cta_types") or ())
CAPTION_STYLES: tuple[str, ...] = tuple(load().get("caption_styles") or ())
CUE_KINDS: tuple[str, ...] = tuple(load().get("cue_kinds") or ())
NEEDS: tuple[str, ...] = tuple(load().get("needs") or ())

# Literal types used by schema_members.py (built from the JSON, so the schema enum always matches the file).
VFId = Literal[VF_CHOICES]  # type: ignore[valid-type]
SDId = Literal[SD_IDS]  # type: ignore[valid-type]
HookType = Literal[HOOK_TYPES]  # type: ignore[valid-type]
CTAType = Literal[CTA_TYPES]  # type: ignore[valid-type]
CaptionStyle = Literal[CAPTION_STYLES]  # type: ignore[valid-type]
CueKind = Literal[CUE_KINDS]  # type: ignore[valid-type]
Need = Literal[NEEDS]  # type: ignore[valid-type]


@lru_cache(maxsize=1)
def _index() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key in ("visual_format", "sound_design", "edit_patterns"):
        for it in load().get(key) or []:
            out[it["id"]] = it
    return out


def label(id_: str | None) -> str:
    """Plain label of a VF/SD/EP id ("other" -> "Other"). Unknown ids come back unchanged."""
    if not id_:
        return ""
    if id_ == "other":
        return "Other"
    it = _index().get(id_)
    return it["label"] if it else id_


def needs(vf: str | None) -> list[str]:
    it = _index().get(vf or "")
    return list(it.get("needs") or []) if it else []


def need_label(need: str) -> str:
    return (load().get("needs_labels") or {}).get(need, need.replace("_", " "))


def legacy_vf(content_format: str | None) -> str:
    return (load().get("legacy_content_format") or {}).get(content_format or "", "other")


def sfx_source(cue_kind: str | None) -> str:
    src = load().get("sfx_sources") or {}
    return src.get(cue_kind or "", src.get("other", ""))


def version() -> str:
    return str(load().get("version") or "")


def _vf_block() -> str:
    t = load()
    rules = t.get("rules") or {}
    lines = ["VISUAL FORMAT (pick 1 primary + 0-2 secondary):"]
    lines += [f"- {it['id']} {it['label']}: {it['cues']}" for it in t.get("visual_format") or []]
    lines.append("- other: only when nothing fits; give a label of 40 characters or less.")
    if rules.get("visual_format"):
        lines.append(rules["visual_format"])
    if rules.get("precedence"):
        lines.append("Precedence when several fit (apply in order):")
        lines += [f"{i}) {p}" for i, p in enumerate(rules["precedence"], 1)]
    if rules.get("visual_rule"):
        lines.append(rules["visual_rule"])
    return "\n".join(lines)


def _sd_block() -> str:
    t = load()
    lines = ["SOUND DESIGN (1 primary + up to 3 secondary, citing seconds):"]
    lines += [
        f"- {it['id']} {it['label']}: {it['cues']} [local rule: {it.get('signal_rule', '')}]"
        for it in t.get("sound_design") or []
    ]
    if (t.get("rules") or {}).get("sound"):
        lines.append(t["rules"]["sound"])
    return "\n".join(lines)


def _ep_block() -> str:
    t = load()
    lines = ["EDIT PATTERNS (fill every field; numbers come from local signals, never guessed):"]
    lines += [
        f"- {it['id']} {it['label']}: {it['cues']} (filled by {it.get('filled_by', 'AI')})"
        for it in t.get("edit_patterns") or []
    ]
    return "\n".join(lines)


def _lists_block() -> str:
    return "\n".join(
        [
            "hook_type: " + ", ".join(HOOK_TYPES),
            "cta_type: " + ", ".join(CTA_TYPES),
            "caption_style: " + ", ".join(CAPTION_STYLES),
            "sound cue kinds: " + ", ".join(CUE_KINDS),
        ]
    )


def prompt_block(kind: str = "all") -> str:
    """Text for prompts and the MCP get_taxonomy tool. kind: visual | sound | edit | lists | all."""
    parts = {"visual": _vf_block, "sound": _sd_block, "edit": _ep_block, "lists": _lists_block}
    if kind == "all":
        return "\n\n".join(f() for f in parts.values())
    if kind not in parts:
        raise ValueError(f"unknown taxonomy block {kind!r}")
    return parts[kind]()
