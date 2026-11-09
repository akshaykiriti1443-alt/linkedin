"""The vault on disk: a SQLite index plus one folder and one note per reel.

    vault/
      inbox/<shortcode>/            raw downloads waiting to be processed
      library/<Category>/<date>_<shortcode>_<slug>/
          video.mp4
          frames/*.jpg
          transcript.txt            plain transcript
          transcript_timed.txt      transcript with timestamps
          analysis.json             everything Claude extracted
          note.md                   the readable card (Obsidian-friendly)
      index.sqlite                  search index (+ your purposes, ticks and remakes)
      index.html                    the dashboard (reelvault site)
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .purposes import clean, clean_guess

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    shortcode   TEXT PRIMARY KEY,
    status      TEXT NOT NULL,          -- pending | done | failed
    folder      TEXT NOT NULL,
    category    TEXT,
    title       TEXT,
    owner       TEXT,
    url         TEXT,
    hook_type   TEXT,
    cta_type    TEXT,
    format      TEXT,
    structure   TEXT,
    error       TEXT,
    added_at    TEXT NOT NULL,
    analyzed_at TEXT,
    data        TEXT                    -- full JSON record
);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    shortcode UNINDEXED, title, category, tags, summary, transcript, hook, onscreen, caption
);
"""

# Columns added after 0.3. Old databases get them on open; nothing else changes.
NEW_COLUMNS = {
    "purposes": "TEXT NOT NULL DEFAULT '[]'",     # JSON list of purpose ids
    "purpose_guess": "TEXT",                       # JSON {"purpose", "reason"} or NULL
    "purpose_confirmed": "INTEGER NOT NULL DEFAULT 0",
    "extra": "TEXT NOT NULL DEFAULT '{}'",         # JSON {"todo_done": [...], "remakes": [...]}
}


def migrate(db: sqlite3.Connection) -> list[str]:
    """Add any missing columns to items. Safe to run on every open, also from several threads or
    processes at once (the app opens the vault once per request). Returns the columns it added."""
    have = {r[1] for r in db.execute("PRAGMA table_info(items)")}
    if all(name in have for name in NEW_COLUMNS):
        return []
    added = []
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")  # one writer at a time; others wait (busy timeout) and then see the columns
    try:
        have = {r[1] for r in db.execute("PRAGMA table_info(items)")}
        for name, decl in NEW_COLUMNS.items():
            if name in have:
                continue
            try:
                db.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")
                added.append(name)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return added


def _loads(text, default):
    try:
        v = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_-]+", "-", s)[:n].strip("-") or "untitled"


def safe_dir(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*&]', "-", name).strip() or "Other"


class Library:
    def __init__(self, vault_dir: Path):
        self.root = vault_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(vault_dir / "index.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        migrate(self.db)

    # --- bookkeeping -----------------------------------------------------
    def has(self, code: str) -> bool:
        return self.db.execute("SELECT 1 FROM items WHERE shortcode=?", (code,)).fetchone() is not None

    def add_pending(self, code: str, folder: Path) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO items(shortcode,status,folder,added_at) VALUES(?,?,?,?)",
            (code, "pending", str(folder), _now()),
        )
        self.db.commit()

    def pending(self, retry_failed: bool = False) -> list[sqlite3.Row]:
        states = ("pending", "failed") if retry_failed else ("pending",)
        q = f"SELECT * FROM items WHERE status IN ({','.join('?' * len(states))}) ORDER BY added_at"
        return self.db.execute(q, states).fetchall()

    def mark_failed(self, code: str, error: str) -> None:
        self.db.execute("UPDATE items SET status='failed', error=? WHERE shortcode=?", (error[:2000], code))
        self.db.commit()

    def get(self, code: str) -> dict | None:
        row = self.db.execute("SELECT data FROM items WHERE shortcode=? AND status='done'", (code,)).fetchone()
        return json.loads(row["data"]) if row else None

    def all_done(self) -> list[dict]:
        rows = self.db.execute("SELECT data FROM items WHERE status='done' ORDER BY analyzed_at DESC").fetchall()
        return [json.loads(r["data"]) for r in rows]

    # --- purposes and your own state ------------------------------------
    def rows_done(self) -> list[dict]:
        """Every analyzed reel, newest first: the record plus purposes, purpose_guess, purpose_confirmed, extra."""
        rows = self.db.execute("SELECT * FROM items WHERE status='done' ORDER BY analyzed_at DESC").fetchall()
        return [self._row(r) for r in rows]

    def row(self, code: str) -> dict | None:
        r = self.db.execute("SELECT * FROM items WHERE shortcode=? AND status='done'", (code,)).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        rec = json.loads(r["data"])
        rec["purposes"] = clean(_loads(r["purposes"], []))
        rec["purpose_guess"] = clean_guess(_loads(r["purpose_guess"], {}))
        rec["purpose_confirmed"] = bool(r["purpose_confirmed"])
        rec["extra"] = _loads(r["extra"], {})
        rec["analyzed_at"] = r["analyzed_at"]
        return rec

    def set_purposes(self, code: str, purposes: list[str], confirmed: bool | None = True) -> list[str]:
        """Save the purposes. confirmed=None leaves the confirmed flag as it is."""
        purposes = clean(purposes)
        if confirmed is None:
            self.db.execute("UPDATE items SET purposes=? WHERE shortcode=?", (json.dumps(purposes), code))
        else:
            self.db.execute("UPDATE items SET purposes=?, purpose_confirmed=? WHERE shortcode=?",
                            (json.dumps(purposes), int(confirmed), code))
        self.db.commit()
        return purposes

    def set_guess(self, code: str, guess: dict) -> bool:
        """Store an AI guess. If you have not confirmed this reel yet, the guess becomes its purpose."""
        guess = clean_guess(guess)
        r = self.db.execute("SELECT purpose_confirmed, extra FROM items WHERE shortcode=?", (code,)).fetchone()
        if not guess or r is None:
            return False
        self.db.execute("UPDATE items SET purpose_guess=? WHERE shortcode=?", (json.dumps(guess), code))
        if not r["purpose_confirmed"]:
            # A remake you already made stays tagged; the guess replaces the rest.
            keep = ["remake"] if _loads(r["extra"], {}).get("remakes") else []
            self.db.execute("UPDATE items SET purposes=? WHERE shortcode=?",
                            (json.dumps(clean([guess["purpose"], *keep])), code))
        self.db.commit()
        return True

    def unguessed(self) -> list[dict]:
        """Reels from before 0.4 that have no AI guess and that you have not sorted yourself yet."""
        rows = self.db.execute(
            "SELECT * FROM items WHERE status='done' AND purpose_guess IS NULL AND purpose_confirmed=0 "
            "ORDER BY analyzed_at DESC").fetchall()
        return [self._row(r) for r in rows]

    def update_extra(self, code: str, change) -> dict:
        """Run change(extra) on this reel's saved state and store the result.
        The read and the write happen under one write lock, so two changes at once never undo each other."""
        if self.db.in_transaction:
            self.db.commit()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            r = self.db.execute("SELECT extra FROM items WHERE shortcode=?", (code,)).fetchone()
            if r is None:
                raise KeyError(code)
            extra = _loads(r["extra"], {})
            change(extra)
            self.db.execute("UPDATE items SET extra=? WHERE shortcode=?", (json.dumps(extra, ensure_ascii=False), code))
        return extra

    def stats(self) -> dict:
        rows = self.db.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # --- filing a finished reel -----------------------------------------
    def file(self, code: str, work_dir: Path, record: dict) -> Path:
        """Move the work folder into library/<Category>/ and index it."""
        a = record["analysis"]
        meta = record["meta"]
        date = (str(meta.get("posted_at") or "")[:10]) or _now()[:10]
        dest = self.root / "library" / safe_dir(a["category"]) / f"{date}_{code}_{slugify(a['title'])}"
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work_dir), str(dest))
        record["folder"] = str(dest.relative_to(self.root))
        (dest / "analysis.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        (dest / "note.md").write_text(render_note(record), encoding="utf-8")
        self._index(code, dest, record)
        return dest

    def _index(self, code: str, dest: Path, record: dict) -> None:
        a, meta, tr = record["analysis"], record["meta"], record["transcript"]
        sk = a["skeleton"]
        self.db.execute(
            """UPDATE items SET status='done', folder=?, category=?, title=?, owner=?, url=?, hook_type=?,
               cta_type=?, format=?, structure=?, error=NULL, analyzed_at=?, data=? WHERE shortcode=?""",
            (str(dest), a["category"], a["title"], meta.get("owner"), meta.get("url"), sk["hook"]["hook_type"],
             sk["cta"]["cta_type"], a["content_format"], sk["structure_name"], _now(),
             json.dumps(record, ensure_ascii=False), code),
        )
        if clean_guess(a.get("purpose_guess")):
            self.set_guess(code, a["purpose_guess"])  # new reels: the AI guess is their purpose until you confirm
        self.db.execute("DELETE FROM search WHERE shortcode=?", (code,))
        self.db.execute(
            "INSERT INTO search VALUES(?,?,?,?,?,?,?,?,?)",
            (code, a["title"], a["category"], " ".join(a["tags"]), a["summary"], tr.get("text", ""),
             f"{sk['hook']['spoken']} {sk['hook']['on_screen_text']}", " ".join(a["visuals"]["on_screen_text"]),
             meta.get("caption", "")),
        )
        self.db.commit()

    def search(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT s.shortcode, i.title, i.category, i.folder,
                      snippet(search, -1, '[', ']', '…', 12) AS hit
               FROM search s JOIN items i ON i.shortcode = s.shortcode
               WHERE search MATCH ? ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()

    def rebuild_notes(self) -> int:
        n = 0
        for rec in self.all_done():
            folder = self.root / rec["folder"]
            if folder.exists():
                (folder / "note.md").write_text(render_note(rec), encoding="utf-8")
                n += 1
        return n


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Markdown card
# --------------------------------------------------------------------------

def _bullets(items) -> str:
    return "\n".join(f"- {x}" for x in items if x) or "- (none)"


def render_note(rec: dict) -> str:
    a, meta = rec["analysis"], rec["meta"]
    sk, vis = a["skeleton"], a["visuals"]
    hook, cta = sk["hook"], sk["cta"]
    fm = {
        "title": a["title"],
        "category": a["category"],
        "subcategory": a["subcategory"],
        "tags": a["tags"],
        "creator": meta.get("owner", ""),
        "url": meta.get("url", ""),
        "format": a["content_format"],
        "hook_type": hook["hook_type"],
        "cta_type": cta["cta_type"],
        "structure": sk["structure_name"],
        "duration_s": rec.get("duration"),
    }
    front = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()) + "\n---\n"
    beats = "\n".join(
        f"{i}. **{b['start_seconds']:.0f}s · {b['purpose']}** — {b['point']}  \n   _Visual:_ {b['visual']}"
        for i, b in enumerate(sk["meat"], 1)
    )
    steps = "\n".join(f"{i}. **{s['step']}** ({s['effort']}) — {s['why']}" for i, s in enumerate(a["action_plan"], 1))
    ideas = "\n\n".join(
        f"**{x['title']}**  \nHook: {x['hook']}  \n" + "\n".join(f"  - {o}" for o in x["outline"]) + f"  \nCTA: {x['cta']}"
        for x in a["script_ideas"]
    )
    extras = ""
    if a.get("claims_to_check"):
        extras += f"\n**Check before you trust**\n{_bullets(a['claims_to_check'])}\n"
    if a.get("client_explainer"):
        extras += f"\n**Explain it to a client**\n{a['client_explainer']}\n"
    link = f"[Open on Instagram]({meta['url']})" if meta.get("url") else ""
    if rec.get("video", "video.mp4"):
        media = f"![[{rec.get('video', 'video.mp4')}]]"
    elif rec.get("cover"):
        media = f"![cover]({rec['cover']})"
    else:
        media = ""
    return f"""{front}
# {a['title']}

{link} · @{meta.get('owner', '')} · {a['category']} › {a['subcategory']}

{media}

## Summary
{a['summary']}

**Key takeaways**
{_bullets(a['key_takeaways'])}

## Skeleton — {sk['structure_name']}

### 🎣 Hook ({hook['hook_type']}, first {hook['ends_at_seconds']:.0f}s)
- **Said:** {hook['spoken'] or '—'}
- **On screen:** {hook['on_screen_text'] or '—'}
- **Visual:** {hook['visual']}
- **Why it works:** {hook['why_it_works']}

### 🥩 Meat
{beats}

### 📣 CTA ({cta['cta_type']}, {cta['placement']})
{cta['text'] or '—'}{f"  (keyword: **{cta['keyword']}**)" if cta['keyword'] else ''}

**Pacing:** {sk['pacing']}

**Retention devices**
{_bullets(sk['retention_devices'])}

## What the visuals say
{vis['what_the_visuals_say']}

- **Style:** {vis['visual_style']}
- **Editing:** {', '.join(vis['editing_techniques'])}

**On-screen text**
{_bullets(vis['on_screen_text'])}

## Plan of action
{steps}

## Reusable template
```
{a['fill_in_template']}
```

## Hook variations
{_bullets(a['hook_variations'])}

## Script ideas for you
{ideas}

## Ready-to-shoot script
{a['ready_script']}

## Why it performed
{a['why_it_performed']}
{extras}
**Quotes worth saving**
{_bullets(a['notable_quotes'])}

## Transcript
{rec['transcript'].get('text') or '(no speech)'}

## Original caption
{meta.get('caption') or '(none)'}
"""
