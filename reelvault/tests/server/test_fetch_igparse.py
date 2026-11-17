"""Fetch engine: every logged-out fixture shape becomes the existing reel fields (DESIGN-FETCH.md 4.2, 9).

The fixtures in tests/fixtures/ig/logged_out/ are read as FILES here (not rebuilt), then trimmed the way the
extension uploads them, then normalized. Rules checked for every shape: ids stay text, the shortcode cross-check
runs, a missing number stays None (never 0).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from _fetch_support import LO_DIR, PK, TIMELINE, gql_item, load_json, lo, mk, v1_detail, v1_reel
from reelvault import igparse
from reelvault.server import db

REELS = TIMELINE[:40]                       # the reels behind the fixtures (make_lo_fixtures.mixed_timeline(40))
BY_CODE = {m["code"]: m for m in REELS}


# ---------------------------------------------------------------------------------------------- reading the files

def read(name: str):
    text = (LO_DIR / name).read_text(encoding="utf-8")
    return igparse.parse_ig_json(text) if name.endswith(".json") else text


def embedded(html: str) -> list[dict]:
    """Every `__bbox.result.data` object of a page (what lo_profile_page / lo_reels_page parse)."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            bb = o.get("__bbox")
            if isinstance(bb, dict) and isinstance(bb.get("result"), dict) and isinstance(bb["result"].get("data"), dict):
                out.append(bb["result"]["data"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for blob in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
        walk(igparse.parse_ig_json(blob))
    return out


def page_user(html: str) -> dict | None:
    merged: dict = {}
    for d in embedded(html):
        u = d.get("xig_user_by_username")
        if isinstance(u, dict):
            merged.update({k: v for k, v in u.items() if k != "polaris_ordered_timeline_connection"})
    return merged or None


def connection(data: dict) -> dict | None:
    """The first object under `data` (depth <= 4) with an `edges` list (3.2: lo_reels_gql)."""
    def find(o, depth):
        if isinstance(o, dict):
            if isinstance(o.get("edges"), list):
                return o
            if depth < 4:
                for v in o.values():
                    hit = find(v, depth + 1)
                    if hit:
                        return hit
        return None

    return find(data, 0)


def reel_nodes(data: dict) -> list[dict]:
    conn = connection(data) or {}
    return [e["node"].get("media") or e["node"] for e in conn.get("edges", [])]


def as_v1(media: dict, strategy: str) -> dict:
    return dict(igparse.trim_media(media), shape="v1", strategy=strategy)


def norm(item):
    return igparse.normalize_lo(item, PK)


def true_time(code: str) -> datetime:
    return datetime.fromtimestamp(BY_CODE[code]["taken_at"], tz=timezone.utc)


def check_basics(ri, strategy: str):
    """What holds for every logged-out reel, whatever the shape."""
    assert isinstance(ri.ig_pk, str) and ri.ig_pk.isdigit() and len(ri.ig_pk) == 19          # ids stay text
    assert igparse.shortcode_to_pk(ri.code) == ri.ig_pk and ri.pk_conflict is False          # cross-check
    assert ri.owner_pk == PK and ri.role == "owner"
    assert ri.layer == "logged_out" and ri.strategy == strategy
    for v in (ri.views, ri.likes, ri.comments, ri.shares, ri.reposts):
        assert v is None or (isinstance(v, int) and v > 0)                                   # missing is None, never 0
    assert ri.partial == (ri.views is None or ri.views_field == "view_count")


# ---------------------------------------------------------------------------------------------- the contract example

def test_contract_upload_gives_the_expected_reels():
    up, expected = load_json("upload_items.json"), load_json("expected_reels.json")
    assert len(up["items"]) == len(expected) == 4
    for item, exp in zip(up["items"], expected):
        ri = norm(item)
        got = {"ig_pk": ri.ig_pk, "code": ri.code, "taken_at": db.iso(ri.taken_at),
               "taken_at_derived": ri.taken_at_derived, "views": ri.views, "views_field": ri.views_field,
               "likes": ri.likes, "comments": ri.comments, "partial": ri.partial, "missing": ri.missing,
               "read_layer": ri.layer, "read_strategy": ri.strategy, "has_video_url": ri.video_url is not None}
        assert got == exp
    acc = igparse.parse_user(up["account"])
    assert (acc.pk, acc.username, acc.followers, acc.media_count) == (PK, "scaleit.demo", 182000, 640)


# ---------------------------------------------------------------------------------------------- success shapes

def test_post_detail_with_both_counts_prefers_the_play_count():
    m = read("post_gql_ok.json")["data"]["xdt_api__v1__media__shortcode__web_info"]["items"][0]
    ri = norm(as_v1(m, "lo_post_gql"))
    check_basics(ri, "lo_post_gql")
    src = BY_CODE[ri.code]
    assert (ri.views, ri.views_field, ri.partial, ri.missing) == (src["play_count"], "play_count", False, [])
    assert (ri.likes, ri.comments) == (src["like_count"], src["comment_count"])
    assert ri.taken_at == true_time(ri.code) and ri.taken_at_derived is False
    assert ri.caption == src["caption"]["text"] and ri.duration == src["video_duration"]
    assert ri.video_url.startswith("https://") and ri.video_url_expires_at is not None and ri.cover_url


def test_post_detail_without_views_stays_none_and_is_partial():
    m = read("post_gql_no_views.json")["data"]["xdt_api__v1__media__shortcode__web_info"]["items"][0]
    ri = norm(as_v1(m, "lo_post_gql"))
    check_basics(ri, "lo_post_gql")
    assert ri.views is None and ri.views_field is None and ri.partial is True and ri.missing == ["views"]
    assert ri.likes == BY_CODE[ri.code]["like_count"] and ri.counts_hidden is False


def test_post_detail_of_a_photo_is_dropped():
    m = read("post_gql_photo.json")["data"]["xdt_api__v1__media__shortcode__web_info"]["items"][0]
    assert m["media_type"] == 1
    assert norm(as_v1(m, "lo_post_gql")) is None


def test_media_detail_reads_view_count_and_marks_it_partial():
    m = read("media_gql_ok.json")["data"]["xig_polaris_media"]["if_not_gated_logged_out"]
    ri = norm(as_v1(m, "lo_media_gql"))
    check_basics(ri, "lo_media_gql")
    assert ri.views_field == "view_count" and ri.partial is True and ri.missing == []
    assert ri.views == int(BY_CODE[ri.code]["play_count"] * 0.62)


@pytest.mark.parametrize("name,n", [("reels_gql_page1.json", 12), ("reels_gql_page2_last.json", 12),
                                    ("reels_gql_wrapped.json", 12)])
def test_reels_tab_pages_are_sparse_reels_with_play_counts(name, n):
    nodes = reel_nodes(read(name)["data"])
    assert len(nodes) == n
    for m in nodes:
        ri = norm(as_v1(m, "lo_reels_gql"))
        check_basics(ri, "lo_reels_gql")
        src = BY_CODE[ri.code]
        assert (ri.views, ri.views_field, ri.partial) == (src["play_count"], "play_count", False)
        assert ri.missing == ["caption", "duration", "video_url"] and ri.caption == "" and ri.duration is None
        # the date comes from the pk and matches the real post time to the second
        assert ri.taken_at_derived is True and ri.taken_at == true_time(ri.code)
        assert ri.cover_url and ri.video_url is None


def test_reels_page_html_gives_the_same_sparse_reels():
    data = [d for d in embedded(read("reels_page.html")) if connection(d)]
    nodes = reel_nodes(data[0])
    assert len(nodes) == 12
    got = [norm(as_v1(m, "lo_reels_page")) for m in nodes]
    for ri in got:
        check_basics(ri, "lo_reels_page")
    assert [r.code for r in got] == [m["code"] for m in REELS[:12]]
    assert all(r.views == BY_CODE[r.code]["play_count"] for r in got)


def test_web_profile_info_gives_the_user_and_view_count_reels():
    user = read("web_profile_info_ok.json")["data"]["user"]
    acc = igparse.parse_user(igparse.trim_user(user))
    assert (acc.pk, acc.username, acc.followers, acc.following, acc.is_private) == (PK, "scaleit.demo", 182000, 412,
                                                                                    False)
    nodes = [e["node"] for e in user["edge_owner_to_timeline_media"]["edges"]]
    assert len(nodes) == 12
    reels = []
    for n in nodes:
        item = igparse.trim_gql(n, "lo_web_profile")
        assert item["shape"] == "gql" and isinstance(item["id"], str) and isinstance(item["owner"]["id"], str)
        ri = norm(item)
        if not n["is_video"]:
            assert ri is None                     # the photos of the mixed timeline are dropped
            continue
        check_basics(ri, "lo_web_profile")
        src = BY_CODE[ri.code]
        assert (ri.views, ri.views_field, ri.partial) == (src["play_count"], "view_count", True)
        assert (ri.likes, ri.comments) == (src["like_count"], src["comment_count"])
        assert ri.taken_at == true_time(ri.code) and not ri.taken_at_derived and ri.video_url and ri.cover_url
        assert ri.video_width == 1080 and ri.caption == src["caption"]["text"]
        reels.append(ri)
    assert 8 <= len(reels) < 12


def test_embed_page_gives_one_gql_reel():
    html = read("embed_ok.html")
    m = re.search(r"__additionalDataLoaded\('extra',(\{.*?\})\);", html, re.S)
    node = igparse.parse_ig_json(m.group(1))["shortcode_media"]
    ri = norm(igparse.trim_gql(node, "lo_embed"))
    check_basics(ri, "lo_embed")
    assert ri.views == BY_CODE[ri.code]["play_count"] and ri.views_field == "view_count" and ri.video_url


def test_profile_pages_give_the_user_with_the_real_pk_not_the_page_id():
    for name in ("profile_page.html", "profile_page_codes_only.html", "reels_page.html"):
        u = page_user(read(name))
        assert u["id"] == "17841400025025320" and u["pk"] == PK       # `id` is the page id, never the pk
        acc = igparse.parse_user(igparse.trim_user(u))
        assert (acc.pk, acc.username, acc.followers, acc.following, acc.media_count, acc.is_private) == (
            PK, "scaleit.demo", 182000, 412, 640, False)
    private = igparse.parse_user(igparse.trim_user(page_user(read("profile_page_private.html"))))
    assert private.is_private is True and private.pk == PK


# ---------------------------------------------------------------------------------------------- nothing to upload

def test_stubs_are_never_reels():
    """Profile-timeline stubs (page + lo_posts_gql) only say which posts exist: the server refuses them as items."""
    stubs = []
    for d in embedded(read("profile_page.html")) + embedded(read("profile_page_codes_only.html")):
        tl = (d.get("xig_user_by_username") or {}).get("polaris_ordered_timeline_connection")
        stubs += [e["node"] for e in (tl or {}).get("edges", [])]
    for name in ("posts_gql_page1.json", "posts_gql_page2_last.json"):
        stubs += [e["node"] for e in read(name)["data"]["node"]["polaris_ordered_timeline_connection"]["edges"]]
    assert len(stubs) >= 36 and all("code" in s for s in stubs)
    for s in stubs[:5] + stubs[-5:]:
        with pytest.raises(igparse.IgParseError):
            norm(s)


@pytest.mark.parametrize("name", ["profile_page_no_data.html", "login_page.html", "embed_no_data.html"])
def test_pages_without_data_hold_no_user_and_no_reel(name):
    html = read(name)
    assert page_user(html) is None and not any(connection(d) for d in embedded(html))
    assert "__additionalDataLoaded" not in html


@pytest.mark.parametrize("name,path", [
    ("post_gql_empty.json", ("data", "xdt_api__v1__media__shortcode__web_info", "items")),
    ("media_gql_gated.json", ("data", "xig_polaris_media", "if_not_gated_logged_out")),
])
def test_empty_and_gated_answers_hold_no_item(name, path):
    o = read(name)
    for k in path:
        o = o[k]
    assert not o


@pytest.mark.parametrize("name", ["rate_429.json", "wait_401.json", "require_login_401.json", "gql_error.json"])
def test_block_answers_are_refused_as_items(name):
    """A block body is a fetch_log row for the extension, never an item (the API tests cover the cooldowns)."""
    with pytest.raises(igparse.IgParseError):
        norm(read(name))


def test_every_fixture_file_is_covered_here():
    used = set(re.findall(r'"([a-z0-9_]+\.(?:json|html))"', open(__file__, encoding="utf-8").read()))
    manifest = {f["file"] for f in load_json("manifest.json")["files"]}
    on_disk = {p.name for p in LO_DIR.iterdir() if p.suffix in (".json", ".html")} - {"manifest.json"}
    assert manifest == on_disk
    assert manifest - used == {"probe_upload.json"} - used     # that one is replayed in test_fetch_probe.py


# ---------------------------------------------------------------------------------------------- rules

def test_ids_must_be_text_in_both_shapes():
    m = TIMELINE[0]
    v1 = v1_detail(m)
    assert isinstance(v1["pk"], str)
    with pytest.raises(igparse.IgParseError):
        norm(dict(v1, pk=int(v1["pk"])))
    g = gql_item(m)
    with pytest.raises(igparse.IgParseError):
        norm(dict(g, id=int(g["id"])))
    with pytest.raises(igparse.IgParseError):
        norm(dict(g, owner={"id": int(PK), "username": "scaleit.demo"}))
    with pytest.raises(igparse.IgParseError):
        norm(dict(g, is_video=False, id=int(g["id"])))            # also for a photo that gets dropped


def test_shortcode_cross_check_wins_in_both_shapes():
    a, b = TIMELINE[0], TIMELINE[1]
    for item in (dict(v1_detail(a), pk=str(b["pk"]), id=None), dict(gql_item(a), id=str(b["pk"]))):
        ri = norm(item)
        assert ri.ig_pk == str(a["pk"]) and ri.code == a["code"] and ri.pk_conflict is True
    with pytest.raises(igparse.IgParseError):
        norm(dict(gql_item(a), shortcode="bad code!"))
    with pytest.raises(igparse.IgParseError):
        norm(dict(gql_item(a), shortcode=None))


def test_unknown_shape_or_strategy_is_refused():
    item = v1_detail(TIMELINE[0])
    for bad in (dict(item, shape="v2"), dict(item, shape=None), dict(item, strategy="lo_magic"),
                dict(item, strategy="li_clips"), {k: v for k, v in item.items() if k != "shape"}, "x", None):
        with pytest.raises(igparse.IgParseError):
            norm(bad)


def test_someone_elses_reel_and_coauthor_role():
    other = mk.media(mk.OTHER, TIMELINE[0]["taken_at"], 7, views=5000, likes=50, comments=5)
    assert norm(v1_detail(other)) is None and norm(gql_item(other)) is None
    g = gql_item(other)
    g["coauthors"] = [{"id": PK, "username": "scaleit.demo"}]
    ri = norm(g)
    assert ri.role == "coauthor" and ri.is_collab and ri.owner_pk == str(mk.OTHER["pk"]) and ri.is_pinned is False


def test_gql_views_prefer_play_count_and_hidden_counts_are_not_partial():
    g = gql_item(TIMELINE[2])
    ri = norm(dict(g, video_play_count=900, video_view_count=500))
    assert (ri.views, ri.views_field, ri.partial) == (900, "play_count", False)
    ri = norm(dict(g, video_play_count=None, video_view_count=None))
    assert ri.views is None and ri.partial and ri.missing == ["views"]
    ri = norm(dict(g, like_and_view_counts_disabled=True))
    assert ri.views is None and ri.likes is None and ri.counts_hidden and not ri.partial and ri.missing == []
    ri = norm(dict(g, pinned=True))
    assert ri.is_pinned is True
    ri = norm(dict(g, video_view_count=-1, like_count=None, comment_count=None))
    assert ri.views is None and ri.likes is None and ri.comments is None


def test_pk_time_matches_the_fixture_clock_and_ignores_small_pks():
    for m in TIMELINE[:60]:
        assert igparse.pk_time(str(m["pk"])) == datetime.fromtimestamp(m["taken_at"], tz=timezone.utc)
    assert igparse.pk_time("123") is None and igparse.pk_time(None) is None and igparse.pk_time("abc") is None
    sparse = v1_reel(TIMELINE[3])
    assert sparse["taken_at"] is None and norm(sparse).taken_at_derived is True


def test_logged_in_normalize_is_unchanged():
    ri = igparse.normalize(igparse.trim_media(igparse.parse_ig_json(mk.dumps(TIMELINE[0]))), PK)
    assert (ri.layer, ri.strategy, ri.partial, ri.missing, ri.taken_at_derived) == (None, None, False, [], False)
    assert json.dumps(ri.to_dict(), default=str)
