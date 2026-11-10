"""WP4: outlier scoring within one account (DESIGN-SAAS.md 6.6) and discovery ranking (6.6.1). Pure functions."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from reelvault import outliers as ol
from reelvault.outliers import ReelMetrics

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def reel(i, age_days, views=None, likes=None, comments=None, **kw) -> ReelMetrics:
    return ReelMetrics(id=f"r{i:03d}", taken_at=NOW - timedelta(days=age_days), views=views, likes=likes,
                       comments=comments, **kw)


def flat(n=40, views=100_000, start_age=40, step=2.0, **kw):
    """n mature reels with identical views (so every baseline is exactly `views`)."""
    return [reel(i, start_age + i * step, views=views, likes=views // 50, comments=views // 2000, **kw)
            for i in range(n)]


def by_id(scores):
    return {s.reel_id: s for s in scores}


# ---------------------------------------------------------------------------------------------- maturity

@pytest.mark.parametrize("age,m", [(0, 0.30), (0.5, 0.30), (1, 0.30), (2, 0.475), (3, 0.65), (5, 0.75), (7, 0.85),
                                   (14, 0.95), (22, 0.975), (30, 1.0), (400, 1.0)])
def test_maturity_curve_points(age, m):
    assert ol.maturity(age) == pytest.approx(m)


def test_maturity_is_monotonic():
    xs = [ol.maturity(d / 4) for d in range(0, 200)]
    assert all(b >= a for a, b in zip(xs, xs[1:]))


# ---------------------------------------------------------------------------------------------- baseline window

def test_baseline_uses_30_nearest_and_excludes_self_pinned_young_and_collabs():
    rs = flat(60)
    # a far-away block of huge reels must not leak into the baseline of r000 (they are not among its 30 nearest)
    for r in rs[40:]:
        r.views = 10_000_000
    target = rs[0]
    target.views = 400_000                                  # 4x
    rs.append(reel(900, 41, views=50_000_000, is_pinned=True))       # pinned: never a peer
    rs.append(reel(901, 41, views=50_000_000, owned=False))         # coauthor: never a peer
    rs.append(reel(902, 41, views=50_000_000, is_collab=True))      # collab: never a peer
    rs.append(reel(903, 1, views=50_000_000))                        # too young: never a peer
    s = by_id(ol.score_account(rs, NOW))
    t = s["r000"]
    assert t.baseline_n == 30
    assert t.baseline == pytest.approx(100_000)
    assert t.score == pytest.approx(4.0)
    assert t.band == "outlier" and t.confidence == "high"
    assert t.log2_score == pytest.approx(2.0)
    # the pinned / collab reels are still scored for display against the account's baseline
    assert s["r900"].score is not None and s["r902"].score is not None
    assert s["r903"].band == "growing" and s["r903"].score is None


def test_ties_take_the_older_reel_first():
    # target in the middle; one peer 2 days newer (views 1) and one 2 days older (views 1000) compete for slot 5
    rs = [reel(0, 50, views=500)]
    rs += [reel(i, 50 + i * 0.1, views=500) for i in range(1, 5)]       # 4 close older peers
    rs.append(reel(10, 48, views=1))          # 2 d newer
    rs.append(reel(11, 52, views=1000))       # 2 d older -> wins the tie
    s = by_id(ol.score_account(rs, NOW))["r000"]
    assert s.baseline_n == 6  # everyone else (< 30)
    # with only 6 peers both are in; check the tie order with a 5-wide window instead
    old = ol.WINDOW
    try:
        ol.WINDOW = 5
        s = by_id(ol.score_account(rs, NOW))["r000"]
        assert s.baseline_n == 5
        assert s.baseline == pytest.approx(500)   # median of 500,500,500,500,1000
    finally:
        ol.WINDOW = old


@pytest.mark.parametrize("n_peers,conf,band", [(4, None, "unscored"), (5, "low", "normal"), (7, "low", "normal"),
                                               (8, "medium", "normal"), (14, "medium", "normal"),
                                               (15, "high", "normal")])
def test_min_5_peers_and_confidence(n_peers, conf, band):
    rs = flat(n_peers + 1)
    s = by_id(ol.score_account(rs, NOW))["r000"]
    assert s.band == band and s.confidence == conf
    assert s.baseline_n == n_peers


def test_zero_baseline_is_unscored():
    rs = [reel(i, 40 + i, views=0) for i in range(10)]
    s = ol.score_account(rs, NOW)
    assert all(x.band == "unscored" and x.score is None for x in s)


@pytest.mark.parametrize("mult,band", [(0.49, "below"), (0.5, "normal"), (1.99, "normal"), (2.0, "above"),
                                       (2.99, "above"), (3.0, "outlier"), (4.99, "outlier"), (5.0, "big"),
                                       (9.99, "big"), (10.0, "breakout"), (40, "breakout")])
def test_band_edges(mult, band):
    rs = flat(20)
    rs[0].views = int(round(100_000 * mult))
    s = by_id(ol.score_account(rs, NOW))["r000"]
    assert s.band == band
    assert ol.band_label(band) in ("Below normal", "Normal", "Above normal", "Outlier", "Big outlier", "Breakout")


def test_growing_reels_get_no_score_and_a_check_back_line():
    rs = flat(20)
    rs.append(reel(500, 1.2, views=5_000_000))
    s = by_id(ol.score_account(rs, NOW))["r500"]
    assert s.band == "growing" and s.score is None
    assert ol.why(s) == "Still growing. Check back in 2 days."


def test_maturity_adjusts_young_reels():
    rs = flat(20)
    young = reel(600, 7, views=85_000)      # m(7) = 0.85 -> adj 100,000 = normal
    rs.append(young)
    s = by_id(ol.score_account(rs, NOW))["r600"]
    assert s.adj == pytest.approx(100_000)
    assert s.score == pytest.approx(1.0)


def test_likes_basis_when_views_hidden():
    rs = [reel(i, 40 + i * 2, views=None, likes=2000) for i in range(20)]
    rs[0].likes = 7000
    rs.append(reel(99, 40, views=None, likes=None, comments=None))
    s = by_id(ol.score_account(rs, NOW))
    assert s["r000"].basis == "likes" and s["r000"].score == pytest.approx(3.5) and s["r000"].band == "outlier"
    assert "(scored on likes)" in ol.why(s["r000"])
    assert s["r099"].basis is None and s["r099"].band == "unscored"
    assert ol.why(s["r099"]) == "Instagram hides the numbers on this reel."


def test_views_reel_peers_use_views_only():
    rs = flat(20)
    # reels with hidden views but likes are not views peers
    rs += [reel(200 + i, 41 + i, views=None, likes=10) for i in range(10)]
    s = by_id(ol.score_account(rs, NOW))["r000"]
    assert s.basis == "views" and s.baseline_n == 19


def test_percentile_and_top10():
    rs = [reel(i, 40 + i, views=100_000 + i * 1000) for i in range(100)]
    s = ol.score_account(rs, NOW)
    top = [x for x in s if x.is_top10]
    assert len(top) == 10
    assert max(x.percentile for x in s) == 100.0 and min(x.percentile for x in s) == 0.0


def test_percentile_ties_are_averaged():
    rs = flat(11)
    s = ol.score_account(rs, NOW)
    assert {x.percentile for x in s} == {50.0}
    assert not any(x.is_top10 for x in s)


# ---------------------------------------------------------------------------------------------- badges

def test_loved_and_hidden_gem_badges():
    rs = flat(20)
    r = rs[0]
    r.views, r.likes, r.comments = 120_000, 120_000 // 50 * 3, 120_000 // 2000 * 3  # 3x engagement, 1.2x views
    s = by_id(ol.score_account(rs, NOW))["r000"]
    assert s.rel_er == pytest.approx(3.0, rel=0.01)
    assert "loved" in s.badges and "hidden_gem" in s.badges


def test_conversation_badge_skips_comment_keyword_ctas():
    def make(caption=None, cta=None):
        rs = flat(20)
        rs[0].comments = rs[0].likes  # comment ratio 25x the usual
        rs[0].caption, rs[0].cta_type = caption, cta
        return by_id(ol.score_account(rs, NOW))["r000"]

    assert "conversation" in make().badges
    assert "conversation" not in make(caption='Comment "GUIDE" and I will send it').badges
    assert "conversation" not in make(caption="comment AUDIT below").badges
    assert "conversation" not in make(cta="comment_keyword").badges
    assert "conversation" in make(caption="comment below what you think").badges


def test_reach_badge_and_engagement_thresholds():
    rs = flat(20, followers_at_pull=90_000)
    s = by_id(ol.score_account(rs, NOW))
    assert "reach" in s["r000"].badges
    small = flat(20, views=900)          # < 1,000 views: no ER at all
    assert all(x.rel_er is None for x in ol.score_account(small, NOW))


def test_badges_never_change_the_score():
    rs = flat(20)
    a = by_id(ol.score_account(rs, NOW))["r000"].score
    rs[0].likes = rs[0].likes * 10
    b = by_id(ol.score_account(rs, NOW))["r000"].score
    assert a == b


# ---------------------------------------------------------------------------------------------- why line

def test_why_line():
    rs = flat(20)
    rs[0].views = 440_000
    rs[0].likes = int(440_000 / 50 * 1.6)
    rs[0].comments = int(440_000 / 2000 * 1.6)
    s = by_id(ol.score_account(rs, NOW))["r000"]
    assert ol.why(s) == "4.4x this creator's normal views · 1.6x their usual likes per view · 40 days old (fully grown)"
    rs2 = flat(20, start_age=12)
    s2 = by_id(ol.score_account(rs2, NOW))["r000"]
    assert ol.why(s2).endswith("12 days old (still growing a little)")


def test_baseline_views_and_normal_line():
    rs = flat(20)
    rs.append(reel(77, 1, views=10))
    assert ol.baseline_views(rs, NOW) == 100_000
    assert ol.normal_line(412000, 30) == "A normal reel here gets about 412,000 views (based on 30 nearby reels)."
    assert "at least 5 reels" in ol.normal_line(None, None)


def test_fixture_timeline_finds_the_planted_outliers():
    """WP2's deterministic timeline has planted outliers at known indices (make_fixtures.account_timeline)."""
    import importlib.util
    from pathlib import Path

    from reelvault import igparse

    fx = Path(__file__).resolve().parents[1] / "fixtures" / "ig" / "make_fixtures.py"
    spec = importlib.util.spec_from_file_location("rv_mkfx_wp4", fx)
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    now = datetime.fromtimestamp(mk.FIXTURE_NOW, tz=timezone.utc)
    tl = mk.account_timeline(250)
    rs = []
    for i, m in enumerate(tl):
        ri = igparse.normalize(igparse.trim_media(igparse.parse_ig_json(mk.dumps(m))), str(mk.TARGET["pk"]))
        rs.append(ReelMetrics(id=f"i{i:03d}", taken_at=ri.taken_at, views=ri.views, likes=ri.likes,
                              comments=ri.comments, is_pinned=ri.is_pinned))
    s = by_id(ol.score_account(rs, now))
    planted = (5, 17, 31, 44, 66, 90, 121, 150, 188, 230)
    found = [i for i in planted if s[f"i{i:03d}"].band in ("outlier", "big", "breakout")]
    assert len(found) >= 8, {i: s[f"i{i:03d}"].score for i in planted}
    assert s["i000"].band == "growing" and s["i001"].band == "growing"
    normal = [x for k, x in s.items() if int(k[1:]) not in planted and x.score is not None]
    assert sum(1 for x in normal if x.band in ("outlier", "big", "breakout")) <= 3


# ---------------------------------------------------------------------------------------------- discovery (6.6.1)

def items(n, views, every_days=2.0, start_age=31.0):
    return [{"taken_at": NOW - timedelta(days=start_age + k * every_days), "views": views} for k in range(n)]


def test_candidate_rank_formula():
    r = ol.candidate_rank(120_000, items(12, 84_000, every_days=7 / 4), NOW)
    assert r["median_views_12"] == 84_000
    assert r["posts_per_week"] == pytest.approx(12 / (11 * 7 / 4 / 7), rel=0.01)
    expect = math.log10(84_001) + 0.5 * math.log10(84_000 / 120_000 + 0.01) + 0.2 * min(r["posts_per_week"], 7) / 7
    assert r["rank_score"] == pytest.approx(expect, abs=1e-3)
    assert not r["not_posting"]
    assert ol.candidate_why({**r, "followers": 120_000, "checked_at": NOW}).startswith(
        "Median 84,000 views on the last 12 reels · posts 4x a week · 120K followers")


def test_candidate_rank_pushes_stale_or_thin_accounts_down():
    good = ol.candidate_rank(50_000, items(12, 20_000), NOW)
    stale = ol.candidate_rank(50_000, items(12, 2_000_000, start_age=70), NOW)
    fresh = ol.candidate_rank(50_000, items(12, 20_000, start_age=4), NOW)
    assert not fresh["not_posting"] and fresh["median_views_12"] > 20_000  # young reels are scaled up
    thin = ol.candidate_rank(50_000, items(3, 2_000_000), NOW)
    assert stale["not_posting"] and thin["not_posting"]
    assert good["rank_score"] > stale["rank_score"] and good["rank_score"] > thin["rank_score"]
    assert ol.candidate_why({**stale, "followers": 50_000, "checked_at": NOW}).startswith("Not posting lately")


def test_candidate_rank_uses_maturity_and_skips_young_reels():
    its = [{"taken_at": NOW - timedelta(days=1), "views": 10_000_000}] + items(11, 50_000)
    r = ol.candidate_rank(10_000, its, NOW)
    assert r["median_views_12"] == 50_000
    # min(followers, 1000) floor: a tiny account is compared with 1,000 followers
    small = ol.candidate_rank(10, items(12, 5_000), NOW)
    assert small["rank_score"] == pytest.approx(math.log10(5001) + 0.5 * math.log10(5 + 0.01)
                                                + 0.2 * min(small["posts_per_week"], 7) / 7, abs=1e-3)
