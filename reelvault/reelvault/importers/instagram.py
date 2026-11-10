"""The member's own Instagram bio as a profile source (DESIGN-SAAS.md 9.2). Owner: WP7.

WP2's `/api/ext/v2/ig-user` handler calls `save_ig_bio(user_id, source_id, IgUser)` after an `ig_profile` job with
purpose `member_bio`. `ig_user` is `igparse.AccountIn.to_dict()`: pk, username, full_name, biography, bio_links,
external_url, category, is_private, is_verified, followers, following, media_count, profile_pic_url.
"""
from __future__ import annotations

import re

from . import TEXT_MAX

HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")


def normalize_handle(value: str) -> str:
    """'@Jane.Doe', 'instagram.com/jane.doe/', 'https://www.instagram.com/jane.doe?igsh=x' -> 'jane.doe'.
    Raises ValueError with a plain message."""
    v = (value or "").strip()
    m = re.search(r"instagram\.com/([A-Za-z0-9._]{1,30})", v, re.I)
    if m:
        v = m.group(1)
    v = v.lstrip("@").strip().strip("/").lower()
    if not HANDLE_RE.match(v) or v in ("p", "reel", "reels", "explore", "accounts", "stories"):
        raise ValueError("That doesn't look like an Instagram handle. Use @yourname or your profile link.")
    return v


def bio_text(ig_user: dict) -> str:
    u = ig_user or {}

    def s(k: str, n: int = 2000) -> str:
        v = u.get(k)
        return v.strip()[:n] if isinstance(v, str) else ""

    lines = [f"Instagram @{s('username', 30)}" + (f" ({s('full_name', 100)})" if s("full_name") else "")]
    if s("category"):
        lines.append(f"Category: {s('category', 100)}")
    if isinstance(u.get("followers"), int):
        lines.append(f"Followers: {u['followers']:,}")
    if s("biography"):
        lines += ["Bio:", s("biography")]
    links = []
    for b in u.get("bio_links") or []:
        if isinstance(b, dict) and isinstance(b.get("url"), str):
            title = b.get("title") if isinstance(b.get("title"), str) else ""
            links.append(f"- {title.strip()[:80] + ': ' if title.strip() else ''}{b['url'][:300]}")
    if s("external_url") and not any(s("external_url") in x for x in links):
        links.append(f"- {s('external_url', 300)}")
    if links:
        lines += ["Links in bio:"] + links
    return "\n".join(lines)[:TEXT_MAX]


def found_links(ig_user: dict) -> list[dict]:
    out = []
    urls = [b.get("url") for b in (ig_user or {}).get("bio_links") or [] if isinstance(b, dict)]
    if isinstance((ig_user or {}).get("external_url"), str):
        urls.insert(0, ig_user["external_url"])
    for u in urls:
        if not isinstance(u, str) or not u.startswith(("http://", "https://")):
            continue
        if re.search(r"skool\.com/", u):
            out.append({"kind": "skool", "url": u})
        elif re.search(r"linkedin\.com/in/", u):
            out.append({"kind": "linkedin_page", "url": u})
        elif not re.search(r"(instagram|facebook|tiktok|youtube|threads|x|twitter)\.com/|linktr\.ee|wa\.me", u):
            out.append({"kind": "website", "url": u})
    seen, uniq = set(), []
    for x in out:
        if x["url"] not in seen:
            seen.add(x["url"])
            uniq.append(x)
    return uniq[:4]


def save_ig_bio(user_id: str, source_id: str, ig_user: dict) -> None:
    """Write the bio text into profile_sources (status ok). Only the source's owner's row is touched."""
    from ..server import db

    text = bio_text(ig_user)
    keep = {k: ig_user.get(k) for k in ("username", "full_name", "category", "followers", "external_url",
                                        "is_private", "is_verified") if k in (ig_user or {})}
    keep["bio_links"] = [b for b in (ig_user or {}).get("bio_links") or [] if isinstance(b, dict)][:8]
    keep["found_links"] = found_links(ig_user)
    ok = bool((ig_user or {}).get("biography") or keep["bio_links"] or keep.get("external_url"))
    with db.tx() as conn:
        src = db.get_owned(conn, db.profile_sources, user_id, source_id)
        if src is None:
            return
        conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == source_id,
                                                       db.profile_sources.c.user_id == user_id).values(
            text=text, meta=dict(src.meta or {}, **keep), status="ok" if ok else "needs_paste",
            error=None if ok else "Your Instagram bio is empty. Paste a few lines about what you do instead.",
            fetched_at=db.utcnow()))
