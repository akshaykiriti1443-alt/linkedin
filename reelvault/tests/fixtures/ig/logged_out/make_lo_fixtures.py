"""Make the LOGGED-OUT Instagram fixtures in this folder (DESIGN-FETCH.md section 9).

    python tests/fixtures/ig/logged_out/make_lo_fixtures.py     # rewrites every *.json / *.html here

These are the answers instagram.com gives a visitor who is NOT signed in (no cookies). The extension tests'
fake Instagram server (tests/ext/fake_ig_server.py) should build its logged-out answers with the same
functions (import this file the way it imports make_fixtures.py), so EXT and SERVER code see the same shapes.

Every shape is marked in manifest.json:
  "source"  = the field names and nesting are read from the current source of instaloader / yt-dlp / gallery-dl
              (November 2026). Still never seen live from this machine.
  "guess"   = best guess. No scraper source confirms it for a logged-out visitor.

Everything is fictional and deterministic (same seed and clock as make_fixtures.py).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("rv_mkfx_base", HERE.parent / "make_fixtures.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

LSD = "AVqFixtureLsd01"                       # the page's anti-forgery token for /api/graphql (not a login secret)
DOC_IDS = {                                   # DESIGN-FETCH.md 3.2; the server can replace them in hello.fetch.doc_ids
    "lo_posts_gql": ["28904005159232689"],                      # instaloader: PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery_connection
    "lo_reels_gql": ["29285972954340351", "7845543455542541"],  # gallery-dl, instaloader: PolarisProfileReelsTabContentQuery_connection
    "lo_post_gql": ["27128499623469141"],                       # instaloader: post by shortcode
    "lo_media_gql": ["27130156389949648"],                      # yt-dlp: PolarisLoggedOutDesktopWWWPostRootContentQuery
}
WAIT_BODY = {"message": "Please wait a few minutes before you try again.", "require_login": True, "status": "fail"}
LOGIN_BODY = {"message": "login_required", "require_login": True, "status": "fail"}
GQL_ERROR = {"data": None, "errors": [{"message": "execution error", "severity": "CRITICAL", "code": 1675030,
                                       "summary": "Query error", "description": "Error performing query."}],
             "extensions": {"is_final": True}}


def page_id(u: dict) -> str:
    """xig_user_by_username.id: the page's own id. It is NOT the user pk (instaloader says so)."""
    return str(17841400000000000 + u["pk"])


def lo_cursor(offset: int, salt: str = "lo") -> str:
    return mk.cursor_for(offset, salt)


# ============================================================================================== media shapes

def photo(owner: dict, taken_at: int, seq: int) -> dict:
    """A photo post: it shows up in the profile timeline and must be skipped (not a reel)."""
    m = mk.media(owner, taken_at, seq, views=None, likes=1200 + seq, comments=14, product_type="feed", media_type=1)
    for k in ("video_versions", "video_duration", "has_audio", "play_count", "ig_play_count", "clips_metadata"):
        m.pop(k, None)
    return m


def lo_media(m: dict, *, views: str = "both") -> dict:
    """Post detail for a logged-out visitor: the private-API media shape, without viewer-only keys.
    views: both | view_count | none (instaloader keeps a fallback for "no counts at all", so test all three)."""
    drop = {"ig_play_count", "fb_play_count", "reshare_count", "media_repost_count", "can_viewer_reshare",
            "can_viewer_save", "photo_of_you", "sharing_friction_info", "mezql_token", "should_request_ads"}
    out = {k: v for k, v in m.items() if k not in drop}
    plays = m.get("play_count")
    out.pop("play_count", None)
    if views == "both":
        out["play_count"] = plays
        out["view_count"] = int(plays * 0.62) if plays is not None else None
    elif views == "view_count":
        out["view_count"] = int(plays * 0.62) if plays is not None else None
    else:
        out["view_count"] = None
    return out


def reels_tab_media(m: dict) -> dict:
    """A reels-tab node: sparse. Verified keys: code, play_count (instaloader reads both). The rest is a guess.
    No caption, no taken_at, no video_versions, no video_duration."""
    return {"pk": m["pk"], "id": m["id"], "code": m["code"], "media_type": 2, "product_type": "clips",
            "play_count": m.get("play_count"), "view_count": None, "like_count": m.get("like_count"),
            "comment_count": m.get("comment_count"), "like_and_view_counts_disabled": False,
            "user": {"pk": m["user"]["pk"], "id": str(m["user"]["pk"])},
            "image_versions2": {"candidates": m["image_versions2"]["candidates"][:2]},
            "clips_tab_pinned_user_ids": m.get("clips_tab_pinned_user_ids") or [],
            "original_height": 1920, "original_width": 1080}


def stub(m: dict, *, codes_only: bool = False) -> dict:
    """A profile-timeline node as the logged-out page embeds it. Only `code` is verified ("the nodes lack most
    of the post's metadata"). pk / media_type / product_type are a guess: use them only when present."""
    if codes_only:
        return {"code": m["code"]}
    return {"code": m["code"], "pk": str(m["pk"]), "id": m["id"], "media_type": m["media_type"],
            "product_type": m.get("product_type")}


def gql_node(m: dict, *, views: bool = True) -> dict:
    """Legacy GraphQL node (web_profile_info timeline edges, old embed pages). Ids are strings here."""
    cap = (m.get("caption") or {}).get("text")
    cands = m["image_versions2"]["candidates"]
    vv = m.get("video_versions") or []
    return {
        "__typename": "GraphVideo" if m["media_type"] == 2 else "GraphImage", "id": str(m["pk"]),
        "shortcode": m["code"], "dimensions": {"height": 1920, "width": 1080}, "display_url": cands[0]["url"],
        "thumbnail_src": cands[-1]["url"], "is_video": m["media_type"] == 2,
        "video_url": vv[0]["url"] if vv else None, "has_audio": m.get("has_audio"),
        "video_view_count": (m.get("play_count") if views else None) if m["media_type"] == 2 else None,
        "video_duration": m.get("video_duration"), "product_type": m.get("product_type"),
        "taken_at_timestamp": m["taken_at"],
        "edge_media_to_caption": {"edges": [{"node": {"text": cap}}] if cap else []},
        "edge_media_to_comment": {"count": m.get("comment_count")},
        "edge_liked_by": {"count": m.get("like_count")}, "edge_media_preview_like": {"count": m.get("like_count")},
        "comments_disabled": False, "pinned_for_users": [], "coauthor_producers": [],
        "owner": {"id": str(m["user"]["pk"]), "username": m["user"]["username"]},
    }


# ============================================================================================== pages (HTML)

def _sjs(key: str, data: dict) -> str:
    """One embedded query result, wrapped the way instagram.com embeds it (yt-dlp walks exactly this path;
    instaloader takes every __bbox.result.data it finds)."""
    blob = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [[
        "RelayPrefetchedStreamCache", "next", [],
        [key, {"__bbox": {"complete": True, "result": {"data": data, "extensions": {"is_final": True}}}}]]]}}]]]}
    return '<script type="application/json" data-content-len="1" data-sjs>' + json.dumps(blob, separators=(",", ":")) + "</script>"


def _html(title: str, body: str, *, lsd: str | None = LSD) -> str:
    tok = ('<script type="application/json" data-sjs>' + json.dumps({"require": [["ScheduledServerJS", "handle",
           None, [{"__bbox": {"define": [["LSD", [], {"token": lsd}, 323]]}}]]]}, separators=(",", ":")) + "</script>") if lsd else ""
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>' + title + "</title></head><body>"
            + tok + body + "</body></html>")


def xig_user(u: dict, *, followers: int, media_count: int = 640, private: bool = False) -> dict:
    s = mk.user_short(u)
    return {"pk": str(u["pk"]), "id": page_id(u), "username": u["username"], "full_name": u["full_name"],
            "is_private": private, "is_verified": True, "biography": "Helping agency owners get clients with ads",
            "follower_count": followers, "following_count": 412, "all_media_count": media_count,
            "category": "Entrepreneur", "is_business": True, "profile_pic_url": s["profile_pic_url"],
            "hd_profile_pic_url_info": {"url": s["profile_pic_url"].replace("s150x150", "s320x320")}}


def timeline_connection(medias: list[dict], offset: int, size: int = 12, *, codes_only: bool = False) -> dict:
    part = medias[offset:offset + size]
    more = offset + size < len(medias)
    return {"edges": [{"node": stub(m, codes_only=codes_only), "cursor": lo_cursor(offset + i + 1)}
                      for i, m in enumerate(part)],
            "page_info": {"end_cursor": lo_cursor(offset + size) if more else None, "has_next_page": more}}


def reels_connection(reels: list[dict], offset: int, size: int = 12) -> dict:
    part = reels[offset:offset + size]
    more = offset + size < len(reels)
    return {"edges": [{"node": {"media": reels_tab_media(m), "__typename": "XDTClipsItemDict"},
                       "cursor": lo_cursor(offset + i + 1, "lor")} for i, m in enumerate(part)],
            "page_info": {"end_cursor": lo_cursor(offset + size, "lor") if more else None, "has_next_page": more,
                          "has_previous_page": False, "start_cursor": None}}


def profile_page_html(u: dict, medias: list[dict], *, followers: int = 182_000, private: bool = False,
                      codes_only: bool = False, embedded: bool = True) -> str:
    """GET /<handle>/ without cookies. Two embedded queries: the profile, then its first 12 posts."""
    title = f"{u['full_name']} (@{u['username']}) &bull; Instagram photos and videos"
    if not embedded:   # the page loads but embeds no data (what a non-navigation fetch may get): partial
        return _html(title, f'<div id="mount"></div>"profile_id":"{u["pk"]}"')
    user = xig_user(u, followers=followers, private=private)
    tl = {"pk": user["pk"], "id": user["id"],
          "polaris_ordered_timeline_connection": ({"edges": [], "page_info": {"end_cursor": None,
                                                                              "has_next_page": False}}
                                                  if private else timeline_connection(medias, 0,
                                                                                      codes_only=codes_only))}
    return _html(title, _sjs("adp_PolarisLoggedOutDesktopWWWProfilePageContentQuery_1", {"xig_user_by_username": user})
                 + _sjs("adp_PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery_1", {"xig_user_by_username": tl})
                 + f'"profile_id":"{u["pk"]}"')


def reels_page_html(u: dict, reels: list[dict], *, followers: int = 182_000) -> str:
    """GET /<handle>/reels/ without cookies. GUESS: no source confirms the reels tab embeds its first page."""
    title = f"{u['full_name']} (@{u['username']}) &bull; Instagram reels"
    return _html(title, _sjs("adp_PolarisLoggedOutDesktopWWWProfilePageContentQuery_1",
                             {"xig_user_by_username": xig_user(u, followers=followers)})
                 + _sjs("adp_PolarisProfileReelsTabContentQuery_1",
                        {"xdt_api__v1__clips__user__connection_v2": reels_connection(reels, 0)})
                 + f'"profile_id":"{u["pk"]}"')


def login_page_html() -> str:
    return _html("Login &bull; Instagram", '<form id="loginForm"><input name="username"></form>', lsd=None)


def embed_html(m: dict, *, with_data: bool = True) -> str:
    """GET /reel/<code>/embed/captioned/. GUESS (legacy shape; current yt-dlp no longer reads it)."""
    data = ("<script>window.__additionalDataLoaded('extra'," + json.dumps({"shortcode_media": gql_node(m)}, separators=(",", ":"))
            + ");</script>") if with_data else '<a class="EmbedVideo" href="#">Watch on Instagram</a>'
    return _html("Instagram", '<div class="Embed">' + data + "</div>", lsd=None)


# ============================================================================================== JSON answers

def posts_gql(u: dict, medias: list[dict], offset: int, *, codes_only: bool = False) -> dict:
    return {"data": {"node": {"__typename": "XDTUserDict", "id": page_id(u),
                              "polaris_ordered_timeline_connection": timeline_connection(medias, offset,
                                                                                         codes_only=codes_only)}},
            "extensions": {"is_final": True}}


def reels_gql(reels: list[dict], offset: int, *, wrapped: bool = False) -> dict:
    conn = reels_connection(reels, offset)
    data = ({"fetch__XDTUserDict": {"id": "x", "xdt_api__v1__clips__user__connection_v2": conn}} if wrapped
            else {"xdt_api__v1__clips__user__connection_v2": conn})
    return {"data": data, "extensions": {"is_final": True}, "status": "ok"}


def post_gql(m: dict | None, *, views: str = "both") -> dict:
    return {"data": {"xdt_api__v1__media__shortcode__web_info": {"items": [lo_media(m, views=views)] if m else []}},
            "extensions": {"is_final": True}}


def media_gql(m: dict | None, *, views: str = "view_count") -> dict:
    inner = lo_media(m, views=views) if m else None
    return {"data": {"xig_polaris_media": {"id": str(m["pk"]) if m else "0", "if_not_gated_logged_out": inner}},
            "extensions": {"is_final": True}}


def web_profile_info(u: dict, medias: list[dict], *, followers: int = 182_000, n: int = 12) -> dict:
    d = mk.web_profile_info(u, followers=followers)
    more = len(medias) > n
    d["data"]["user"]["edge_owner_to_timeline_media"] = {
        "count": 640, "page_info": {"has_next_page": more, "end_cursor": lo_cursor(n, "low") if more else None},
        "edges": [{"node": gql_node(m)} for m in medias[:n]]}
    return d


def mixed_timeline(n: int = 40) -> tuple[list[dict], list[dict]]:
    """(timeline with a photo at every 4th place, the reels only), newest first."""
    reels = mk.account_timeline(n)
    tl = []
    for i, m in enumerate(reels):
        if i % 4 == 3:
            tl.append(photo(mk.TARGET, m["taken_at"] + 600, 500 + i))
        tl.append(m)
    return tl, reels


# ============================================================================================== upload example

def upload_example(reels: list[dict]) -> tuple[dict, list[dict]]:
    """The exact body of POST /jobs/<id>/items for logged-out items (DESIGN-FETCH.md 5.2), and what the server
    must make of each item."""
    spec = importlib.util.spec_from_file_location("rv_igparse_fx", HERE.parents[3] / "reelvault" / "igparse.py")
    ig = importlib.util.module_from_spec(spec)
    import sys
    sys.modules["rv_igparse_fx"] = ig
    spec.loader.exec_module(ig)
    full = ig.trim_media(ig.parse_ig_json(mk.dumps(lo_media(reels[0], views="both"))))
    noviews = ig.trim_media(ig.parse_ig_json(mk.dumps(lo_media(reels[1], views="none"))))
    sparse = ig.trim_media(ig.parse_ig_json(mk.dumps(reels_tab_media(reels[2]))))
    g = gql_node(reels[3])
    gql = {"id": g["id"], "shortcode": g["shortcode"], "is_video": True, "product_type": "clips",
           "taken_at_timestamp": g["taken_at_timestamp"],
           "caption": (g["edge_media_to_caption"]["edges"][0]["node"]["text"] or "")[:5000],
           "video_view_count": g["video_view_count"], "video_play_count": None,
           "like_count": g["edge_liked_by"]["count"], "comment_count": g["edge_media_to_comment"]["count"],
           "like_and_view_counts_disabled": False, "comments_disabled": False,
           "video_duration": g["video_duration"], "has_audio": g["has_audio"],
           "video_url": g["video_url"], "display_url": g["display_url"],
           "dimensions": g["dimensions"], "owner": g["owner"], "pinned": False, "coauthors": []}
    body = {"layer": "logged_out",
            "account": {"pk": str(mk.TARGET["pk"]), "username": mk.TARGET["username"],
                        "full_name": mk.TARGET["full_name"], "biography": "Helping agency owners get clients with ads",
                        "bio_links": [], "external_url": None, "category": "Entrepreneur", "is_private": False,
                        "is_verified": True, "followers": 182000, "following": 412, "media_count": 640,
                        "profile_pic_url": mk.user_short(mk.TARGET)["profile_pic_url"]},
            "items": [{"shape": "v1", "strategy": "lo_post_gql", **full},
                      {"shape": "v1", "strategy": "lo_post_gql", **noviews},
                      {"shape": "v1", "strategy": "lo_reels_gql", **sparse},
                      {"shape": "gql", "strategy": "lo_web_profile", **gql}],
            "paging": {"source": "lo", "strategy": "lo_post_gql", "more_available": True, "stage_done": False,
                       "lo_stop": None},
            "fetch_log": [
                {"strategy": "lo_profile_page", "status": "ok", "code": None, "http": 200, "ms": 640, "items": 12,
                 "at": mk.FIXTURE_NOW},
                {"strategy": "lo_reels_gql", "status": "blocked", "code": "login_wall", "http": 401, "ms": 210,
                 "items": 0, "at": mk.FIXTURE_NOW + 9},
                {"strategy": "lo_post_gql", "status": "ok", "code": None, "http": 200, "ms": 480, "items": 1,
                 "at": mk.FIXTURE_NOW + 18}]}

    def iso(ts):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def derived(pk):
        return ((int(pk) >> 23) + mk.IG_EPOCH_MS) // 1000

    exp = [
        {"ig_pk": full["pk"], "code": full["code"], "taken_at": iso(full["taken_at"]), "taken_at_derived": False,
         "views": full["play_count"], "views_field": "play_count", "likes": full["like_count"],
         "comments": full["comment_count"], "partial": False, "missing": [], "read_layer": "logged_out",
         "read_strategy": "lo_post_gql", "has_video_url": True},
        {"ig_pk": noviews["pk"], "code": noviews["code"], "taken_at": iso(noviews["taken_at"]),
         "taken_at_derived": False, "views": None, "views_field": None, "likes": noviews["like_count"],
         "comments": noviews["comment_count"], "partial": True, "missing": ["views"],
         "read_layer": "logged_out", "read_strategy": "lo_post_gql", "has_video_url": True},
        {"ig_pk": sparse["pk"], "code": sparse["code"], "taken_at": iso(derived(sparse["pk"])),
         "taken_at_derived": True, "views": sparse["play_count"], "views_field": "play_count",
         "likes": sparse["like_count"], "comments": sparse["comment_count"], "partial": False,
         "missing": ["caption", "duration", "video_url"], "read_layer": "logged_out",
         "read_strategy": "lo_reels_gql", "has_video_url": False},
        {"ig_pk": gql["id"], "code": gql["shortcode"], "taken_at": iso(gql["taken_at_timestamp"]),
         "taken_at_derived": False, "views": gql["video_view_count"], "views_field": "view_count",
         "likes": gql["like_count"], "comments": gql["comment_count"], "partial": True, "missing": [],
         "read_layer": "logged_out", "read_strategy": "lo_web_profile", "has_video_url": True},
    ]
    return body, exp


PROBE_UPLOAD = {"rows": [
    {"strategy": "lo_profile_page", "layer": "logged_out", "handle": "instagram", "status": "ok", "code": None,
     "http": 200, "ms": 640, "items": 12, "fields": ["user", "followers", "reel_list", "lsd"]},
    {"strategy": "lo_reels_page", "layer": "logged_out", "handle": "instagram", "status": "partial",
     "code": "no_data", "http": 200, "ms": 590, "items": 0, "fields": ["lsd"]},
    {"strategy": "lo_reels_gql", "layer": "logged_out", "handle": "instagram", "status": "blocked",
     "code": "login_wall", "http": 401, "ms": 180, "items": 0, "fields": []},
    {"strategy": "lo_web_profile", "layer": "logged_out", "handle": "instagram", "status": "blocked",
     "code": "rate", "http": 401, "ms": 150, "items": 0, "fields": []},
    {"strategy": "lo_posts_gql", "layer": "logged_out", "handle": "instagram", "status": "ok", "code": None,
     "http": 200, "ms": 420, "items": 12, "fields": ["reel_list"]},
    {"strategy": "lo_post_gql", "layer": "logged_out", "handle": "instagram", "status": "partial",
     "code": "no_views", "http": 200, "ms": 510, "items": 1,
     "fields": ["likes", "comments", "taken_at", "caption", "video_url", "duration"]},
    {"strategy": "lo_media_gql", "layer": "logged_out", "handle": "instagram", "status": "ok", "code": None,
     "http": 200, "ms": 530, "items": 1,
     "fields": ["views", "likes", "comments", "taken_at", "caption", "video_url", "duration"]},
    {"strategy": "lo_embed", "layer": "logged_out", "handle": "instagram", "status": "blocked", "code": "no_data",
     "http": 200, "ms": 300, "items": 0, "fields": []},
    {"strategy": "lo_profile_page", "layer": "logged_out", "handle": "scaleit.demo", "status": "ok", "code": None,
     "http": 200, "ms": 610, "items": 12, "fields": ["user", "followers", "reel_list", "lsd"]},
    {"strategy": "li_session", "layer": "logged_in", "handle": None, "status": "ok", "code": None, "http": 200,
     "ms": 0, "items": 0, "fields": ["signed_in"]},
    {"strategy": "li_clips", "layer": "logged_in", "handle": "scaleit.demo", "status": "ok", "code": None,
     "http": 200, "ms": 700, "items": 12,
     "fields": ["views", "likes", "comments", "taken_at", "caption", "video_url", "duration"]},
]}

MANIFEST = [
    # file, strategy, http status, class, verified
    ("profile_page.html", "lo_profile_page", 200, "success", "source: instaloader structures.py Profile._from_page; "
     "wrapper path from yt-dlp. Stub fields other than `code` are a guess."),
    ("profile_page_codes_only.html", "lo_profile_page", 200, "success", "source (the only verified stub field is code)"),
    ("profile_page_private.html", "lo_profile_page", 200, "private", "source: instaloader (private page shows no posts)"),
    ("profile_page_no_data.html", "lo_profile_page", 200, "partial", "guess: page without embedded data; "
     "profile_id regex is from gallery-dl"),
    ("login_page.html", "any page strategy", 200, "login_wall", "source: final URL /accounts/login/ after a redirect "
     "(yt-dlp, gallery-dl, instaloader)"),
    ("reels_page.html", "lo_reels_page", 200, "success", "guess"),
    ("web_profile_info_ok.json", "lo_web_profile", 200, "success", "source: instagrapi; instaloader says it is often "
     "refused (400/401/429)"),
    ("wait_401.json", "any API strategy", 401, "rate_limit", "source: instaloader/instagrapi (401 + Please wait)"),
    ("require_login_401.json", "any API strategy", 401, "login_wall", "guess for the body; 401/403 is source"),
    ("rate_429.json", "any strategy", 429, "rate_limit", "source: all three"),
    ("posts_gql_page1.json", "lo_posts_gql", 200, "success", "source: instaloader get_posts (logged out)"),
    ("posts_gql_page2_last.json", "lo_posts_gql", 200, "success", "source"),
    ("reels_gql_page1.json", "lo_reels_gql", 200, "success", "shape: source (instaloader get_reels). Logged out: guess"),
    ("reels_gql_page2_last.json", "lo_reels_gql", 200, "success", "same"),
    ("reels_gql_wrapped.json", "lo_reels_gql", 200, "success", "shape: source (gallery-dl fetch__XDTUserDict). "
     "Logged out: guess"),
    ("post_gql_ok.json", "lo_post_gql", 200, "success", "source: instaloader Post._obtain_metadata (logged out)"),
    ("post_gql_no_views.json", "lo_post_gql", 200, "partial", "source hints at it: instaloader keeps a play-count "
     "fallback for this case"),
    ("post_gql_photo.json", "lo_post_gql", 200, "success", "source (a photo: skip it)"),
    ("post_gql_empty.json", "lo_post_gql", 200, "empty", "source: instaloader 'Fetching Post metadata failed'"),
    ("media_gql_ok.json", "lo_media_gql", 200, "success", "source: yt-dlp (reads view_count, not play_count)"),
    ("media_gql_gated.json", "lo_media_gql", 200, "empty", "source: yt-dlp (if_not_gated_logged_out is null)"),
    ("gql_error.json", "any GraphQL strategy", 200, "changed", "source for errors[] (gallery-dl, instagrapi); "
     "exact text is a guess"),
    ("embed_ok.html", "lo_embed", 200, "success", "guess (legacy)"),
    ("embed_no_data.html", "lo_embed", 200, "partial", "guess"),
    ("upload_items.json", "POST /jobs/<id>/items", 200, "contract", "this build's contract"),
    ("expected_reels.json", "server result for upload_items.json", 200, "contract", "this build's contract"),
    ("probe_upload.json", "POST /jobs/<id>/probe", 200, "contract", "this build's contract"),
]


def build_all() -> dict[str, str]:
    tl, reels = mixed_timeline(40)
    u = mk.TARGET
    files: dict[str, str] = {}
    d = mk.dumps
    files["profile_page.html"] = profile_page_html(u, tl)
    files["profile_page_codes_only.html"] = profile_page_html(u, tl, codes_only=True)
    files["profile_page_private.html"] = profile_page_html(u, tl, private=True)
    files["profile_page_no_data.html"] = profile_page_html(u, tl, embedded=False)
    files["login_page.html"] = login_page_html()
    files["reels_page.html"] = reels_page_html(u, reels)
    files["web_profile_info_ok.json"] = d(web_profile_info(u, tl))
    files["wait_401.json"] = d(WAIT_BODY)
    files["require_login_401.json"] = d(LOGIN_BODY)
    files["rate_429.json"] = d({"message": "Please wait a few minutes before you try again.", "status": "fail"})
    files["posts_gql_page1.json"] = d(posts_gql(u, tl[:24], 12))
    files["posts_gql_page2_last.json"] = d(posts_gql(u, tl[:30], 24))
    files["reels_gql_page1.json"] = d(reels_gql(reels[:24], 0))
    files["reels_gql_page2_last.json"] = d(reels_gql(reels[:24], 12))
    files["reels_gql_wrapped.json"] = d(reels_gql(reels[:24], 0, wrapped=True))
    files["post_gql_ok.json"] = d(post_gql(reels[0]))
    files["post_gql_no_views.json"] = d(post_gql(reels[1], views="none"))
    files["post_gql_photo.json"] = d(post_gql(tl[3]))
    files["post_gql_empty.json"] = d(post_gql(None))
    files["media_gql_ok.json"] = d(media_gql(reels[0]))
    files["media_gql_gated.json"] = d(media_gql(None))
    files["gql_error.json"] = d(GQL_ERROR)
    files["embed_ok.html"] = embed_html(reels[0])
    files["embed_no_data.html"] = embed_html(reels[0], with_data=False)
    body, exp = upload_example(reels)
    files["upload_items.json"] = json.dumps(body, ensure_ascii=False, indent=1)
    files["expected_reels.json"] = json.dumps(exp, ensure_ascii=False, indent=1)
    files["probe_upload.json"] = json.dumps(PROBE_UPLOAD, ensure_ascii=False, indent=1)
    files["manifest.json"] = json.dumps(
        {"lsd": LSD, "doc_ids": DOC_IDS, "target": {"pk": str(u["pk"]), "username": u["username"],
                                                    "page_id": page_id(u)},
         "files": [{"file": f, "strategy": s, "http": h, "class": c, "verified": v} for f, s, h, c, v in MANIFEST]},
        ensure_ascii=False, indent=1)
    return files


def main() -> None:
    for name, text in build_all().items():
        (HERE / name).write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
        print("wrote", name)


if __name__ == "__main__":
    main()
