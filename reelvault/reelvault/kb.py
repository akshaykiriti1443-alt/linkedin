"""Export the vault as a Markdown knowledge base (works great in Obsidian or Notion import).

    vault/knowledge-base/
      README.md            index of every reel by category
      hooks.md             swipe file of hooks, grouped by hook type
      ctas.md              every CTA, grouped by type
      structures.md        skeleton patterns and the reels that use them
      templates.md         all fill-in-the-blank templates
      action-plans.md      every plan of action, by category
      categories/<Category>.md   summaries + takeaways per category
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .library import Library, safe_dir


def _link(rec: dict, from_dir: Path, root: Path) -> str:
    """Relative Markdown link from a KB page to the reel's note.md."""
    up = [".."] * len(from_dir.relative_to(root).parts)
    rel = Path(*up, rec["folder"], "note.md").as_posix().replace(" ", "%20")
    return f"[{rec['analysis']['title']}]({rel})"


def _group(recs, key):
    g = defaultdict(list)
    for r in recs:
        g[key(r)].append(r)
    return dict(sorted(g.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def export(lib: Library) -> Path:
    root = lib.root
    kb = root / "knowledge-base"
    cats_dir = kb / "categories"
    cats_dir.mkdir(parents=True, exist_ok=True)
    recs = lib.all_done()
    A = lambda r: r["analysis"]  # noqa: E731
    by_cat = _group(recs, lambda r: A(r)["category"])

    # README
    lines = [f"# Reel knowledge base\n\n{len(recs)} reels analyzed.\n",
             "- [Hook swipe file](hooks.md)\n- [CTAs](ctas.md)\n- [Structures](structures.md)\n"
             "- [Templates](templates.md)\n- [Action plans](action-plans.md)\n"]
    for cat, items in by_cat.items():
        lines.append(f"\n## [{cat}](categories/{safe_dir(cat).replace(' ', '%20')}.md) ({len(items)})\n")
        lines += [f"- {_link(r, kb, root)} — @{r['meta'].get('owner', '')} · {A(r)['skeleton']['hook']['hook_type']}"
                  for r in items]
    (kb / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Hooks
    lines = ["# Hook swipe file\n"]
    for ht, items in _group(recs, lambda r: A(r)["skeleton"]["hook"]["hook_type"]).items():
        lines.append(f"\n## {ht} ({len(items)})\n")
        for r in items:
            h = A(r)["skeleton"]["hook"]
            said = h["spoken"] or h["on_screen_text"] or h["visual"]
            lines.append(f"- **\"{said}\"**  \n  {h['why_it_works']} — {_link(r, kb, root)}")
    (kb / "hooks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # CTAs
    lines = ["# CTA library\n"]
    for ct, items in _group(recs, lambda r: A(r)["skeleton"]["cta"]["cta_type"]).items():
        lines.append(f"\n## {ct} ({len(items)})\n")
        for r in items:
            c = A(r)["skeleton"]["cta"]
            kw = f" (keyword **{c['keyword']}**)" if c["keyword"] else ""
            lines.append(f"- \"{c['text'] or '—'}\"{kw} · {c['placement']} — {_link(r, kb, root)}")
    (kb / "ctas.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Structures
    lines = ["# Skeleton patterns\n"]
    for st, items in _group(recs, lambda r: A(r)["skeleton"]["structure_name"]).items():
        lines.append(f"\n## {st} ({len(items)})\n")
        ex = A(items[0])["skeleton"]
        lines.append("Example beats: " + " → ".join(b["purpose"] for b in ex["meat"]) + "\n")
        lines += [f"- {_link(r, kb, root)}" for r in items]
    (kb / "structures.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Templates
    lines = ["# Fill-in-the-blank templates\n"]
    for r in recs:
        lines.append(f"\n## {A(r)['skeleton']['structure_name']} — {_link(r, kb, root)}\n\n```\n{A(r)['fill_in_template']}\n```")
    (kb / "templates.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Action plans
    lines = ["# Plans of action\n"]
    for cat, items in by_cat.items():
        lines.append(f"\n## {cat}\n")
        for r in items:
            lines.append(f"\n### {_link(r, kb, root)}\n")
            lines += [f"- [ ] **{s['step']}** ({s['effort']}) — {s['why']}" for s in A(r)["action_plan"]]
    (kb / "action-plans.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Per category
    for cat, items in by_cat.items():
        lines = [f"# {cat}\n"]
        for r in items:
            a = A(r)
            lines.append(f"\n## {_link(r, cats_dir, root)}\n\n_{a['subcategory']} · @{r['meta'].get('owner', '')}_\n\n{a['summary']}\n")
            lines += [f"- {t}" for t in a["key_takeaways"]]
        (cats_dir / f"{safe_dir(cat)}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kb
