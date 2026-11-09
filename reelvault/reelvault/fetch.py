"""Get your saved Instagram posts onto disk.

Instagram's official Graph API has no endpoint for saved posts, so there are
three ways in:

1. ``from_saved``  - use your Instagram login (cookies) and read your Saved
   feed the way instagram.com does. Fastest, fully automatic.
2. ``from_export`` - read the JSON from Instagram's "Download your information"
   export, then download each link with yt-dlp. Slower, but your login never
   leaves the browser.
3. ``from_folder`` - you already have the .mp4 files. We just import them.

Every item lands in ``vault/inbox/<shortcode>/`` with ``video.mp4`` and ``meta.json``.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Iterator

from .config import Config
from .library import Library

SHORTCODE_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


def _write_meta(folder: Path, meta: dict) -> None:
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _first_video(folder: Path) -> Path | None:
    vids = sorted(folder.glob("*.mp4"))
    return vids[0] if vids else None


# --------------------------------------------------------------------------
# 1. Your Instagram login (cookies), via instagram.com's own web API
# --------------------------------------------------------------------------

def _web(cfg: Config):
    from . import igweb

    cookies = igweb.load_cookies(Path(cfg.cookies_file))
    if not cookies.get("sessionid"):
        raise SystemExit("Connect Instagram first. Go to Settings and click Connect Instagram.")
    return igweb, igweb.session(cookies)


def _save_media(cfg: Config, lib: Library, igweb, s, m: dict) -> bool:
    meta = igweb.meta_of(m)
    code = meta["shortcode"]
    url = igweb.best_video_url(m)
    if not url:
        return False  # photo post
    folder = cfg.inbox / code
    folder.mkdir(parents=True, exist_ok=True)
    try:
        igweb.download(s, url, folder / "video.mp4")
    except Exception as e:
        print(f"  could not download {code}: {e}")
        shutil.rmtree(folder, ignore_errors=True)
        return False
    _write_meta(folder, meta)
    lib.add_pending(code, folder)
    print(f"  saved {code} by @{meta['owner']}")
    return True


def from_saved(cfg: Config, lib: Library, limit: int | None = None, pause: float = 3.0) -> int:
    """Download saved reels we do not have yet (newest first)."""
    igweb, s = _web(cfg)
    added = seen = 0
    try:
        for m in igweb.saved_media(s):
            if limit is not None and added >= limit:
                break
            seen += 1
            code = m.get("code")
            if not code or lib.has(code):
                continue
            if _save_media(cfg, lib, igweb, s, m):
                added += 1
                time.sleep(pause)  # be gentle; Instagram rate-limits hard
    except igweb.IGError as e:
        raise SystemExit(str(e))
    if seen == 0:
        print("  Instagram returned no saved posts.")
    return added


def from_links(cfg: Config, lib: Library, urls: list[str], pause: float = 3.0) -> int:
    """Download specific reels by link through your login."""
    igweb, s = _web(cfg)
    added = 0
    for url in urls:
        m = SHORTCODE_RE.search(url)
        if not m or lib.has(m.group(1)):
            continue
        try:
            media = igweb.media_by_shortcode(s, m.group(1))
        except igweb.IGError as e:
            print(f"  could not open {m.group(1)}: {e}")
            if "logged you out" in str(e) or "too many" in str(e):
                raise SystemExit(str(e))
            continue
        if _save_media(cfg, lib, igweb, s, media):
            added += 1
        else:
            print(f"  {m.group(1)} has no video, skipping")
        time.sleep(pause)
    return added


# --------------------------------------------------------------------------
# 2. "Download your information" export + yt-dlp
# --------------------------------------------------------------------------

def urls_from_export(path: Path) -> list[str]:
    """Pull every post/reel URL out of the export, whatever its exact layout."""
    files = [path] if path.is_file() else list(path.rglob("*saved*.json")) or list(path.rglob("*.json"))
    seen: dict[str, str] = {}
    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in SHORTCODE_RE.finditer(text.replace("\\/", "/")):
            seen.setdefault(m.group(1), f"https://www.instagram.com/p/{m.group(1)}/")
    return list(seen.values())


def from_urls(cfg: Config, lib: Library, urls: list[str], cookies_from_browser: str | None = None,
              limit: int | None = None, pause: float = 3.0) -> int:
    import yt_dlp

    added = 0
    for url in urls:
        if limit is not None and added >= limit:
            break
        m = SHORTCODE_RE.search(url)
        if not m:
            print(f"  skip (not an Instagram post URL): {url}")
            continue
        code = m.group(1)
        if lib.has(code):
            continue
        folder = cfg.inbox / code
        folder.mkdir(parents=True, exist_ok=True)
        opts = {
            "outtmpl": str(folder / "video.%(ext)s"),
            "format": "mp4/bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        if cookies_from_browser:
            opts["cookiesfrombrowser"] = (cookies_from_browser,)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:  # yt-dlp raises many error types; log and move on
            print(f"  failed {code}: {e}")
            shutil.rmtree(folder, ignore_errors=True)
            continue
        if not (folder / "video.mp4").exists():
            vid = _first_video(folder)
            if not vid:
                print(f"  {code} has no video, skipping")
                shutil.rmtree(folder, ignore_errors=True)
                continue
            vid.rename(folder / "video.mp4")
        _write_meta(
            folder,
            {
                "shortcode": code,
                "url": url,
                "owner": info.get("uploader") or info.get("channel") or "",
                "caption": info.get("description") or "",
                "posted_at": info.get("timestamp"),
                "likes": info.get("like_count"),
                "views": info.get("view_count"),
                "duration": info.get("duration"),
                "source": "yt-dlp",
            },
        )
        lib.add_pending(code, folder)
        added += 1
        print(f"  saved {code}")
        time.sleep(pause)
    return added


# --------------------------------------------------------------------------
# 3. Local folder of videos
# --------------------------------------------------------------------------

def _iter_videos(folder: Path) -> Iterator[Path]:
    for ext in ("*.mp4", "*.mov", "*.m4v", "*.webm"):
        yield from folder.rglob(ext)


def from_folder(cfg: Config, lib: Library, folder: Path) -> int:
    added = 0
    for src in sorted(_iter_videos(folder)):
        code = re.sub(r"[^A-Za-z0-9_-]", "_", src.stem)[:60]
        if lib.has(code):
            continue
        dest = cfg.inbox / code
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / "video.mp4")
        sidecar = src.with_suffix(".txt")
        _write_meta(
            dest,
            {
                "shortcode": code,
                "url": "",
                "owner": "local",
                "caption": sidecar.read_text(encoding="utf-8") if sidecar.exists() else "",
                "source": f"local:{src}",
            },
        )
        lib.add_pending(code, dest)
        added += 1
        print(f"  imported {src.name}")
    return added
