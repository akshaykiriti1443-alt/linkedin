"""Write new scripts from the skeletons in your vault."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from . import brain
from .config import Config
from .library import Library, slugify

REMIX_SYSTEM = """You write short-form video scripts (Instagram reels, TikTok, Shorts) for one creator.

You get skeletons from reels the creator saved: hook, beats, CTA, and why each worked.
Borrow the structure and the psychology. Never copy their words or claims.

For each script give:
1. Title
2. Skeleton used (name the source reel)
3. HOOK (0-3s): spoken line + on-screen text + [VISUAL]
4. MEAT: numbered beats with timing, spoken lines and [VISUAL] cues
5. CTA: spoken + on-screen
6. Caption (with the CTA keyword if any) and 5 hashtags
7. Two alternate hooks

Plain words. Short sentences. Active voice. Fit the creator's voice and offer from the profile."""


REMIX_INSTRUCTION = "Write the scripts asked for at the end of the input. Output Markdown."


def unique_path(folder: Path, stem: str, suffix: str = ".md") -> Path:
    """folder/stem.md, or stem-2.md, stem-3.md ... so a new run never overwrites earlier scripts."""
    out = folder / f"{stem}{suffix}"
    n = 2
    while out.exists():
        out = folder / f"{stem}-{n}{suffix}"
        n += 1
    return out


def _skeleton_brief(rec: dict) -> dict:
    a = rec["analysis"]
    return {
        "source": f"@{rec['meta'].get('owner', '')} — {a['title']}",
        "category": a["category"],
        "structure": a["skeleton"]["structure_name"],
        "hook": a["skeleton"]["hook"],
        "meat": a["skeleton"]["meat"],
        "cta": a["skeleton"]["cta"],
        "retention_devices": a["skeleton"]["retention_devices"],
        "template": a["fill_in_template"],
        "why_it_performed": a["why_it_performed"],
    }


def pick_sources(lib: Library, codes: list[str], category: str | None, hook_type: str | None, n: int) -> list[dict]:
    if codes:
        recs = [lib.get(c) for c in codes]
        missing = [c for c, r in zip(codes, recs) if r is None]
        if missing:
            raise SystemExit(f"Not in the vault (or not analyzed yet): {', '.join(missing)}")
        return recs
    recs = lib.all_done()
    if category:
        recs = [r for r in recs if r["analysis"]["category"].lower() == category.lower()]
    if hook_type:
        recs = [r for r in recs if r["analysis"]["skeleton"]["hook"]["hook_type"] == hook_type]
    if not recs:
        raise SystemExit("No analyzed reels match those filters.")
    return recs[:n]


def remix(cfg: Config, lib: Library, topic: str, codes: list[str], category: str | None = None,
          hook_type: str | None = None, count: int = 3, sources: int = 5) -> Path:
    recs = pick_sources(lib, codes, category, hook_type, sources)
    briefs = [_skeleton_brief(r) for r in recs]
    prompt = (
        f"<creator_profile>\n{cfg.profile_text()}\n</creator_profile>\n\n"
        f"<skeletons>\n{json.dumps(briefs, indent=2, ensure_ascii=False)}\n</skeletons>\n\n"
        f"Write {count} scripts about: {topic}\n"
        "Use a different skeleton for each script where you can. Output Markdown."
    )
    print(f"Writing {count} script(s) from {len(briefs)} skeleton(s)...")
    if cfg.brain != "api":
        # The topic stays in the prompt (sent through stdin). It can hold quotes and & from other people's
        # hooks, and cmd.exe would break such text on the command line of an npm claude.cmd.
        text = brain.text(prompt, REMIX_INSTRUCTION, REMIX_SYSTEM, cfg.vault_dir, cfg.model, cfg.effort)
    else:
        import anthropic

        client = anthropic.Anthropic()
        with client.beta.messages.stream(
            model=cfg.model,
            max_tokens=32000,
            system=REMIX_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": cfg.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise SystemExit("Claude declined this request.")
        text = "".join(b.text for b in msg.content if b.type == "text")
    out_dir = cfg.vault_dir / "scripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = unique_path(out_dir, f"{date.today()}_{slugify(topic)}")
    sources_md = "\n".join(f"- {b['source']} ({b['structure']})" for b in briefs)
    out.write_text(f"# Scripts: {topic}\n\n**Skeletons used**\n{sources_md}\n\n---\n\n{text}\n", encoding="utf-8")
    return out
