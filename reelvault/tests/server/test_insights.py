"""WP4: weighted lift, within-creator lift, evidence, grid, combos, recency, sub-formats (DESIGN-SAAS.md 6.7, 6.9)."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from reelvault import insights as ins
from reelvault import outliers as ol
from reelvault.insights import Unit

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
_n = [0]


def U(acc, score, *, w=1.0, age=40, tier=None, visual_ok=True, **tags) -> Unit:
    _n[0] += 1
    return Unit(reel_id=f"u{_n[0]:05d}", account_id=acc, handle=acc, score=score, weight=w,
                tier=tier or ("priority" if w == 1.0 else "sample"), taken_at=NOW - timedelta(days=age),
                visual_ok=visual_ok, tags=tags, url=f"https://www.instagram.com/reel/u{_n[0]}/")


def row(data, dim, value, window=None):
    rows = data["rows"][dim] if window is None else data["windows"][window]["rows"][dim]
    return next(r for r in rows if r["value"] == value)


# ---------------------------------------------------------------------------------------------- lift core

def handmade():
    return ([U("A", 4.0, visual_format="VF04", hook_type="question") for _ in range(4)]
            + [U("A", 1.0, w=10.0, visual_format="VF01", hook_type="question") for _ in range(2)]
            + [U("B", 5.0, visual_format="VF01", hook_type="question") for _ in range(2)]
            + [U("B", 1.0, w=5.0, visual_format="VF04", hook_type="question") for _ in range(2)])


def test_weighted_shrunk_lift_alpha_10():
    d = ins.compute(handmade(), now=NOW)
    assert d["N"] == 10 and d["W"] == pytest.approx(36)
    assert d["H0"] == pytest.approx(6 / 36, abs=1e-4)
    h0 = 6 / 36
    x = row(d, "visual_format", "VF04")
    assert x["lift"] == pytest.approx(round(((4 + 10 * h0) / (14 + 10)) / h0, 2))
    assert x["n"] == 6 and x["k"] == 2 and x["hits"] == 4
    assert x["hit_rate"] == pytest.approx(round(4 / 14, 3))
    assert x["usage"] == pytest.approx(round(14 / 36, 3))
    y = row(d, "visual_format", "VF01")
    assert y["lift"] == pytest.approx(round(((2 + 10 * h0) / (22 + 10)) / h0, 2))


def test_within_creator_lift_weights_each_creator_equally():
    d = ins.compute(handmade(), now=NOW)
    x = row(d, "visual_format", "VF04")
    d_a = 2.0 - (4 * 2.0 * 1 + 0) / (4 + 20)
    d_b = 0.0 - (2 * math.log2(5) * 1) / (2 + 10)
    assert x["typical"] == pytest.approx(round(2 ** ((d_a + d_b) / 2), 2))
    # a value every reel of a creator has (no "not tagged" reels) has no creator to compare -> typical None
    assert row(d, "hook_type", "question")["typical"] is None


def test_evidence_uses_unweighted_counts():
    us = [U(c, 4.0, visual_format="VF05") for c in ("A", "B", "C")]
    us += [U("A", 1.0, w=50.0, visual_format="VF05") for _ in range(2)]
    us += [U(c, 1.0, visual_format="VF01") for c in "ABCD" for _ in range(3)]
    d = ins.compute(us, now=NOW)
    assert row(d, "visual_format", "VF05")["evidence"] == "promising"   # n=5, k=3 even though W is huge
    us += [U(c, 1.0, visual_format="VF05") for c in "BCD" for _ in range(2)]
    d = ins.compute(us, now=NOW)
    assert row(d, "visual_format", "VF05")["evidence"] == "strong"      # n=11, k=4
    assert row(ins.compute([U("A", 4.0, visual_format="VF09")] * 1 + [U("A", 1.0, visual_format="VF01")] * 3,
                           now=NOW), "visual_format", "VF09")["evidence"] == "thin"


def test_grid_rules():
    rows = [
        {"value": "a", "lift": 1.3, "usage": 0.2, "evidence": "promising", "n": 9, "k": 3},   # working_common
        {"value": "b", "lift": 0.8, "usage": 0.3, "evidence": "strong", "n": 30, "k": 5},     # overused
        {"value": "c", "lift": 2.0, "usage": 0.05, "evidence": "thin", "n": 3, "k": 1},        # opportunity
        {"value": "d", "lift": 1.6, "usage": 0.2, "evidence": "strong", "n": 20, "k": 2},      # both wc + opp
        {"value": "e", "lift": 1.3, "usage": 0.2, "evidence": "thin", "n": 4, "k": 1},         # nothing
        {"value": "f", "lift": 2.0, "usage": 0.05, "evidence": "thin", "n": 2, "k": 1},        # n < 3
    ]
    g = ins._grid(rows)  # noqa: SLF001
    assert g["working_common"] == ["a", "d"]
    assert g["overused"] == ["b"]
    assert g["opportunity"] == ["c", "d"]


def test_combos_need_5_reels():
    us = [U("A", 4.0, visual_format="VF04", hook_type="contrarian", sound="SD06") for _ in range(3)]
    us += [U(c, 1.0, visual_format="VF04", hook_type="contrarian", sound="SD01") for c in "BC"]
    us += [U("B", 4.0, visual_format="VF01", hook_type="question", sound="SD06") for _ in range(2)]
    us += [U("C", 1.0, visual_format="VF01", hook_type="question", sound="SD01") for _ in range(2)]
    d = ins.compute(us, now=NOW)
    pairs = {(c["kind"], c["a"], c["b"]): c for c in d["combos"]}
    assert ("format×hook", "VF04", "contrarian") in pairs and pairs[("format×hook", "VF04", "contrarian")]["n"] == 5
    assert ("format×hook", "VF01", "question") not in pairs          # only 4 reels
    assert ("format×sound", "VF04", "SD06") not in pairs              # 3 reels


def test_h0_zero_falls_back_to_top10():
    us = [U("A", 2.5, visual_format="VF04") for _ in range(3)] + [U("A", 1.0, visual_format="VF01") for _ in range(6)]
    for u in us[:3]:
        u.is_top10 = True
    d = ins.compute(us, now=NOW)
    assert d["hit_rule"] == "top 10%" and d["H0"] > 0
    assert row(d, "visual_format", "VF04")["lift"] > 1
    assert "top 10%" in d["data_note"]["hit_rule_line"]


def test_recency_rising_and_fading():
    assert ins._trend(1.2, 2.0, 5) == "rising"          # noqa: SLF001
    assert ins._trend(1.2, 2.0, 4) is None              # noqa: SLF001
    assert ins._trend(1.2, 1.5, 6) is None              # noqa: SLF001  (1.5 < 1.3 x 1.2 = 1.56)
    assert ins._trend(2.0, 1.4, 5) == "fading"          # noqa: SLF001
    assert ins._trend(1.4, 0.5, 5) is None              # noqa: SLF001  (lift < 1.5)
    us = [U("A", 1.0, age=200, visual_format="VF07") for _ in range(30)]
    us += [U(c, 4.0, age=20, visual_format="VF07") for c in "ABC" for _ in range(2)]
    us += [U(c, 1.0, age=a, visual_format="VF01") for c in "ABC" for a in (20, 30, 40, 50, 60, 150, 220, 300)]
    us += [U("B", 4.0, age=200, visual_format="VF01")]
    d = ins.compute(us, now=NOW)
    r = row(d, "visual_format", "VF07")
    assert r["n_recent"] == 6 and r["lift_recent"] >= 1.5 and r["lift_recent"] >= 1.3 * r["lift"]
    assert r["trend"] == "rising"
    assert d["windows"]["recent"]["window_days"] == 90 if "windows" in d else True
    # fading: worked a lot before, not in the last 90 days
    us = [U(c, 4.0, age=200, visual_format="VF08") for c in "ABC" for _ in range(4)]
    us += [U(c, 1.0, age=20, visual_format="VF08") for c in "ABC" for _ in range(2)]
    us += [U(c, 1.0, age=a, visual_format="VF01") for c in "ABC" for a in (20, 30, 40, 150, 220, 300, 310, 320)]
    us += [U("A", 4.0, age=30, visual_format="VF01")]
    r = row(ins.compute(us, now=NOW), "visual_format", "VF08")
    assert r["lift"] >= 1.5 and r["n_recent"] == 6 and r["trend"] == "fading", r


def test_window_drops_old_reels_and_notes_them():
    us = [U("A", 4.0, age=500, visual_format="VF04")] + [U("A", 1.0, age=40, visual_format="VF01") for _ in range(5)]
    d = ins.compute(us, now=NOW, window_days=365)
    assert d["N"] == 5 and d["data_note"]["excluded"]["older_than_window"] == 1
    assert ins.compute(us, now=NOW, window_days=None)["N"] == 6


def test_visual_dimensions_skip_unverified_tags():
    us = [U("A", 4.0, visual_ok=False, visual_format="VF05", hook_type="question", headline_box=True)
          for _ in range(3)]
    us += [U("A", 1.0, visual_format="VF01", hook_type="story_open", headline_box=False) for _ in range(5)]
    d = ins.compute(us, now=NOW)
    assert not any(r["value"] == "VF05" for r in d["rows"]["visual_format"])
    assert not any(r["value"] == "yes" for r in d["rows"]["headline_box"])
    assert row(d, "hook_type", "question")["n"] == 3          # non-visual dimensions still use them
    assert d["data_note"]["excluded"]["unverified_visual"] == 3


def test_collabs_and_self_are_left_out_of_competitor_lift():
    us = [U("A", 4.0, visual_format="VF04") for _ in range(3)] + [U("A", 1.0, visual_format="VF01") for _ in range(5)]
    collab = U("A", 9.0, visual_format="VF10")
    collab.is_collab = True
    me = [U("ME", 9.0, visual_format="VF11") for _ in range(3)]
    for u in me:
        u.is_self = True
    d = ins.compute(us + [collab] + me, now=NOW)
    assert d["N"] == 8 and d["accounts"] == 1
    vals = {r["value"] for r in d["rows"]["visual_format"]}
    assert "VF10" not in vals and "VF11" not in vals


def test_you_vs_them_needs_10_scored_reels_of_your_own():
    us = [U("A", 4.0, visual_format="VF05") for _ in range(4)] + [U(c, 1.0, visual_format="VF01") for c in "AB"
                                                                  for _ in range(4)]
    us += [U("B", 4.0, visual_format="VF05") for _ in range(4)]
    us += [U("C", 1.0, visual_format="VF01") for _ in range(8)]
    me = [U("ME", 1.0, visual_format="VF01", topics=["ad-creative"]) for _ in range(7)]
    me += [U("ME", 2.0, visual_format="VF02") for _ in range(3)]
    for u in me:
        u.is_self = True
    assert ins.compute(us + me, now=NOW, self_scored=9)["you_vs_them"] is None
    y = ins.compute(us + me, now=NOW, self_scored=10)["you_vs_them"]
    f = {x["vf"]: x for x in y["formats"]}
    assert f["VF01"]["you"] == pytest.approx(0.7) and f["VF05"]["you"] == 0
    assert y["line"].startswith("You post Talking head 70% of the time; your competitors")
    assert "Their best format, Screen recording, you've never tried." in y["line"]
    assert y["topics_covered"] == ["ad-creative"]


# ---------------------------------------------------------------------------------------------- can make

def test_can_make():
    caps = {"on_camera": False, "voice": True, "screen": True, "broll": False, "hands": False, "second_person": False,
            "location": False, "editor": False}
    assert ins.can_make("VF01", caps) == {"ok": False, "reason": "Needs you on camera. You said voice only."}
    assert ins.can_make("VF05", caps)["ok"] is True
    assert ins.can_make("VF12", caps)["reason"].startswith("Needs you on camera, filming on location and a second")
    assert ins.can_make("VF01", None) == {"ok": True, "reason": None}
    assert ins.can_make("VF07", caps)["ok"] is True                   # needs nothing
    assert ins.can_make("VF05", {"screen_recording": True})["ok"] is True   # q12 ids are understood too


def test_cards_put_can_make_first_and_need_evidence():
    us = [U(c, 4.0, visual_format="VF01") for c in "ABC" for _ in range(3)]
    us += [U(c, 4.0, visual_format="VF05") for c in "ABC" for _ in range(2)]
    us += [U(c, 1.0, visual_format="VF09") for c in "ABC" for _ in range(8)]
    d = ins.compute(us, now=NOW, capabilities={"on_camera": False, "voice": True, "screen": True})
    vf_cards = [c for c in d["cards"] if c["dimension"] == "visual_format"]
    assert [c["value"] for c in vf_cards] == ["VF05", "VF01"]
    assert vf_cards[1]["can_make"]["ok"] is False
    assert vf_cards[0]["line"].startswith("2.") or "x more likely to be an outlier" in vf_cards[0]["line"]
    assert "reels from 3 of your 3 competitors" in vf_cards[0]["line"]


# ---------------------------------------------------------------------------------------------- sub-formats

def recipe(**kw):
    base = dict(visual_format="VF01", headline_box=True, caption_style="word_by_word_highlight", face_bubble=False,
                broll_share="light", setting="desk", framing="close")
    base.update(kw)
    return base


def test_sub_format_grouping_and_merge_order():
    us = [U("A", 4.0, **recipe()) for _ in range(3)]                             # full key, 3 units
    us += [U("B", 4.0, **recipe(framing="medium")), U("B", 1.0, **recipe(framing="wide")),
           U("C", 1.0, **recipe(framing="medium"))]                              # merge after dropping framing
    us += [U("C", 1.0, **recipe(visual_format="VF05", setting="screen_only", framing="none")) for _ in range(3)]
    us += [U("D", 1.0, **recipe(visual_format="VF09")), U("D", 1.0, **recipe(visual_format="VF09"))]  # too few
    g = ins.sub_formats(us)
    keys = {x.recipe_key: x for x in g}
    assert "VF01|hb=1|cap=word|fb=0|br=light|set=desk|fr=close" in keys
    assert "VF01|hb=1|cap=word|fb=0|br=light|set=desk" in keys
    merged = keys["VF01|hb=1|cap=word|fb=0|br=light|set=desk"]
    assert merged.n == 3 and merged.dropped == ["framing"] and merged.k == 2 and merged.hits == 1
    assert not any(k.startswith("VF05") for k in keys)                  # 3 units but no outlier
    assert not any(k.startswith("VF09") for k in keys)                  # < 3 units even at VF only
    every = [rid for x in g for rid in x.reel_ids]
    assert len(every) == len(set(every))                                # a reel is in one group at most
    # deterministic: shuffled input -> same groups
    sh = list(us)
    random.Random(4).shuffle(sh)
    assert [x.to_dict() for x in ins.sub_formats(sh)] == [x.to_dict() for x in g]


def test_sub_formats_skip_unverified_self_and_collab():
    us = [U("A", 4.0, visual_ok=False, **recipe()) for _ in range(3)]
    me = [U("ME", 4.0, **recipe()) for _ in range(3)]
    for u in me:
        u.is_self = True
    assert ins.sub_formats(us + me) == []


def test_fallback_name():
    assert ins.fallback_name("VF01", {"setting": "desk", "headline_box": True,
                                      "caption_style": "word_by_word_highlight"}) == \
        "Desk talking head + top headline box + word-by-word captions"
    assert ins.fallback_name("VF05", {"face_bubble": True}) == "Screen recording + face bubble"


def test_sample_weights_hand_strata_without_tags_to_the_rest():
    w = ins.sample_weights({0: 20, 1: 20}, {0: ["a", "b", "c", "d"], 1: ["e", "f", "g", "h", "i"]})
    assert w["a"] == 5.0 and w["e"] == 4.0
    w = ins.sample_weights({0: 20, 1: 20, 2: 20}, {0: ["a", "b"], 1: ["c", "d"], 2: []})
    assert sum(w.values()) == pytest.approx(60)


def test_music_rows():
    reels = [{"id": "r1", "url": "u1", "score": 5, "is_hit": True,
              "audio": {"type": "licensed", "title": "Song A", "artist": "X"}},
             {"id": "r2", "url": "u2", "score": 1, "is_hit": False,
              "audio": {"type": "licensed", "title": "song a", "artist": "x"}},
             {"id": "r3", "url": "u3", "score": 4, "is_hit": True,
              "audio": {"type": "original", "title": "Hook sound", "is_reused": True}},
             {"id": "r4", "url": "u4", "score": 4, "is_hit": True,
              "audio": {"type": "original", "title": "Original audio"}},
             {"id": "r5", "url": "u5", "score": 4, "is_hit": True, "audio": {"type": "unknown"}}]
    m = ins.music_rows(reels)
    assert m[0] == {"title": "Song A", "artist": "X", "type": "licensed", "outliers": 1, "reels": 2,
                    "example_reel": {"id": "r1", "url": "u1"}}
    assert m[1]["type"] == "reused_original" and len(m) == 2


# ---------------------------------------------------------------------------------------------- simulation

def _simulate(seed=11, creators=6, n=100, planted="VF04", share=0.15, effect=2.7, sigma=0.6):
    """6 creators x 100 reels, a planted format worth `effect` x normal; only the priority set and a 25-reel
    stratified sample per creator are 'tagged' (as in production), with weights recomputed at insight time."""
    rng = random.Random(seed)
    units = []
    others = ["VF01", "VF02", "VF05", "VF09", "VF03"]
    truth = {}
    for c in range(creators):
        base = 50_000 * (c + 1)
        rows = []
        for i in range(n):
            vf = planted if rng.random() < share else rng.choice(others)
            age = 4 + i * 2.5
            v = base * math.exp(rng.gauss(0, sigma)) * (effect if vf == planted else 1.0) * ol.maturity(age)
            rid = f"c{c}r{i:03d}"
            truth[rid] = vf
            rows.append(ol.ReelMetrics(id=rid, taken_at=NOW - timedelta(days=age), views=int(v)))
        scores = ol.score_account(rows, NOW)
        tiers = {t.reel_id: t for t in ol.pick_sample(scores, per_account=25, strata=5, seed=f"c{c}")}
        elig = [s for s in scores if tiers[s.reel_id].tier in ("sample", "rest")]
        n_pri = sum(1 for t in tiers.values() if t.tier == "priority")
        strata = ol.strata_of(elig, 5, n_account=len(elig) + n_pri)
        sizes, tagged = {}, {}
        for s in elig:
            k = strata[s.reel_id]
            sizes[k] = sizes.get(k, 0) + 1
            if tiers[s.reel_id].tier == "sample":
                tagged.setdefault(k, []).append(s.reel_id)
        w = ins.sample_weights(sizes, tagged)
        for s in scores:
            t = tiers[s.reel_id]
            if t.tier not in ("priority", "sample") or s.score is None:
                continue
            units.append(Unit(reel_id=s.reel_id, account_id=f"c{c}", handle=f"c{c}", score=s.score,
                              weight=1.0 if t.tier == "priority" else w[s.reel_id], tier=t.tier,
                              taken_at=s.taken_at, is_top10=s.is_top10, tags={"visual_format": truth[s.reel_id]}))
    return units


def test_planted_format_is_found_from_priority_set_plus_weighted_sample():
    units = _simulate()
    assert len(units) < 6 * 100 * 0.6      # we really only "processed" a part of the reels
    d = ins.compute(units, now=NOW, window_days=None)
    r = row(d, "visual_format", "VF04")
    assert r["lift"] > 2, r
    assert r["typical"] > 2, r
    assert r["evidence"] == "strong"
    assert row(d, "visual_format", "VF01")["lift"] < 1.2
    assert d["cards"][0]["value"] == "VF04"


def test_lift_under_one_never_reads_more_likely():
    """Integration finding: a slideshow at 0.3x read '0.3x more likely to be an outlier' on Home."""
    from reelvault import insights

    assert insights.lift_phrase(2.62) == "2.6x more likely to be an outlier"
    assert insights.lift_phrase(0.28) == "Less likely to be an outlier (0.3x)"


# ---------------------------------------------------------------------------------------------- fix round 1

def test_deep_reads_on_hits_do_not_change_lift(member):
    """Lift must use ONE tagger for every unit (quick tags; member overrides win). Deep reads run almost only on
    outliers, so letting their (different) answers replace the quick tags turned tagger habits into "lift"."""
    from reelvault.server import db
    from reelvault.server import jobs_score as js

    uid = member.id
    now = db.utcnow()
    with db.tx() as conn:
        ids = []
        for a in range(3):
            aid = f"acc_fix1_{a}"
            conn.execute(db.accounts.insert().values(id=aid, user_id=uid, handle=f"comp{a}", role="competitor"))
            for k in range(20):
                rid = f"reel_fix1_{a}_{k:02d}"
                hit = k < 3
                conn.execute(db.reels.insert().values(
                    id=rid, user_id=uid, account_id=aid, ig_pk=str(1000 + a * 100 + k), code=f"C{a}x{k:02d}abc",
                    url=f"https://www.instagram.com/reel/C{a}x{k:02d}abc/", taken_at=now - timedelta(days=30 + k),
                    score=5.0 if hit else 1.0, tier="priority", band="outlier" if hit else "normal"))
                vf = "VF04" if hit or k % 4 == 0 else "VF01"
                conn.execute(db.analyses.insert().values(
                    id=db.new_id("ana"), reel_id=rid, user_id=uid, level="quick", visual_format=vf,
                    sound_primary="SD05", hook_type="question", tag_quality="vision"))
                ids.append((rid, hit))

    def lifts():
        with db.tx(immediate=False) as conn:
            units, _ = js.build_units(conn, uid)
        data = ins.compute(units, now=now) if "now" in ins.compute.__code__.co_varnames else ins.compute(units)
        return {r["value"]: r["lift"] for r in data["rows"]["visual_format"]}, \
            {r["value"]: r["lift"] for r in data["rows"]["sound"]}

    before_vf, before_sd = lifts()
    with db.tx() as conn:  # deep reads on the hits only, with answers the quick tagger never gives
        for rid, hit in ids:
            if hit:
                conn.execute(db.analyses.insert().values(
                    id=db.new_id("ana"), reel_id=rid, user_id=uid, level="deep", visual_format="VF09",
                    sound_primary="SD01", hook_type="bold_claim", tag_quality="vision"))
    after_vf, after_sd = lifts()
    assert after_vf == before_vf and after_sd == before_sd
    assert "VF09" not in after_vf and "SD01" not in after_sd
    assert before_vf["VF04"] > 1.0
    with db.tx(immediate=False) as conn:  # the reel page still shows the deep read's answer
        assert js.effective_tags(conn, uid, [ids[0][0]], data=False)[ids[0][0]]["visual_format"] == "VF09"


def test_sub_format_lift_uses_the_visual_format_base_rate():
    """A sub-format holding every reel of one visual format gets that format's lift (it read 1.0x / was pulled
    toward 1 because ungrouped reels fell out of the base)."""
    us = []
    for acc in ("a", "b", "c"):
        for _ in range(2):
            us.append(U(acc, 5.0, visual_format="VF04", sub_format="fmt_green"))
        for _ in range(2):
            us.append(U(acc, 1.0, w=4.0, visual_format="VF04", sub_format="fmt_green"))
        us.append(U(acc, 5.0, visual_format="VF01"))
        for _ in range(4):
            us.append(U(acc, 1.0, visual_format="VF01"))
        for _ in range(4):
            us.append(U(acc, 1.0, visual_format="VF09"))
    d = ins.compute(us, now=NOW)
    vf = row(d, "visual_format", "VF04")
    sf = row(d, "sub_format", "fmt_green")
    assert sf["n"] == vf["n"] and sf["lift"] == vf["lift"] and vf["lift"] > 1.0
    assert sf["usage"] == vf["usage"]
    # one group that holds every outlier must beat normal
    us2 = [U("a", 5.0, visual_format="VF04", sub_format="fmt_only") for _ in range(4)] + \
          [U(acc, 1.0, visual_format="VF01") for acc in ("a", "b", "c") for _ in range(10)] + \
          [U(acc, 1.0, visual_format="VF04", sub_format="fmt_only") for acc in ("b", "c") for _ in range(3)]
    d2 = ins.compute(us2, now=NOW)
    assert row(d2, "sub_format", "fmt_only")["lift"] > 1.0
    assert all(r["value"] != "_none" for r in d2["rows"]["sub_format"])
