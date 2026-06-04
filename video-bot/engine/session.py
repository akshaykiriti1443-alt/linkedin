#!/usr/bin/env python3
"""
session.py — Project session persistence.
Reads/writes workspace/project.md to track completed pipeline steps.
Re-running auto_edit.py skips steps that are already done.
"""

import os, json
from datetime import datetime
from pathlib import Path

WORKSPACE = Path("workspace")
PROJECT_MD = WORKSPACE / "project.md"

STEPS = [
    "transcribe",
    "timeline_preview",
    "cut_xml",
    "color_grade",
    "sfx",
    "render_graphics",
    "build_timeline",
]

def _load() -> dict:
    if not PROJECT_MD.exists():
        return {}
    done = {}
    for line in PROJECT_MD.read_text().splitlines():
        if line.startswith("- [x]"):
            parts = line[6:].split("|", 1)
            step = parts[0].strip()
            meta = json.loads(parts[1].strip()) if len(parts) > 1 else {}
            done[step] = meta
    return done

def is_done(step: str) -> bool:
    return step in _load()

def mark_done(step: str, metadata: dict = None):
    WORKSPACE.mkdir(exist_ok=True)
    meta_str = f" | {json.dumps(metadata)}" if metadata else ""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"- [x] {step}{meta_str}  <!-- {ts} -->\n"

    if not PROJECT_MD.exists():
        PROJECT_MD.write_text(f"# project.md — Video Bot Session\n\n## Completed Steps\n\n")

    content = PROJECT_MD.read_text()
    # Update if step already listed, else append
    if f"- [x] {step}" in content:
        import re
        content = re.sub(rf"- \[x\] {step}.*\n", line, content)
        PROJECT_MD.write_text(content)
    else:
        with open(PROJECT_MD, "a") as f:
            f.write(line)

def reset(step: str = None):
    """Reset one step or all steps."""
    if not PROJECT_MD.exists():
        return
    if step is None:
        PROJECT_MD.unlink()
        print("Session reset — all steps cleared.")
        return
    content = PROJECT_MD.read_text()
    import re
    content = re.sub(rf"- \[x\] {step}.*\n", "", content)
    PROJECT_MD.write_text(content)
    print(f"Step '{step}' reset.")

def status():
    done = _load()
    print("\n📋 Session status:")
    for s in STEPS:
        icon = "✅" if s in done else "⬜"
        meta = done.get(s, {})
        suffix = f"  ({meta.get('duration_s', '')}s)" if "duration_s" in meta else ""
        print(f"  {icon}  {s}{suffix}")
    print()

if __name__ == "__main__":
    import sys
    if "--reset" in sys.argv:
        idx = sys.argv.index("--reset")
        step = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        reset(step)
    else:
        status()
