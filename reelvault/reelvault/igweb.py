"""Talk to Instagram the same way instagram.com does in your browser.

Uses your login cookies (sessionid, csrftoken, ds_user_id...) and the web app's
own JSON endpoints: who am I, my Saved feed, and one post by its link. This
avoids the old GraphQL query ids that other tools depend on, which Instagram
changes often.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator

import requests

WEB_APP_ID = "936619743392459"   # the id instagram.com sends with every API call
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
BASE = "https://www.instagram.com"
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


class IGError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Cookies on disk
# --------------------------------------------------------------------------

def save_cookies(path: Path, cookies: dict) -> None:
    keep = {k: v for k, v in cookies.items() if v}
    path.write_text(json.dumps(keep, indent=2), encoding="utf-8")


def load_cookies(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

def session(cookies: dict) -> requests.Session:
    if not cookies.get("sessionid"):
        raise IGError("Not logged in to Instagram.")
    s = requests.Session()
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".instagram.com", path="/")
    s.headers.update({
        "User-Agent": UA,
        "X-IG-App-ID": WEB_APP_ID,
        "X-CSRFToken": cookies.get("csrftoken", ""),
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE + "/",
        "Accept": "*/*",
    })
    return s


def _get(s: requests.Session, path: str, params: dict | None = None) -> dict:
    r = s.get(BASE + path, params=params, timeout=30, allow_redirects=False)
    if r.status_code in (301, 302) or "/accounts/login" in r.headers.get("Location", ""):
        raise IGError("Instagram logged you out. Connect Instagram again.")
    if r.status_code == 401 or r.status_code == 403:
        raise IGError("Instagram refused the request. Connect Instagram again.")
    if r.status_code == 429:
        raise IGError("Instagram says 'too many requests'. Wait an hour, then try again.")
    if r.status_code != 200:
        raise IGError(f"Instagram answered {r.status_code} for {path}.")
    try:
        data = r.json()
    except ValueError:
        raise IGError("Instagram sent a page instead of data. Connect Instagram again.")
    if data.get("status") == "fail":
        msg = data.get("message") or "request failed"
        if "login" in msg.lower() or data.get("require_login"):
            raise IGError("Instagram logged you out. Connect Instagram again.")
        raise IGError(f"Instagram: {msg}")
    return data


def whoami(s: requests.Session, cookies: dict) -> str:
    uid = cookies.get("ds_user_id")
    if not uid:
        raise IGError("The login is missing your user id. Log in to Instagram again.")
    user = _get(s, f"/api/v1/users/{uid}/info/").get("user") or {}
    if not user.get("username"):
        raise IGError("Instagram didn't return your username.")
    return user["username"]


# --------------------------------------------------------------------------
# Posts
# --------------------------------------------------------------------------

def shortcode_to_id(code: str) -> int:
    n = 0
    for ch in code:
        n = n * 64 + ALPHABET.index(ch)
    return n


def id_to_shortcode(media_id: int) -> str:
    out = ""
    while media_id:
        media_id, r = divmod(media_id, 64)
        out = ALPHABET[r] + out
    return out or "A"


def saved_media(s: requests.Session, pause: float = 2.0) -> Iterator[dict]:
    """Your Saved posts, newest first (the 'All posts' collection)."""
    max_id = None
    while True:
        data = _get(s, "/api/v1/feed/saved/posts/", {"max_id": max_id} if max_id else None)
        for item in data.get("items", []):
            m = item.get("media") or item
            if m:
                yield m
        if not data.get("more_available") or not data.get("next_max_id"):
            return
        max_id = data["next_max_id"]
        time.sleep(pause)


def media_by_shortcode(s: requests.Session, code: str) -> dict:
    items = _get(s, f"/api/v1/media/{shortcode_to_id(code)}/info/").get("items") or []
    if not items:
        raise IGError(f"Post {code} not found (deleted or private).")
    return items[0]


def best_video_url(m: dict) -> str | None:
    """The video in a reel, or the first video inside a carousel."""
    for node in [m] + list(m.get("carousel_media") or []):
        versions = node.get("video_versions") or []
        if versions:
            best = max(versions, key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))
            return best.get("url")
    return None


def cover_url(m: dict) -> str | None:
    cands = (m.get("image_versions2") or {}).get("candidates") or []
    return cands[0].get("url") if cands else None


def _views_of(m: dict) -> tuple[int | None, str | None]:
    """views = play_count ?? ig_play_count ?? view_count (DESIGN-SAAS.md 5.8), and which field it came from."""
    for field in ("play_count", "ig_play_count", "view_count"):
        v = m.get(field)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return v, field
    return None, None


def audio_of(m: dict) -> dict:
    """Sound metadata from clips_metadata: type original/licensed/unknown, title, artist, canonical id, reused."""
    cm = m.get("clips_metadata") or {}
    mi = (cm.get("music_info") or {}).get("music_asset_info") or {}
    osi = cm.get("original_sound_info") or {}
    at = (cm.get("audio_type") or "").lower()
    typ = "licensed" if at.startswith("licensed") else ("original" if at.startswith("original") else "unknown")
    owner_pk = str((m.get("user") or m.get("owner") or {}).get("pk") or "")
    artist_pk = str((osi.get("ig_artist") or {}).get("pk") or "")
    return {
        "type": typ,
        "title": mi.get("title") if typ == "licensed" else (osi.get("original_audio_title") or mi.get("title")),
        "artist": mi.get("display_artist"),
        "music_canonical_id": (str(cm["music_canonical_id"]) if cm.get("music_canonical_id") else None),
        "is_reused": bool(typ == "original" and osi and artist_pk and owner_pk and artist_pk != owner_pk),
    }


def meta_of(m: dict) -> dict:
    code = m.get("code") or id_to_shortcode(int(str(m.get("pk") or m.get("id", "0")).split("_")[0]))
    cap = m.get("caption") or {}
    user = m.get("user") or {}
    hidden = bool(m.get("like_and_view_counts_disabled")) or (isinstance(m.get("like_count"), int)
                                                              and m["like_count"] < 0)
    views, views_field = _views_of(m)
    owner_pk = str(user.get("pk") or "")
    pinned_ids = {str(x) for x in (m.get("clips_tab_pinned_user_ids") or []) + (m.get("timeline_pinned_user_ids") or [])}
    shares = m.get("reshare_count") if m.get("reshare_count") is not None else m.get("share_count")
    return {
        "shortcode": code,
        "url": f"{BASE}/reel/{code}/",
        "owner": user.get("username", ""),
        "caption": (cap.get("text") if isinstance(cap, dict) else str(cap)) or "",
        "posted_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(m["taken_at"])) if m.get("taken_at") else None,
        "likes": m.get("like_count"),
        "views": m.get("play_count") or m.get("view_count") or m.get("ig_play_count"),
        "duration": m.get("video_duration"),
        "source": "instagram-web",
        # Added for ReelVault Members (WP2); the desktop app ignores keys it does not know.
        "comment_count": None if m.get("comments_disabled") else m.get("comment_count"),
        "pinned": bool(owner_pk and owner_pk in pinned_ids),
        "counts_hidden": hidden,
        "views_field": None if hidden else views_field,
        "audio": audio_of(m),
        "shares": shares,
        "reposts": m.get("media_repost_count"),
    }


def download(s: requests.Session, url: str, dest: Path) -> None:
    with s.get(url, stream=True, timeout=120) as r:
        if r.status_code != 200:
            raise IGError(f"Video download failed ({r.status_code}).")
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
        tmp.replace(dest)
