"""Get your saved Instagram posts onto disk.

Instagram's official Graph API has no endpoint for saved posts, so there are
three ways in:

1. ``from_saved``  - log in with your own session through Instaloader and walk
   your Saved collection. Fastest, fully automatic.
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
# 1. Logged-in session (Instaloader)
# --------------------------------------------------------------------------

def _login(cfg: Config):
    import instaloader

    L = instaloader.Instaloader(
        download_pictures=cfg.include_images,
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        dirname_pattern=str(cfg.inbox / "{target}"),
        filename_pattern="{shortcode}",
        quiet=True,
    )
    user = cfg.instagram_username
    if not user:
        raise SystemExit("Set instagram_username in reelvault.toml (or IG_USERNAME).")
    try:
        L.load_session_from_file(user, cfg.session_file or None)
    except FileNotFoundError:
        raise SystemExit(
            "No saved Instagram session found.\n"
            f"Create one once with:   instaloader --login {user}\n"
            "or import it from your browser:   instaloader --load-cookies chrome\n"
            "Then run this command again."
        )
    if L.test_login() != user:
        raise SystemExit("The saved session has expired. Log in again with: instaloader --login " + user)
    return L


def from_saved(cfg: Config, lib: Library, limit: int | None = None, pause: float = 4.0) -> int:
    """Download saved posts we do not have yet (newest first)."""
    import instaloader

    L = _login(cfg)
    me = instaloader.Profile.own_profile(L.context)
    added = 0
    for post in me.get_saved_posts():
        if limit is not None and added >= limit:
            break
        code = post.shortcode
        if lib.has(code):
            continue
        if not post.is_video and not cfg.include_images:
            # Carousels may still hold videos
            if post.typename != "GraphSidecar" or not any(n.is_video for n in post.get_sidecar_nodes()):
                continue
        folder = cfg.inbox / code
        folder.mkdir(parents=True, exist_ok=True)
        L.download_post(post, target=code)
        video = _first_video(folder)
        if video and video.name != "video.mp4":
            video.rename(folder / "video.mp4")
        _write_meta(
            folder,
            {
                "shortcode": code,
                "url": f"https://www.instagram.com/p/{code}/",
                "owner": post.owner_username,
                "caption": post.caption or "",
                "posted_at": post.date_utc.isoformat(),
                "likes": post.likes,
                "views": post.video_view_count if post.is_video else None,
                "duration": post.video_duration if post.is_video else None,
                "source": "instaloader",
            },
        )
        lib.add_pending(code, folder)
        added += 1
        print(f"  saved {code} by @{post.owner_username}")
        time.sleep(pause)  # be gentle; Instagram rate-limits hard
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
