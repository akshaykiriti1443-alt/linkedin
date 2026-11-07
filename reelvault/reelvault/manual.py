"""File a breakdown that was written outside the API pipeline.

This is the "no API key" route. Inside a Claude Code session (your Claude
subscription), Claude watches a reel through the vidIQ connector, writes the
breakdown as JSON in the ReelAnalysis shape, and files it here:

    reelvault add record.json --cover cover.jpg --walkthrough walkthrough.md

record.json:
    {
      "shortcode": "DfXyDVPopFX",
      "meta": {"url": "...", "owner": "hormozi", "caption": "...", "views": 202711, ...},
      "duration": 41.1,
      "transcript": {"language": "en", "text": "...", "segments": [{"start": 0, "end": 6, "text": "..."}]},
      "analysis": { ...ReelAnalysis fields... }
    }
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import Config
from .library import Library
from .schema import ReelAnalysis


def add_record(cfg: Config, lib: Library, record_path: Path, cover: Path | None = None,
               walkthrough: Path | None = None) -> Path:
    rec = json.loads(record_path.read_text(encoding="utf-8"))
    code = rec["shortcode"]
    rec["analysis"] = ReelAnalysis.model_validate(rec["analysis"]).model_dump()  # fail fast on a bad shape
    rec.setdefault("meta", {}).setdefault("owner", "")
    rec.setdefault("transcript", {"language": "", "text": "", "segments": []})
    rec.setdefault("frames", [])
    rec["video"] = None
    rec["source"] = rec.get("source", "vidiq+claude-code")

    old = lib.db.execute("SELECT folder, status FROM items WHERE shortcode=?", (code,)).fetchone()
    if old and old["status"] == "done" and Path(old["folder"]).exists():
        shutil.rmtree(old["folder"])  # replace an earlier breakdown of the same reel

    work = cfg.inbox / code
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    if cover and cover.exists():
        shutil.copy2(cover, work / "cover.jpg")
        rec["cover"] = "cover.jpg"
    if walkthrough and walkthrough.exists():
        shutil.copy2(walkthrough, work / "walkthrough.md")
    tr = rec["transcript"]
    (work / "transcript.txt").write_text(tr.get("text") or "(no speech)", encoding="utf-8")
    (work / "transcript_timed.txt").write_text(
        "\n".join(f"[{s['start']:>6.1f}s] {s['text']}" for s in tr.get("segments", [])) or "(no speech)",
        encoding="utf-8",
    )
    (work / "meta.json").write_text(json.dumps(rec["meta"], indent=2, ensure_ascii=False), encoding="utf-8")
    lib.add_pending(code, work)
    return lib.file(code, work, rec)


def restore_from_dashboard(cfg: Config, lib: Library, html_path: Path) -> int:
    """Rebuild the vault from a published dashboard (its data is embedded in the page).

    Use this in a fresh session: read the claude.ai Artifact, save the HTML, then restore.
    """
    import base64
    import re
    import tempfile

    html = html_path.read_text(encoding="utf-8")
    m = re.search(r"const DATA = (\[.*?\]);\n", html, re.S)
    if not m:
        raise SystemExit("No reel data found in that page.")
    items = json.loads(m.group(1))
    n = 0
    with tempfile.TemporaryDirectory() as tmp:
        for it in items:
            rec = {
                "shortcode": it["id"],
                "meta": it.get("meta") or {},
                "duration": it.get("duration"),
                "transcript": {"text": it.get("transcript", ""), "segments": it.get("segments", [])},
                "analysis": it["a"],
            }
            rp = Path(tmp) / f"{it['id']}.json"
            rp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            cover = None
            thumb = it.get("thumb") or ""
            if thumb.startswith("data:image"):
                cover = Path(tmp) / f"{it['id']}.jpg"
                cover.write_bytes(base64.b64decode(thumb.split(",", 1)[1]))
            add_record(cfg, lib, rp, cover)
            n += 1
    return n
