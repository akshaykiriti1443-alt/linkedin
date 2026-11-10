"""Skool importer (DESIGN-SAAS.md 9.2): group About pages and @profiles. Owner: WP7.

Port of the research prototype `probe/skool_extract.py`. The same parser runs on:
- `__NEXT_DATA__` uploaded by the member's own browser (`page_read`, WP2 calls `parse_next_data`), and
- a server fetch (fallback A, only when `app_settings.skool_fetch` is true and only on the member's click).

Fields kept (group): metadata.displayName, description, lpDescription (full About), survey (membership questions,
a JSON string), links, owner (name, bio, link_instagram/linkedin/website/youtube), totalMembers, privacy, labels.
Fields kept (profile): firstName, lastName, metadata.bio, linkInstagram, linkLinkedin, linkWebsite, linkYoutube,
location. Nothing else (no emails, no member lists).
"""
from __future__ import annotations

import html as htmlmod
import json
import re
from typing import Any
from urllib.parse import urlparse

from . import TEXT_MAX

NEXT_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
OG_RE = re.compile(r'<meta[^>]+property="og:(title|description)"[^>]+content="([^"]*)"', re.I)
GROUP_RE = re.compile(r"^https://www\.skool\.com/[a-z0-9-]{1,80}/about$")
PROFILE_RE = re.compile(r"^https://www\.skool\.com/@[A-Za-z0-9-]{1,60}$")
RESERVED = {"signup", "login", "discovery", "settings", "chat", "notifications", "about", "pricing", "games"}
NEEDS_PASTE = "Paste your About text here."
TIMEOUT_S = 10
MAX_BYTES = 3 * 1024 * 1024


def normalize_url(url: str) -> tuple[str, str]:
    """Any Skool link the member types -> (kind, canonical url).
    'skool.com/@jane-doe' -> ('skool_profile', 'https://www.skool.com/@jane-doe');
    'https://www.skool.com/my-group' or '.../my-group/about?x=1' -> ('skool_group', 'https://www.skool.com/my-group/about').
    Raises ValueError with a plain message."""
    u = (url or "").strip()
    if not u:
        raise ValueError("Add your Skool link.")
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    host = (p.hostname or "").lower()
    if host not in ("skool.com", "www.skool.com"):
        raise ValueError("That is not a Skool link. It should start with skool.com/.")
    parts = [x for x in (p.path or "").split("/") if x]
    bad = ("That Skool link doesn't look right. Use your profile link (skool.com/@you) or your group's About page "
           "(skool.com/your-group/about).")
    if not parts:
        raise ValueError(bad)
    if parts[0].startswith("@"):
        handle = parts[0]
        if not re.match(r"^@[A-Za-z0-9-]{1,60}$", handle):
            raise ValueError(bad)
        return "skool_profile", f"https://www.skool.com/{handle}"
    slug = parts[0].lower()
    if not re.match(r"^[a-z0-9-]{1,80}$", slug) or slug in RESERVED:
        raise ValueError(bad)
    return "skool_group", f"https://www.skool.com/{slug}/about"


def _j(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _s(v: Any, n: int = 5000) -> str:
    return v.strip()[:n] if isinstance(v, str) else ""


def _page_props(next_data: Any) -> dict:
    if not isinstance(next_data, dict):
        return {}
    props = next_data.get("props")
    if isinstance(props, dict) and isinstance(props.get("pageProps"), dict):
        return props["pageProps"]
    if isinstance(next_data.get("pageProps"), dict):
        return next_data["pageProps"]
    return next_data


def _group(g: dict) -> tuple[list[str], dict] | None:
    md = g.get("metadata") if isinstance(g.get("metadata"), dict) else {}
    name = _s(md.get("displayName"), 200)
    if not name:
        return None
    owner = _j(md.get("owner"))
    owner = owner if isinstance(owner, dict) else {}
    om = owner.get("metadata") if isinstance(owner.get("metadata"), dict) else {}
    survey = _j(md.get("survey"))
    survey = survey.get("survey") if isinstance(survey, dict) else survey
    survey = survey if isinstance(survey, list) else []
    links = _j(md.get("links"))
    links = links.get("links") if isinstance(links, dict) else links
    links = links if isinstance(links, list) else []
    labels = [_s((lb.get("metadata") or {}).get("displayName"), 80) for lb in (g.get("labels") or [])
              if isinstance(lb, dict) and isinstance(lb.get("metadata"), dict)]
    labels = [x for x in labels if x]
    questions = []
    for q in survey:
        if not isinstance(q, dict) or not _s(q.get("question")):
            continue
        if (q.get("type") or "") == "email":
            continue  # "What's your email?" says nothing about the customer
        opts = [_s(o, 200) for o in (q.get("options") or []) if _s(o)]
        questions.append({"q": _s(q.get("question"), 300), "type": _s(q.get("type"), 20), "options": opts[:12]})
    link_rows = [{"label": _s(lk.get("label"), 120), "url": _s(lk.get("url"), 300)} for lk in links
                 if isinstance(lk, dict) and _s(lk.get("url"))]
    owner_name = " ".join(x for x in (_s(owner.get("first_name"), 60), _s(owner.get("last_name"), 60)) if x)
    owner_links = {k: _s(om.get(f"link_{k}"), 300) for k in ("website", "instagram", "linkedin", "youtube")
                   if _s(om.get(f"link_{k}"))}
    private = md.get("privacy") == 1
    members = md.get("totalMembers") if isinstance(md.get("totalMembers"), int) else None

    lines = [f"Skool group: {name}"]
    if _s(md.get("description")):
        lines.append(_s(md.get("description"), 500))
    facts = []
    if members is not None:
        facts.append(f"{members:,} members")
    facts.append("private group" if private else "public group")
    if labels:
        facts.append("categories: " + ", ".join(labels[:8]))
    lines.append(" · ".join(facts))
    if _s(md.get("lpDescription")):
        lines += ["", "About:", _s(md.get("lpDescription"), 8000)]
    if questions:
        lines += ["", "Membership questions (what new members are asked):"]
        for q in questions:
            lines.append(f"- {q['q']}" + (f" (options: {' | '.join(q['options'])})" if q["options"] else ""))
    if link_rows:
        lines += ["", "Links:"] + [f"- {lk['label'] or 'Link'}: {lk['url']}" for lk in link_rows[:10]]
    if owner_name or _s(om.get("bio")):
        lines += ["", f"Owner: {owner_name}" + (f" (@{_s(owner.get('name'), 60)})" if _s(owner.get("name")) else "")]
        if _s(om.get("bio")):
            lines.append(f"Owner bio: {_s(om.get('bio'), 500)}")
        for k, v in owner_links.items():
            lines.append(f"Owner {k}: {v}")
    meta = {"kind": "group", "slug": _s(g.get("name"), 80), "display_name": name, "total_members": members,
            "private": private, "labels": labels, "survey": questions, "links": link_rows,
            "owner": {"name": owner_name, "handle": _s(owner.get("name"), 60), "bio": _s(om.get("bio"), 500)},
            "owner_links": owner_links}
    return lines, meta


def _person(u: dict) -> tuple[list[str], dict] | None:
    um = u.get("metadata") if isinstance(u.get("metadata"), dict) else {}
    handle = _s(u.get("name"), 60)
    name = " ".join(x for x in (_s(u.get("firstName"), 60), _s(u.get("lastName"), 60)) if x)
    if not (handle or name):
        return None
    links = {k: _s(um.get(f"link{k.capitalize()}"), 300) for k in ("website", "instagram", "linkedin", "youtube")
             if _s(um.get(f"link{k.capitalize()}"))}
    lines = [f"Skool profile: {name}" + (f" (@{handle})" if handle else "")]
    if _s(um.get("bio")):
        lines.append(f"Bio: {_s(um.get('bio'), 1000)}")
    if _s(um.get("location")):
        lines.append(f"Location: {_s(um.get('location'), 120)}")
    for k, v in links.items():
        lines.append(f"{k.capitalize()}: {v}")
    meta = {"kind": "profile", "handle": handle, "display_name": name, "bio": _s(um.get("bio"), 1000),
            "location": _s(um.get("location"), 120), "links": links}
    return lines, meta


def parse_next_data(next_data: Any) -> dict:
    """Skool `__NEXT_DATA__` (full, or `{props: {pageProps: {currentGroup, currentUser}}}` from the extension, or the
    bare pageProps) -> {"text": str, "meta": dict}. Empty text when the page holds neither a group nor a profile
    (for example the Skool sign-in page)."""
    pp = _page_props(next_data)
    lines: list[str] = []
    meta: dict = {}
    g = pp.get("currentGroup") if isinstance(pp.get("currentGroup"), dict) else {}
    got = _group(g) if g else None
    if got:
        lines, meta = got
    u = pp.get("currentUser") if isinstance(pp.get("currentUser"), dict) else {}
    # On a group page currentUser is the VIEWER (often the member themself); only a profile page carries
    # profileData, so only then is it the person the page is about.
    if u and u.get("profileData") is not None and not got:
        person = _person(u)
        if person:
            lines, meta = person
    text = "\n".join(lines).strip()[:TEXT_MAX]
    meta["found_links"] = found_links(meta)
    return {"text": text, "meta": meta}


def parse_html(html: str) -> dict:
    """A saved/fetched Skool page -> {"text", "meta", "status", "error"?}. Falls back to og:title/og:description
    when there is no `__NEXT_DATA__`; a sign-up/sign-in page or an empty page -> needs_paste."""
    html = html or ""
    m = NEXT_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
        except ValueError:
            data = None
        if data is not None:
            out = parse_next_data(data)
            if out["text"]:
                return dict(out, status="ok")
    og = {k.lower(): htmlmod.unescape(v).strip() for k, v in OG_RE.findall(html)}
    title, desc = og.get("title", ""), og.get("description", "")
    if title and not re.search(r"^skool\s*:?\s*(sign ?up|log ?in|sign ?in)", title, re.I) and desc:
        text = f"{title}\n{desc}"[:TEXT_MAX]
        return {"text": text, "meta": {"kind": "og", "display_name": title[:200], "found_links": []},
                "status": "needs_paste" if len(text) < 80 else "ok",
                **({"error": "We only found a short summary. " + NEEDS_PASTE} if len(text) < 80 else {})}
    return {"text": "", "meta": {"found_links": []}, "status": "needs_paste",
            "error": "Skool showed a sign-in page instead. " + NEEDS_PASTE}


def found_links(meta: dict) -> list[dict]:
    """Links worth offering as new sources ("We found your website: add it?")."""
    links = dict(meta.get("links") if meta.get("kind") == "profile" else meta.get("owner_links") or {})
    out = []
    if links.get("website"):
        out.append({"kind": "website", "url": links["website"]})
    if links.get("instagram"):
        m = re.search(r"instagram\.com/([A-Za-z0-9._]{1,30})", links["instagram"])
        if m:
            out.append({"kind": "instagram_bio", "url": f"https://www.instagram.com/{m.group(1).lower()}/"})
    if links.get("linkedin"):
        out.append({"kind": "linkedin_page", "url": links["linkedin"]})
    return out


def _no_signup(inner):
    """Wraps the pinned transport: a redirect to Skool's sign-up or sign-in page is NOT followed (9.2)."""
    import httpx

    class NoSignup(httpx.BaseTransport):
        def handle_request(self, request):
            if re.match(r"^/(signup|login|sign-in|signin)\b", request.url.path or ""):
                return httpx.Response(403, request=request, text="sign-in page not followed")
            return inner.handle_request(request)

        def close(self):
            close = getattr(inner, "close", None)
            if close:
                close()

    return NoSignup()


def fetch_page(url: str, *, public_origin: str = "", transport: Any = None) -> dict:
    """Server fallback A: ONE GET of the member's own Skool page with an honest user agent, 10 s, no sign-up
    redirects followed. Returns parse_html's dict. The caller checks `skool_fetch` and the daily limit."""
    from ..ai.net import PinnedTransport
    from ..server.security import FetchError, safe_fetch

    kind, canon = normalize_url(url)
    ua = f"ReelVaultMembers/1.0 (+{public_origin or 'https://reelvault.members'})"
    wrapped = _no_signup(transport or PinnedTransport())
    try:
        res = safe_fetch(canon, max_bytes=MAX_BYTES, timeout=TIMEOUT_S, transport=wrapped,
                         headers={"User-Agent": ua, "Accept": "text/html"}, allow_http=False)
    except FetchError as e:
        return {"text": "", "meta": {"found_links": []}, "status": "needs_paste", "error": f"{e} {NEEDS_PASTE}"}
    if res.status >= 400:
        return {"text": "", "meta": {"found_links": []}, "status": "needs_paste",
                "error": f"Skool didn't show the page ({res.status}). {NEEDS_PASTE}"}
    out = parse_html(res.text)
    out["meta"] = dict(out.get("meta") or {}, final_url=res.url, page_kind=kind)
    return out
