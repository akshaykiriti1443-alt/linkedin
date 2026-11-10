"""WP4: tiers and the stratified, weighted baseline sample (DESIGN-SAAS.md 6.0 stage 2). Pure functions + one DB run."""
from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from reelvault import outliers as ol
from reelvault.outliers import ReelMetrics

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def account(n=100, *, seed=3, start_age=5.0, step=1.5, outliers=(10, 20, 30, 40), pinned=(), collab=(),
            prefix="r"):
    rng = random.Random(seed)
    rs = []
    for i in range(n):
        v = int(100_000 * (2.718 ** rng.gauss(0, 0.35)))
        if i in outliers:
            v *= 6
        rs.append(ReelMetrics(id=f"{prefix}{i:04d}", taken_at=NOW - timedelta(days=start_age + i * step), views=v,
                              likes=v // 40, comments=v // 1500, is_pinned=i in pinned, is_collab=i in collab,
                              owned=i not in collab))
    return rs


def tiers_of(scores, **kw):
    kw.setdefault("seed", "acc_1:pul_1")
    return {t.reel_id: t for t in ol.pick_sample(scores, **kw)}


def test_tiers_priority_sample_rest():
    sc = ol.score_account(account(100, pinned=(50,), collab=(60,)), NOW)
    t = tiers_of(sc)
    s = {x.reel_id: x for x in sc}
    pri = [k for k, v in t.items() if v.tier == "priority"]
    # every outlier and every top-10% reel is priority (owned, not pinned, not collab)
    for k, x in s.items():
        if x.owned and not x.is_pinned and not x.is_collab and x.score is not None and (
                ol.band_rank(x.band) >= ol.band_rank("outlier") or x.is_top10):
            assert t[k].tier == "priority", k
    assert {"r0010", "r0020", "r0030", "r0040"} <= set(pri)
    assert t["r0050"].tier in ("rest", "none") and t["r0060"].tier in ("rest", "none")
    assert all(t[k].weight == 1.0 for k in pri)
    samp = [v for v in t.values() if v.tier == "sample"]
    assert len(samp) == 25
    assert all(v.weight > 1 for v in samp)
    assert all(v.weight == 0 for v in t.values() if v.tier in ("rest", "none"))


def test_five_strata_of_five_and_weights_sum_to_eligible():
    sc = ol.score_account(account(100), NOW)
    t = tiers_of(sc)
    samp = [v for v in t.values() if v.tier == "sample"]
    per = Counter(v.stratum for v in samp)
    assert sorted(per) == [0, 1, 2, 3, 4] and set(per.values()) == {5}
    eligible = [v for v in t.values() if v.tier in ("sample", "rest") and v.stratum is not None]
    assert sum(v.weight for v in samp) == pytest.approx(len(eligible))
    # strata are equal time slices: stratum 0 is the oldest
    s = {x.reel_id: x for x in sc}
    oldest = max(s[v.reel_id].taken_at for v in eligible if v.stratum == 0)
    newest_next = min(s[v.reel_id].taken_at for v in eligible if v.stratum == 1)
    assert oldest <= newest_next


def test_small_stratum_takes_everyone():
    sc = ol.score_account(account(30, outliers=()), NOW)
    t = tiers_of(sc)
    elig = [v for v in t.values() if v.tier in ("sample", "rest") and v.stratum is not None]
    # 30 reels, ~3 priority (top 10%) -> ~27 eligible over 5 strata of ~5: every stratum <= 5 is taken whole or 5
    samp = [v for v in t.values() if v.tier == "sample"]
    assert len(samp) >= 25 or len(samp) == len(elig)
    assert sum(v.weight for v in samp) == pytest.approx(len(elig))


def test_fixed_seed_rerun_is_stable_and_seed_matters():
    sc = ol.score_account(account(100), NOW)
    a = tiers_of(sc, seed="acc_1:pul_1")
    b = tiers_of(sc, seed="acc_1:pul_1")
    assert {k: (v.tier, v.weight) for k, v in a.items()} == {k: (v.tier, v.weight) for k, v in b.items()}
    c = tiers_of(sc, seed="acc_1:pul_2")
    assert {k for k, v in a.items() if v.tier == "sample"} != {k for k, v in c.items() if v.tier == "sample"}
    # a re-run with the earlier picks as `previous` keeps exactly the same picks, whatever the seed
    prev = {k: v.tier for k, v in a.items()}
    d = tiers_of(sc, seed="something-else", previous=prev)
    assert {k for k, v in a.items() if v.tier == "sample"} == {k for k, v in d.items() if v.tier == "sample"}


def test_next_pull_adds_new_strata_and_keeps_earlier_picks():
    first = account(100)
    sc1 = ol.score_account(first, NOW)
    t1 = tiers_of(sc1, seed="acc:p1")
    picks1 = {k for k, v in t1.items() if v.tier == "sample"}
    both = account(200)  # same first 100 reels (same seed), plus 100 older ones
    sc2 = ol.score_account(both, NOW)
    t2 = tiers_of(sc2, seed="acc:p2", previous={k: v.tier for k, v in t1.items()})
    picks2 = {k for k, v in t2.items() if v.tier == "sample"}
    still_eligible = {k for k in picks1 if t2[k].tier in ("sample", "rest")}
    assert still_eligible <= picks2                      # earlier picks are kept
    assert len({v.stratum for v in t2.values() if v.tier == "sample"}) == 10   # 5 strata per 100 reels
    assert len(picks2) >= 48
    elig = [v for v in t2.values() if v.tier in ("sample", "rest") and v.stratum is not None]
    assert sum(t2[k].weight for k in picks2) == pytest.approx(len(elig))


def test_a_small_refresh_adds_no_strata():
    t1 = tiers_of(ol.score_account(account(100), NOW))
    more = account(104, start_age=5.0 - 4 * 1.5)  # 4 newer reels on top (ages shift), same seed for the first ones
    t2 = tiers_of(ol.score_account(more, NOW), previous={k: v.tier for k, v in t1.items()})
    assert len({v.stratum for v in t2.values() if v.tier == "sample"}) == 5


def test_growing_and_unscored_reels_get_tier_none():
    rs = account(40) + [ReelMetrics(id="young", taken_at=NOW - timedelta(days=1), views=5_000_000),
                        ReelMetrics(id="hidden", taken_at=NOW - timedelta(days=40), views=None, likes=None)]
    t = tiers_of(ol.score_account(rs, NOW))
    assert t["young"].tier == "none" and t["hidden"].tier == "none"


def test_starred_collab_becomes_priority():
    rs = account(60, collab=(7,), outliers=())
    rs[7].starred = True
    t = tiers_of(ol.score_account(rs, NOW))
    assert t["r0007"].tier == "priority"
    rs[7].starred = False
    t = tiers_of(ol.score_account(rs, NOW))
    assert t["r0007"].tier == "rest"


def test_best_for_deep_fills_with_top10():
    sc = ol.score_account(account(100, outliers=(10, 20)), NOW)
    best = ol.best_for_deep(sc, 5)
    assert best[:2] == sorted(["r0010", "r0020"], key=lambda k: -next(x.score for x in sc if x.reel_id == k))
    assert len(best) == 5
    top10 = {x.reel_id for x in sc if x.is_top10}
    assert set(best[2:]) <= top10


def test_process_all_samples_every_eligible_reel():
    sc = ol.score_account(account(100), NOW)
    t = tiers_of(sc, process_all=True)
    assert all(v.tier in ("priority", "sample", "none", "rest") for v in t.values())
    assert sum(1 for v in t.values() if v.tier == "rest") == 0
    assert all(v.weight == 1.0 for v in t.values() if v.tier == "sample")


def test_pick_sample_job_writes_tiers(member, run_jobs, monkeypatch):
    """score_account -> pick_sample through the job queue; stored weights sum to the eligible count."""
    from reelvault.server import db, jobs
    from reelvault.server import jobs_score as js

    monkeypatch.setattr(js, "INSIGHTS_DEBOUNCE_S", 3600)
    aid = db.new_id("acc")
    rs = account(100)
    with db.tx() as conn:
        conn.execute(db.accounts.insert().values(id=aid, user_id=member.id, handle="demo.acc"))
        for i, r in enumerate(rs):
            rid = db.new_id("reel")
            conn.execute(db.reels.insert().values(id=rid, user_id=member.id, account_id=aid, ig_pk=str(1000 + i),
                                                  code=f"C{i}", url=f"https://www.instagram.com/reel/C{i}/",
                                                  taken_at=r.taken_at, views=r.views, likes=r.likes,
                                                  comments=r.comments))
            conn.execute(db.reel_accounts.insert().values(reel_id=rid, account_id=aid, user_id=member.id,
                                                          role="owner"))
    jobs.enqueue(member.id, "score_account", {"account_id": aid}, dedupe_key=f"score_account:{aid}")
    done = run_jobs()
    kinds = [j.kind for j in done]
    assert kinds[:2] == ["score_account", "pick_sample"]
    assert done[0].result["scored"] == 100 - 0 and done[1].result["sample"] == 25
    with db.read() as conn:
        rows = conn.execute(select(db.reels).where(db.reels.c.user_id == member.id)).all()
        acc = conn.execute(select(db.accounts).where(db.accounts.c.id == aid)).first()
        q = conn.execute(select(db.jobs).where(db.jobs.c.kind == "refresh_insights")).all()
    tiers = Counter(r.tier for r in rows)
    assert tiers["sample"] == 25 and tiers["priority"] >= 10
    elig = sum(1 for r in rows if r.tier in ("sample", "rest"))
    assert sum(r.sample_weight for r in rows if r.tier == "sample") == pytest.approx(elig)
    assert acc.baseline_views and 80_000 < acc.baseline_views < 125_000
    assert len(q) == 1 and q[0].state == "queued" and q[0].run_after is not None  # debounced insights
    # running it again changes nothing (fixed seed + earlier picks kept)
    before = {r.id: (r.tier, r.sample_weight) for r in rows}
    js.pick_sample_now(member.id, aid)
    with db.read() as conn:
        after = {r.id: (r.tier, r.sample_weight) for r in conn.execute(
            select(db.reels).where(db.reels.c.user_id == member.id)).all()}
    assert before == after
