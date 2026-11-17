"""Normalize uploaded Instagram media objects into ReelIn rows (server side). DESIGN-SAAS.md 5.5, 5.8. WP2.

The extension uploads a TRIMMED media object (`IgMedia`, 5.5) and copies fields without interpreting them. This
module is the only place that interprets them:

    normalize(ig_media, account_pk) -> ReelIn | None   # None: someone else's reel, or not a video
    parse_user(ig_user) -> AccountIn                    # IgUser (or a raw web_profile_info / users/<id>/info user)
    shortcode_to_pk("C9x...") -> "3456789012345678901" ; pk_to_shortcode(pk) -> code
    cdn_expiry(signed_cdn_url) -> datetime | None        # from the hex `oe=` parameter
    normalize_lo(lo_item, account_pk) -> ReelIn | None   # logged-out items, shapes "v1" and "gql" (DESIGN-FETCH.md 4.2)
    pk_time(pk) -> datetime | None                       # the post time inside a media pk

Ids are digit STRINGS everywhere. A JSON number where an id belongs means a broken extension (JavaScript would have
rounded it), so normalize() raises IgParseError and the API answers 400 bad_input.

For tests and the fixtures, `parse_ig_json(text)` and `trim_media(raw)` / `trim_user(raw)` / `trim_gql(raw)` mirror
the extension's `bigint.js` and `trim.js`, so Python sees exactly what the extension would upload.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}
PK_RE = re.compile(r"^\d{1,20}$")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,20}$")
HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")
CAPTION_MAX = 5000
BIGINT_RE = re.compile(r'("[A-Za-z0-9_]+"\s*:\s*)(-?\d{16,})(?=\s*[,}\]])')
IG_EPOCH_MS = 1314220021721                    # media pk = (ms since this moment) << 23 | sequence
# Read layers and the strategies that upload items (DESIGN-FETCH.md 2, 3.2). `rented` / `rx_*` come with layer 3.
LO_STRATEGIES = ("lo_profile_page", "lo_reels_page", "lo_reels_gql", "lo_web_profile", "lo_posts_gql", "lo_post_gql",
                 "lo_media_gql", "lo_embed")
LO_SHAPES = ("v1", "gql")


class IgParseError(ValueError):
    """The upload does not have the shape the extension contract promises (5.5)."""


# ============================================================================================== ids

def shortcode_to_pk(code: str) -> str:
    """Base64-alphabet decode of a shortcode, as an exact digit string. Raises IgParseError on a bad code."""
    if not isinstance(code, str) or not CODE_RE.match(code):
        raise IgParseError("bad shortcode")
    n = 0
    for ch in code:
        n = n * 64 + _INDEX[ch]
    return str(n)


def pk_to_shortcode(pk: str | int) -> str:
    n = int(pk)
    out = ""
    while n:
        n, r = divmod(n, 64)
        out = ALPHABET[r] + out
    return out or "A"


def cdn_expiry(url: str | None) -> datetime | None:
    """Signed Instagram CDN URLs carry `oe=<hex unix seconds>`. Returns the expiry as aware UTC, else None."""
    if not url or not isinstance(url, str):
        return None
    try:
        qs = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    vals = qs.get("oe") or []
    if not vals or not re.fullmatch(r"[0-9A-Fa-f]{1,12}", vals[0]):
        return None
    try:
        return datetime.fromtimestamp(int(vals[0], 16), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _pk_str(v: Any, what: str, *, required: bool = True) -> str | None:
    """Digit string or None. JSON numbers (int/float) are refused: the extension must send strings (5.5)."""
    if v is None or v == "":
        if required:
            raise IgParseError(f"{what} is missing")
        return None
    if isinstance(v, bool) or not isinstance(v, str):
        raise IgParseError(f"{what} must be a digit string, not a JSON number")
    if not PK_RE.match(v):
        raise IgParseError(f"{what} must be digits")
    return v


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v == v else None
    if isinstance(v, str) and re.fullmatch(r"-?\d{1,20}", v):
        return int(v)
    return None


def _float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return None


def _str(v: Any, limit: int | None = None) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        v = str(v)
    return v[:limit] if limit else v


def _https(u: Any) -> str | None:
    u = _str(u, 4000)
    return u if u and u.startswith("https://") else None


# ============================================================================================== rows

@dataclass
class ReelIn:
    """One normalized reel, ready to upsert into `reels` (3.11) for the account it was pulled from."""

    ig_pk: str
    code: str
    url: str
    owner_pk: str
    owner_username: str | None
    role: str                                  # owner | coauthor (the pulled account's role on this reel)
    is_collab: bool                            # has coauthors
    coauthors: list[str]                       # handles
    coauthor_pks: list[str]
    taken_at: datetime | None
    caption: str
    duration: float | None
    has_audio: bool | None
    is_pinned: bool                            # pinned on the pulled account's profile / clips tab
    is_paid_partnership: bool
    audio: dict
    views: int | None
    likes: int | None
    comments: int | None
    views_field: str | None                    # play_count | ig_play_count | view_count
    shares: int | None
    reposts: int | None
    counts_hidden: bool
    video_url: str | None
    video_width: int | None
    cover_url: str | None
    video_url_expires_at: datetime | None
    pk_conflict: bool = False
    product_type: str | None = None
    # fetch engine (DESIGN-FETCH.md 4.2). normalize() leaves them at these defaults; normalize_lo() fills them.
    layer: str | None = None                   # logged_out | logged_in (| rented later)
    strategy: str | None = None                # lo_post_gql, li_clips, ...
    partial: bool = False                      # no views (and counts are not hidden), or only `view_count`
    missing: list[str] = field(default_factory=list)   # of: views, caption, duration, video_url
    taken_at_derived: bool = False             # taken_at was missing and comes from the pk

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AccountIn:
    pk: str
    username: str
    full_name: str | None = None
    biography: str | None = None
    bio_links: list[dict] = field(default_factory=list)
    external_url: str | None = None
    category: str | None = None
    is_private: bool = False
    is_verified: bool = False
    followers: int | None = None
    following: int | None = None
    media_count: int | None = None
    profile_pic_url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================================================== normalize

def _views(m: dict) -> tuple[int | None, str | None]:
    for fld in ("play_count", "ig_play_count", "view_count"):
        v = _int(m.get(fld))
        if v is not None and v >= 0:
            return v, fld
    return None, None


def _audio(a: Any, owner_pk: str) -> dict:
    if not isinstance(a, dict):
        return {"type": "unknown", "title": None, "artist": None, "music_canonical_id": None, "is_reused": False}
    at = (a.get("audio_type") or "").lower()
    if at in ("licensed_music", "licensed"):
        typ = "licensed"
    elif at in ("original_sounds", "original"):
        typ = "original"
    else:
        typ = "unknown"
    title = _str(a.get("music_title") if typ == "licensed" else (a.get("original_sound_title") or a.get("music_title")),
                 300)
    reused = bool(a.get("is_reused")) if typ == "original" else False
    return {"type": typ, "title": title, "artist": _str(a.get("music_artist"), 300),
            "music_canonical_id": _str(a.get("music_canonical_id"), 64), "is_reused": reused}


def normalize(m: dict, account_pk: str) -> ReelIn | None:
    """IgMedia (5.5) -> ReelIn for the account with pk `account_pk`, or None when it is someone else's reel (a
    suggested reel) or not a video. Raises IgParseError when ids are not digit strings or the shape is broken."""
    if not isinstance(m, dict):
        raise IgParseError("media must be an object")
    account_pk = _pk_str(account_pk, "account pk")
    pk = _pk_str(m.get("pk"), "pk")
    id_str = m.get("id")
    if id_str is not None and not isinstance(id_str, str):
        raise IgParseError("id must be a string")
    owner = m.get("owner") if isinstance(m.get("owner"), dict) else {}
    owner_pk = _pk_str(owner.get("pk"), "owner.pk", required=False)
    if owner_pk is None and isinstance(id_str, str) and "_" in id_str:
        owner_pk = _pk_str(id_str.split("_", 1)[1], "id owner part", required=False)
    if owner_pk is None:
        raise IgParseError("owner.pk is missing")
    co = []
    for c in m.get("coauthor_producers") or []:
        if isinstance(c, dict):
            cpk = _pk_str(c.get("pk"), "coauthor pk", required=False)
            if cpk:
                co.append((cpk, _str(c.get("username"), 30)))
    co_pks = [c[0] for c in co]
    if owner_pk == account_pk:
        role = "owner"
    elif account_pk in co_pks:
        role = "coauthor"
    else:
        return None  # suggested reel from someone else (5.4 ig_pull step 3)
    mt = _int(m.get("media_type"))
    if not (m.get("product_type") == "clips" or mt == 2):
        return None

    # pk cross-checks (5.5, 5.8): the id string wins over pk inside the extension; here the shortcode wins.
    conflict = bool(m.get("pk_conflict"))
    if isinstance(id_str, str) and id_str.split("_", 1)[0] != pk:
        conflict = True
        idpk = id_str.split("_", 1)[0]
        if PK_RE.match(idpk):
            pk = idpk
    code = m.get("code")
    if code is not None:
        code = _str(code)
        derived = shortcode_to_pk(code)
        if derived != pk:
            conflict = True
            pk = derived
    else:
        code = pk_to_shortcode(pk)

    hidden = bool(m.get("like_and_view_counts_disabled"))
    views, vfield = _views(m)
    likes = _int(m.get("like_count"))
    if likes is not None and likes < 0:
        likes = None
        hidden = True
    if hidden:
        views, vfield, likes = None, None, None
    comments = None if m.get("comments_disabled") else _int(m.get("comment_count"))
    if comments is not None and comments < 0:
        comments = None
    shares = _int(m.get("reshare_count"))
    if shares is None:
        shares = _int(m.get("share_count"))
    reposts = _int(m.get("media_repost_count"))

    pinned_ids = [str(x) for x in (m.get("clips_tab_pinned_user_ids") or []) if isinstance(x, (str, int))]
    pinned_ids += [str(x) for x in (m.get("timeline_pinned_user_ids") or []) if isinstance(x, (str, int))]
    is_pinned = account_pk in pinned_ids

    ts = _int(m.get("taken_at"))
    taken = datetime.fromtimestamp(ts, tz=timezone.utc) if ts and ts > 0 else None
    video = m.get("video") if isinstance(m.get("video"), dict) else {}
    cover = m.get("cover") if isinstance(m.get("cover"), dict) else {}
    vurl = _https(video.get("url"))
    caption = m.get("caption")
    if isinstance(caption, dict):  # raw object slipped through; take its text
        caption = caption.get("text")
    return ReelIn(
        ig_pk=pk, code=code, url=f"https://www.instagram.com/reel/{code}/", owner_pk=owner_pk,
        owner_username=_str(owner.get("username"), 30), role=role, is_collab=bool(co), coauthors=[c[1] for c in co if c[1]],
        coauthor_pks=co_pks, taken_at=taken, caption=(_str(caption) or "")[:CAPTION_MAX],
        duration=_float(m.get("video_duration")), has_audio=_bool(m.get("has_audio")), is_pinned=is_pinned,
        is_paid_partnership=bool(m.get("is_paid_partnership")), audio=_audio(m.get("audio"), owner_pk),
        views=views, likes=likes, comments=comments, views_field=vfield, shares=shares, reposts=reposts,
        counts_hidden=hidden, video_url=vurl, video_width=_int(video.get("width")), cover_url=_https(cover.get("url")),
        video_url_expires_at=cdn_expiry(vurl), pk_conflict=conflict, product_type=_str(m.get("product_type"), 40),
    )


def parse_user(u: dict) -> AccountIn:
    """IgUser (5.4) -> AccountIn. Also accepts a raw web_profile_info `data.user` or users/<id>/info `user`."""
    if not isinstance(u, dict):
        raise IgParseError("user must be an object")
    pk = u.get("pk", u.get("id"))
    pk = _pk_str(pk, "user pk")
    username = _str(u.get("username"), 60)
    if not username or not HANDLE_RE.match(username.lower()):
        raise IgParseError("bad username")

    def count(*keys):
        for k in keys:
            v = u.get(k)
            if isinstance(v, dict):
                v = v.get("count")
            v = _int(v)
            if v is not None:
                return v
        return None

    links = []
    for b in (u.get("bio_links") or [])[:10]:
        if isinstance(b, dict) and _https(b.get("url")):
            links.append({"url": _https(b.get("url")), "title": _str(b.get("title"), 100) or ""})
    return AccountIn(
        pk=pk, username=username.lower(), full_name=_str(u.get("full_name"), 200),
        biography=_str(u.get("biography"), 2000), bio_links=links, external_url=_https(u.get("external_url")),
        category=_str(u.get("category") or u.get("category_name"), 100), is_private=bool(u.get("is_private")),
        is_verified=bool(u.get("is_verified")),
        followers=count("followers", "follower_count", "edge_followed_by"),
        following=count("following", "following_count", "edge_follow"),
        media_count=count("media_count", "all_media_count", "edge_owner_to_timeline_media"),
        profile_pic_url=_https(u.get("profile_pic_url")),
    )


# ============================================================================================== logged-out items

def pk_time(pk: str | int | None) -> datetime | None:
    """The post time that sits inside a media pk: (pk >> 23) + IG_EPOCH_MS milliseconds (DESIGN-FETCH.md 2.2).
    None when the pk does not look like a media pk of 2011 or later."""
    try:
        n = int(pk)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    ms = (n >> 23) + IG_EPOCH_MS
    if n < (1 << 40) or ms > 4102444800000:      # tiny test pks, or a date after 2100
        return None
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def is_partial(views: int | None, views_field: str | None, counts_hidden: bool) -> bool:
    """A reel is partial when it has no view count although counts are public, or only the logged-out
    `view_count` (the logged-in layer reads `play_count`). DESIGN-FETCH.md 2.2."""
    if counts_hidden:
        return False
    return views is None or views_field == "view_count"


def _gql_to_v1(it: dict) -> dict:
    """LoItem shape "gql" (DESIGN-FETCH.md 4.2) -> the IgMedia fields normalize() reads. Ids stay digit strings."""
    owner = it.get("owner") if isinstance(it.get("owner"), dict) else {}
    dims = it.get("dimensions") if isinstance(it.get("dimensions"), dict) else {}
    pk = it.get("id")
    owner_id = owner.get("id")
    co = []
    for c in it.get("coauthors") or []:
        if isinstance(c, dict):
            co.append({"pk": c.get("id", c.get("pk")), "username": c.get("username")})
    plays, views = _int(it.get("video_play_count")), _int(it.get("video_view_count"))
    vurl = it.get("video_url")
    cover = it.get("display_url")
    return {
        "pk": pk, "id": None, "code": it.get("shortcode"), "taken_at": it.get("taken_at_timestamp"),
        "product_type": it.get("product_type") or "clips", "media_type": 2 if it.get("is_video") else 1,
        "caption": it.get("caption"),
        "play_count": plays if plays is not None and plays >= 0 else None,
        "view_count": views if views is not None and views >= 0 else None,
        "like_count": it.get("like_count"), "comment_count": it.get("comment_count"),
        "like_and_view_counts_disabled": bool(it.get("like_and_view_counts_disabled")),
        "comments_disabled": bool(it.get("comments_disabled")), "video_duration": it.get("video_duration"),
        "has_audio": it.get("has_audio"),
        "timeline_pinned_user_ids": [owner_id] if it.get("pinned") and isinstance(owner_id, str) else [],
        "coauthor_producers": co,
        "video": {"url": vurl, "width": dims.get("width"), "height": dims.get("height")} if vurl else None,
        "cover": {"url": cover, "width": dims.get("width"), "height": dims.get("height")} if cover else None,
        "owner": {"pk": owner_id, "username": owner.get("username")},
    }


def normalize_lo(item: dict, account_pk: str, *, layer: str = "logged_out") -> ReelIn | None:
    """One logged-out item (LoItem, DESIGN-FETCH.md 4.2) -> ReelIn, or None for someone else's reel or a photo.

    shape "v1":  the IgMedia fields of 5.5 (reels tab nodes, post and media detail), through normalize().
    shape "gql": a trimmed legacy node (web_profile_info timeline, embed page): pk = id, code = shortcode,
                 views = video_play_count ?? video_view_count.
    Missing numbers stay None (never 0). A missing taken_at comes from the pk. Raises IgParseError for an unknown
    shape or strategy and for ids that are not digit strings."""
    if not isinstance(item, dict):
        raise IgParseError("item must be an object")
    shape, strategy = item.get("shape"), item.get("strategy")
    if shape not in LO_SHAPES:
        raise IgParseError("unknown item shape")
    if strategy not in LO_STRATEGIES:
        raise IgParseError("unknown strategy")
    if shape == "gql":
        if not item.get("is_video"):
            _pk_str(item.get("id"), "id")           # still refuse a JSON number here
            return None
        if item.get("shortcode") is None:
            raise IgParseError("shortcode is missing")
        m = _gql_to_v1(item)
    else:
        m = item
    ri = normalize(m, account_pk)
    if ri is None:
        return None
    ri.layer, ri.strategy = layer, strategy
    if ri.taken_at is None:
        ri.taken_at = pk_time(ri.ig_pk)
        ri.taken_at_derived = ri.taken_at is not None
    ri.partial = is_partial(ri.views, ri.views_field, ri.counts_hidden)
    missing = []
    if ri.views is None and not ri.counts_hidden:
        missing.append("views")
    if not ri.caption and m.get("caption") is None:
        missing.append("caption")
    if ri.duration is None:
        missing.append("duration")
    if ri.video_url is None:
        missing.append("video_url")
    ri.missing = missing
    return ri


# ============================================================================================== mirrors of the extension

def quote_bigints(text: str) -> str:
    """Python twin of bigint.js quoteBigInts: quote every 16+ digit integer that is an object key's value."""
    return BIGINT_RE.sub(r'\1"\2"', text)


def parse_ig_json(text: str) -> Any:
    """What the extension's parseIg(text) gives: JSON with every 16+ digit id as a string."""
    return json.loads(quote_bigints(text))


def _sid(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return str(v)


def _smallest(versions: Any, min_w: int = 480) -> dict | None:
    vs = [v for v in (versions or []) if isinstance(v, dict) and v.get("url")]
    if not vs:
        return None
    ok = [v for v in vs if (_int(v.get("width")) or 0) >= min_w]
    pick = min(ok, key=lambda v: _int(v.get("width")) or 0) if ok else max(vs, key=lambda v: _int(v.get("width")) or 0)
    return {"url": pick.get("url"), "width": _int(pick.get("width")), "height": _int(pick.get("height"))}


def trim_media(m: dict) -> dict:
    """Python twin of trim.js trimMedia: raw Instagram media (after parse_ig_json) -> IgMedia (5.5)."""
    pk = _sid(m.get("pk"))
    id_s = _sid(m.get("id"))
    conflict = False
    for alt in (m.get("pk_id"), (m.get("strong_id__") or "").split("_")[0] or None):
        if alt is not None and pk is not None and str(alt) != pk:
            conflict = True
    if id_s and "_" in id_s and pk and id_s.split("_")[0] != pk:
        conflict = True
        pk = id_s.split("_")[0]
    cap = m.get("caption")
    text = cap.get("text") if isinstance(cap, dict) else (cap if isinstance(cap, str) else None)
    owner = m.get("user") or m.get("owner") or {}
    cm = m.get("clips_metadata") or {}
    mi = (cm.get("music_info") or {}).get("music_asset_info") or {}
    osi = cm.get("original_sound_info") or {}
    artist_pk = _sid((osi.get("ig_artist") or {}).get("pk"))
    owner_pk = _sid(owner.get("pk"))
    video = _smallest(m.get("video_versions"))
    if video is None:
        for c in m.get("carousel_media") or []:
            video = _smallest(c.get("video_versions"))
            if video:
                break
    cover = _smallest((m.get("image_versions2") or {}).get("candidates"))
    audio = None
    if cm:
        audio = {"audio_type": cm.get("audio_type"), "music_title": mi.get("title"),
                 "music_artist": mi.get("display_artist"),
                 "music_canonical_id": _sid(cm.get("music_canonical_id")) or _sid(mi.get("audio_cluster_id")),
                 "original_sound_title": osi.get("original_audio_title"),
                 "is_reused": bool(osi) and artist_pk is not None and artist_pk != owner_pk}

    def g(k):
        return m.get(k) if k in m else None

    return {
        "pk": pk, "id": id_s, "code": m.get("code"), "taken_at": g("taken_at"), "product_type": g("product_type"),
        "media_type": g("media_type"), "pk_conflict": conflict, "caption": (text or "")[:CAPTION_MAX] if text else None,
        "play_count": g("play_count"), "ig_play_count": g("ig_play_count"), "view_count": g("view_count"),
        "fb_play_count": g("fb_play_count"), "like_count": g("like_count"), "comment_count": g("comment_count"),
        "like_and_view_counts_disabled": bool(g("like_and_view_counts_disabled")),
        "comments_disabled": bool(g("comments_disabled")), "reshare_count": g("reshare_count"),
        "share_count": g("share_count"), "media_repost_count": g("media_repost_count"),
        "video_duration": g("video_duration"), "has_audio": g("has_audio"),
        "clips_tab_pinned_user_ids": [str(x) for x in (m.get("clips_tab_pinned_user_ids") or [])],
        "timeline_pinned_user_ids": [str(x) for x in (m.get("timeline_pinned_user_ids") or [])],
        "coauthor_producers": [{"pk": _sid(c.get("pk")), "username": c.get("username")}
                               for c in (m.get("coauthor_producers") or []) if isinstance(c, dict)],
        "is_paid_partnership": bool(g("is_paid_partnership")), "audio": audio, "video": video, "cover": cover,
        "owner": {"pk": owner_pk, "username": owner.get("username")},
    }


def trim_user(u: dict) -> dict:
    """Python twin of trim.js trimUser: raw web_profile_info / info / topsearch user -> IgUser (5.4)."""
    def count(*keys):
        for k in keys:
            v = u.get(k)
            if isinstance(v, dict):
                v = v.get("count")
            if v is not None:
                return v
        return None

    return {
        "pk": _sid(u.get("pk") if u.get("pk") is not None else u.get("id")), "username": u.get("username"),
        "full_name": u.get("full_name"), "biography": u.get("biography"),
        "bio_links": [{"url": b.get("url"), "title": b.get("title") or ""} for b in (u.get("bio_links") or [])
                      if isinstance(b, dict) and b.get("url")],
        "external_url": u.get("external_url"), "category": u.get("category_name") or u.get("category"),
        "is_private": bool(u.get("is_private")), "is_verified": bool(u.get("is_verified")),
        "followers": count("follower_count", "edge_followed_by"), "following": count("following_count", "edge_follow"),
        "media_count": count("media_count", "all_media_count", "edge_owner_to_timeline_media"),
        "profile_pic_url": u.get("profile_pic_url"),
    }


def trim_gql(n: dict, strategy: str = "lo_web_profile") -> dict:
    """Python twin of trim.js trimGql: a raw legacy GraphQL node (web_profile_info timeline edge, embed page
    `shortcode_media`, after parse_ig_json) -> LoItem shape "gql" (DESIGN-FETCH.md 4.2)."""
    def count(*keys):
        for k in keys:
            v = n.get(k)
            if isinstance(v, dict) and v.get("count") is not None:
                return v.get("count")
        return None

    cap = None
    for e in ((n.get("edge_media_to_caption") or {}).get("edges") or []):
        t = ((e or {}).get("node") or {}).get("text")
        if isinstance(t, str):
            cap = t[:CAPTION_MAX]
            break
    owner = n.get("owner") or {}
    dims = n.get("dimensions") or {}

    def g(k):
        return n.get(k) if k in n else None

    return {
        "shape": "gql", "strategy": strategy, "id": _sid(n.get("id")), "shortcode": n.get("shortcode"),
        "is_video": bool(n.get("is_video")), "product_type": g("product_type"),
        "taken_at_timestamp": g("taken_at_timestamp"), "caption": cap,
        "video_view_count": g("video_view_count"), "video_play_count": g("video_play_count"),
        "like_count": count("edge_liked_by", "edge_media_preview_like"),
        "comment_count": count("edge_media_to_comment", "edge_media_to_parent_comment"),
        "like_and_view_counts_disabled": bool(g("like_and_view_counts_disabled")),
        "comments_disabled": bool(g("comments_disabled")), "video_duration": g("video_duration"),
        "has_audio": g("has_audio"), "video_url": g("video_url"), "display_url": g("display_url"),
        "dimensions": {"height": dims.get("height"), "width": dims.get("width")},
        "owner": {"id": _sid(owner.get("id")), "username": owner.get("username")},
        "pinned": bool(n.get("pinned_for_users")),
        "coauthors": [{"id": _sid(c.get("id") if c.get("id") is not None else c.get("pk")),
                       "username": c.get("username")}
                      for c in (n.get("coauthor_producers") or []) if isinstance(c, dict)],
    }


def page_items(page: dict, source: str = "clips") -> list[dict]:
    """Raw media objects of a clips/user (`items[].media`) or feed/user (`items[]`) page."""
    items = page.get("items") or []
    if source == "clips":
        return [it.get("media") for it in items if isinstance(it, dict) and isinstance(it.get("media"), dict)]
    return [it for it in items if isinstance(it, dict)]
