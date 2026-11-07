"""reelvault command line.

    reelvault init                      make reelvault.toml + profile.md
    reelvault login                     log in to Instagram once, save the session
    reelvault fetch [--limit 20]        download new saved reels
    reelvault process                   transcribe + analyze everything waiting
    reelvault run                       fetch + process + rebuild dashboard and KB
    reelvault site / kb                 rebuild the dashboard / knowledge base
    reelvault search "pricing hook"     full-text search
    reelvault remix --topic "..."       write new scripts from saved skeletons
    reelvault status                    counts
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import DEFAULT_CATEGORIES, load_config
from .library import Library

TOML_TEMPLATE = """# reelvault settings
vault_dir = "vault"                 # where videos, notes and the dashboard live
instagram_username = ""             # your Instagram handle, no @
session_file = ""                   # leave empty to use Instaloader's default location

model = "claude-opus-5-5"           # Claude model for analysis and scripts
effort = "medium"                   # low | medium | high | xhigh | max
whisper_model = "small"             # tiny | base | small | medium | large-v3 (bigger = better, slower)
max_frames = 12                     # stills sent to Claude per reel
frame_width = 768
include_images = false              # true = also keep photo posts (no transcript)

categories = [
{cats}
]
"""

PROFILE_TEMPLATE = """# Creator profile
Claude reads this to write hooks and scripts that fit YOU. Edit it.

- Name / brand:
- Niche:
- Audience (who, what they struggle with):
- What I sell or offer:
- Voice (e.g. direct, funny, calm, expert):
- Language(s):
- Main CTA I want (follow, DM keyword, link in bio...):
- Topics I avoid:
"""


def cmd_init(args, cfg):
    if Path("reelvault.toml").exists():
        print("reelvault.toml already exists, leaving it.")
    else:
        cats = "\n".join(f'    "{c}",' for c in DEFAULT_CATEGORIES)
        Path("reelvault.toml").write_text(TOML_TEMPLATE.format(cats=cats), encoding="utf-8")
        print("Wrote reelvault.toml")
    if not Path("profile.md").exists():
        Path("profile.md").write_text(PROFILE_TEMPLATE, encoding="utf-8")
        print("Wrote profile.md — fill it in so scripts match your niche.")
    print("Next: set instagram_username in reelvault.toml, then run: reelvault login")


def cmd_login(args, cfg):
    import instaloader

    user = cfg.instagram_username or input("Instagram username: ").strip()
    L = instaloader.Instaloader()
    L.interactive_login(user)  # asks for password and 2FA code in the terminal
    L.save_session_to_file(cfg.session_file or None)
    print(f"Session saved for @{user}. Your password is not stored.")


def cmd_fetch(args, cfg, lib):
    from . import fetch

    if args.folder:
        n = fetch.from_folder(cfg, lib, Path(args.folder))
    elif args.from_export or args.urls:
        urls = fetch.urls_from_export(Path(args.from_export)) if args.from_export else \
            [u.strip() for u in Path(args.urls).read_text().splitlines() if u.strip()]
        print(f"Found {len(urls)} links.")
        n = fetch.from_urls(cfg, lib, urls, args.cookies_from_browser, args.limit)
    else:
        print("Reading your Saved collection...")
        n = fetch.from_saved(cfg, lib, args.limit)
    print(f"Added {n} new item(s).")
    return n


def cmd_process(args, cfg, lib):
    from .analyze import process_pending

    ok, bad = process_pending(cfg, lib, retry_failed=args.retry_failed, limit=args.limit)
    print(f"Done: {ok} analyzed, {bad} failed.")


def cmd_site(args, cfg, lib):
    from .site import build

    print(f"Dashboard: {build(lib).resolve()}")
    print(f"Portable dashboard (host anywhere): {build(lib, standalone=True).resolve()}")


def cmd_add(args, cfg, lib):
    from .manual import add_record

    dest = add_record(cfg, lib, Path(args.record), Path(args.cover) if args.cover else None,
                      Path(args.walkthrough) if args.walkthrough else None)
    print(f"Filed: {dest}")


def cmd_restore(args, cfg, lib):
    from .manual import restore_from_dashboard

    print(f"Restored {restore_from_dashboard(cfg, lib, Path(args.page))} reel(s).")


def cmd_kb(args, cfg, lib):
    from .kb import export

    print(f"Knowledge base: {export(lib).resolve()}")


def cmd_run(args, cfg, lib):
    cmd_fetch(args, cfg, lib)
    args.retry_failed = False
    cmd_process(args, cfg, lib)
    cmd_site(args, cfg, lib)
    cmd_kb(args, cfg, lib)


def cmd_search(args, cfg, lib):
    q = " ".join(args.query)
    try:
        rows = lib.search(q, args.limit)
    except sqlite3.OperationalError:
        rows = lib.search('"' + q.replace('"', "") + '"', args.limit)  # treat odd input as a phrase
    if not rows:
        print("No matches.")
    for r in rows:
        print(f"{r['shortcode']:<14} {r['category']:<32} {r['title']}\n{'':15}{r['hit']}\n")


def cmd_remix(args, cfg, lib):
    from .remix import remix

    out = remix(cfg, lib, args.topic, args.source or [], args.category, args.hook_type, args.count, args.sources)
    print(f"Scripts: {out.resolve()}")


def cmd_status(args, cfg, lib):
    s = lib.stats()
    print(f"Vault: {cfg.vault_dir.resolve()}")
    for k in ("done", "pending", "failed"):
        print(f"  {k:<8} {s.get(k, 0)}")


def cmd_notes(args, cfg, lib):
    print(f"Rewrote {lib.rebuild_notes()} note(s).")


def main(argv=None):
    load_dotenv()
    p = argparse.ArgumentParser(prog="reelvault", description="Turn your saved Instagram reels into a searchable swipe file.")
    p.add_argument("--config", default="reelvault.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create reelvault.toml and profile.md")
    sub.add_parser("login", help="log in to Instagram and save the session")

    def fetch_opts(sp):
        sp.add_argument("--limit", type=int, help="max new items this run")
        sp.add_argument("--from-export", metavar="PATH", help="Instagram 'Download your information' folder or saved_posts.json")
        sp.add_argument("--urls", metavar="FILE", help="text file with one post/reel URL per line")
        sp.add_argument("--folder", metavar="DIR", help="import .mp4 files you already have")
        sp.add_argument("--cookies-from-browser", metavar="BROWSER", help="chrome, firefox, safari, edge... (for --from-export/--urls)")

    fetch_opts(sub.add_parser("fetch", help="download new saved reels"))
    sp = sub.add_parser("process", help="transcribe + analyze waiting reels")
    sp.add_argument("--retry-failed", action="store_true")
    sp.add_argument("--limit", type=int)
    sp = sub.add_parser("run", help="fetch + process + site + kb")
    fetch_opts(sp)
    sub.add_parser("site", help="rebuild vault/index.html and vault/dashboard.html")
    sp = sub.add_parser("add", help="file a breakdown written in a Claude Code session (no API key)")
    sp.add_argument("record", help="record JSON (see reelvault/manual.py)")
    sp.add_argument("--cover", help="cover image")
    sp.add_argument("--walkthrough", help="scene-by-scene walkthrough markdown")
    sp = sub.add_parser("restore", help="rebuild the vault from a saved dashboard page")
    sp.add_argument("page", help="dashboard.html or artifact.html")
    sub.add_parser("kb", help="rebuild vault/knowledge-base/")
    sub.add_parser("notes", help="rewrite every note.md from analysis.json")
    sub.add_parser("status", help="show counts")
    sp = sub.add_parser("search", help="full-text search")
    sp.add_argument("query", nargs="+")
    sp.add_argument("--limit", type=int, default=20)
    sp = sub.add_parser("remix", help="write new scripts from saved skeletons")
    sp.add_argument("--topic", required=True, help="what the new video is about")
    sp.add_argument("--source", nargs="*", metavar="SHORTCODE", help="use these reels' skeletons")
    sp.add_argument("--category", help="or pick skeletons from this category")
    sp.add_argument("--hook-type", help="or only reels with this hook type")
    sp.add_argument("--count", type=int, default=3, help="how many scripts")
    sp.add_argument("--sources", type=int, default=5, help="max skeletons to feed in")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd == "init":
        return cmd_init(args, cfg)
    if args.cmd == "login":
        return cmd_login(args, cfg)
    lib = Library(cfg.vault_dir)
    handlers = {"fetch": cmd_fetch, "process": cmd_process, "run": cmd_run, "site": cmd_site, "kb": cmd_kb,
                "search": cmd_search, "remix": cmd_remix, "add": cmd_add, "restore": cmd_restore, "status": cmd_status, "notes": cmd_notes}
    handlers[args.cmd](args, cfg, lib)


if __name__ == "__main__":
    sys.exit(main())
