"""Website importer (DESIGN-SAAS.md 9.2): SSRF-safe fetch (security.safe_fetch, 11.8) + trafilatura. Owner: WP7.

fetch_site(url) reads the homepage and up to 4 same-site pages whose path or link text matches
about|services|pricing|offer|case|results|work|testimonial|reviews, extracts the main text as markdown
(<= 6,000 characters per page, <= 20,000 in total) and the page metadata. A site that gives less than 300
characters (a JavaScript-only site) comes back with status "needs_paste". No headless browser in v1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

from . import TEXT_MAX

PAGE_MAX = 6_000
MIN_TEXT = 300
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 10
EXTRA_PAGES = 4
LINK_WORDS = re.compile(r"about|services|pricing|offer|case|results|work|testimonial|reviews", re.I)
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|mp4|mov|zip|docx?|xlsx?|pptx?|css|js|xml|ico)$", re.I)
NEEDS_PASTE = "We couldn't read your site. Paste your homepage text."


@dataclass
class SiteResult:
    status: str                      # "ok" | "needs_paste"
    text: str = ""
    meta: dict = field(default_factory=dict)
    error: str | None = None


def normalize_url(url: str) -> str:
    """'scaleitads.com' -> 'https://scaleitads.com/'. Raises ValueError with a plain message."""
    u = (url or "").strip()
    if not u:
        raise ValueError("Add your website link.")
    if len(u) > 500:
        raise ValueError("That link is too long.")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    if p.scheme.lower() not in ("http", "https"):
        raise ValueError("That link must start with http:// or https://.")
    host = (p.hostname or "").lower()
    try:
        import ipaddress

        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if not is_ip and (not host or "." not in host or not re.match(r"^[a-z0-9.-]+$", host)):
        raise ValueError("That doesn't look like a website link.")
    if p.username or p.password:
        raise ValueError("Links with a user name or password are not allowed.")
    return p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(), path=p.path or "/", fragment="").geturl()


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def _site(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def pick_links(html: str, base_url: str, limit: int = EXTRA_PAGES) -> list[str]:
    """Same-site links whose path or anchor text names an about/services/pricing/... page, in page order."""
    p = _Links()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - broken HTML: use what was parsed
        pass
    base = urlparse(base_url)
    out: list[str] = []
    seen = {urldefrag(base_url)[0].rstrip("/")}
    for href, text in p.links:
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urldefrag(urljoin(base_url, href))[0]
        u = urlparse(full)
        if u.scheme not in ("http", "https") or _site(u.hostname or "") != _site(base.hostname or ""):
            continue
        if SKIP_EXT.search(u.path or ""):
            continue
        if not (LINK_WORDS.search(u.path or "") or LINK_WORDS.search(text or "")):
            continue
        key = full.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(full)
        if len(out) >= limit:
            break
    return out


def extract(html: str, url: str | None = None) -> tuple[str, dict]:
    """(markdown text <= PAGE_MAX, {"title", "description", "sitename"}) of one page."""
    import trafilatura

    text = trafilatura.extract(html, url=url, output_format="markdown", favor_recall=True, include_comments=False,
                               include_tables=True) or ""
    meta: dict[str, Any] = {}
    try:
        md = trafilatura.extract_metadata(html, default_url=url)
    except Exception:  # noqa: BLE001
        md = None
    if md is not None:
        for k in ("title", "description", "sitename"):
            v = getattr(md, k, None)
            if isinstance(v, str) and v.strip():
                meta[k] = v.strip()[:300]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:PAGE_MAX], meta


def _is_html(res) -> bool:
    ct = (res.headers.get("content-type") or "").lower()
    return not ct or "html" in ct or "xml" in ct


def fetch_site(url: str, *, transport: Any = None, max_pages: int = 1 + EXTRA_PAGES) -> SiteResult:
    """Read a member's website. Raises nothing for page problems: returns needs_paste with a plain reason.
    Raises ValueError (plain message) only for a link that is not a website link."""
    from ..server.security import FetchError, safe_fetch

    start = normalize_url(url)
    try:
        home = safe_fetch(start, max_bytes=MAX_BYTES, timeout=TIMEOUT_S, transport=transport,
                          headers={"Accept": "text/html,application/xhtml+xml"})
    except FetchError as e:
        return SiteResult("needs_paste", error=f"{e} {NEEDS_PASTE}")
    if home.status >= 400:
        return SiteResult("needs_paste", error=f"Your site answered with an error ({home.status}). {NEEDS_PASTE}")
    if not _is_html(home):
        return SiteResult("needs_paste", error=f"That link is not a web page. {NEEDS_PASTE}")
    html = home.text
    text, meta = extract(html, home.url)
    pages = [{"url": home.url, "chars": len(text)}]
    parts = [text] if text else []
    for link in pick_links(html, home.url, limit=max(0, max_pages - 1)):
        try:
            res = safe_fetch(link, max_bytes=MAX_BYTES, timeout=TIMEOUT_S, transport=transport,
                             headers={"Accept": "text/html,application/xhtml+xml"})
        except FetchError:
            continue
        if res.status >= 400 or not _is_html(res):
            continue
        t, m = extract(res.text, res.url)
        if len(t) < 40 or t in parts:
            continue
        title = m.get("title") or urlparse(res.url).path
        parts.append(f"## Page: {title}\n\n{t}")
        pages.append({"url": res.url, "chars": len(t)})
    head = []
    if meta.get("title"):
        head.append(f"# {meta['title']}")
    if meta.get("description"):
        head.append(meta["description"])
    full = "\n\n".join(head + parts).strip()[:TEXT_MAX]
    meta = dict(meta, pages=pages, final_url=home.url)
    body_chars = sum(len(p) for p in parts)
    if body_chars < MIN_TEXT:
        return SiteResult("needs_paste", text=full, meta=meta, error=NEEDS_PASTE)
    return SiteResult("ok", text=full, meta=meta)
