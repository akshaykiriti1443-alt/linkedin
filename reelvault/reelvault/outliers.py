"""Outlier scoring within one account, tiers for media work, and discovery ranking (DESIGN-SAAS.md 6.0, 6.6, 6.6.1).

Pure functions only: no database, no clock (callers pass `now`). WP4 owns this file. Ported and extended from the
research prototype `saas/outlier/outliers.py`.

    scores = score_account([ReelMetrics(...), ...], now)        # one account's reels -> list[Score]
    tiers = pick_sample(scores, per_account=25, strata=5, seed="acc_x:pul_y", previous={...})
    why(score) -> "4.4x this creator's normal views · 1.6x their usual likes per view · 41 days old (fully grown)"
"""
from __future__ import annotations

import bisect
import hashlib
import math
import random
import re
import statistics as st
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# ---------------------------------------------------------------------------------------------- constants (6.6)

MIN_AGE_DAYS = 3.0          # younger reels are "growing": no score
WINDOW = 30                 # nearest eligible reels used for the baseline
MIN_PEERS = 5               # fewer -> "unscored"
MATURITY_POINTS = ((0.0, 0.30), (1.0, 0.30), (3.0, 0.65), (7.0, 0.85), (14.0, 0.95), (30.0, 1.00))
HIT = 3.0                   # O >= 3 is an outlier (insights hit rule)

BANDS = (("breakout", 10.0), ("big", 5.0), ("outlier", 3.0), ("above", 2.0), ("normal", 0.5), ("below", 0.0))
BAND_LABELS = {"below": "Below normal", "normal": "Normal", "above": "Above normal", "outlier": "Outlier",
               "big": "Big outlier", "breakout": "Breakout", "growing": "Still growing",
               "unscored": "Not enough reels yet"}
BAND_COLORS = {"below": "grey", "normal": "grey", "above": "blue", "outlier": "green", "big": "purple",
               "breakout": "red", "growing": "grey", "unscored": "grey"}
BAND_ORDER = ("below", "normal", "above", "outlier", "big", "breakout")
OUTLIER_BANDS = ("outlier", "big", "breakout")
BADGE_LABELS = {"loved": "Loved by viewers", "conversation": "Gets people talking", "hidden_gem": "Hidden gem",
                "reach": "Reached beyond followers"}

KEYWORD_CTA_RE = re.compile(r"(?i:comment)\s+[\"“'‘]?[A-Z]{2,}")

PER_PULL = 100              # one pull = up to 100 reels; each pull's worth of reels adds `strata` new strata (6.0)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------------------------- data shapes

@dataclass
class ReelMetrics:
    """What scoring needs about one reel of one account."""

    id: str
    taken_at: datetime | None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    is_pinned: bool = False
    owned: bool = True              # role owner on this account AND not a collab (has no coauthors)
    is_collab: bool = False
    followers_at_pull: int | None = None
    caption: str | None = None
    cta_type: str | None = None     # effective quick-tag CTA (for the conversation badge exclusion)
    starred: bool = False


@dataclass
class Score:
    reel_id: str
    taken_at: datetime | None
    basis: str | None               # views | likes | None
    age_days: float | None
    adj: float | None               # metric / maturity(age)  (stored as reels.adj_views)
    baseline: float | None
    baseline_n: int | None
    score: float | None             # O
    log2_score: float | None
    percentile: float | None
    band: str                       # below normal above outlier big breakout growing unscored
    confidence: str | None
    is_top10: bool = False
    rel_er: float | None = None
    rel_comment_ratio: float | None = None
    badges: list[str] = field(default_factory=list)
    owned: bool = True
    is_pinned: bool = False
    is_collab: bool = False
    starred: bool = False
    views: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Tier:
    reel_id: str
    tier: str                       # priority | sample | rest | none
    weight: float                   # 1.0 priority; N_stratum / n_sampled for sample; 0 rest/none
    stratum: int | None = None


# ---------------------------------------------------------------------------------------------- maturity, bands

def maturity(age_days: float) -> float:
    """Share of lifetime reach a reel has at this age: piecewise linear through MATURITY_POINTS, 1.0 after 30 d."""
    a = max(0.0, float(age_days))
    prev_d, prev_m = MATURITY_POINTS[0]
    for d, m in MATURITY_POINTS[1:]:
        if a <= d:
            if d == prev_d:
                return m
            return prev_m + (m - prev_m) * (a - prev_d) / (d - prev_d)
        prev_d, prev_m = d, m
    return 1.0


def band_for(o: float) -> str:
    for name, t in BANDS:
        if o >= t:
            return name
    return "below"


def band_label(band: str) -> str:
    return BAND_LABELS.get(band or "", "Not scored")


def band_rank(band: str | None) -> int:
    """below 0 .. breakout 5; growing/unscored/None -1 (for 'band >= outlier' checks)."""
    return BAND_ORDER.index(band) if band in BAND_ORDER else -1


def confidence_for(n: int) -> str:
    return "high" if n >= 15 else ("medium" if n >= 8 else "low")


def has_keyword_cta(caption: str | None) -> bool:
    return bool(caption) and bool(KEYWORD_CTA_RE.search(caption or ""))


# ---------------------------------------------------------------------------------------------- scoring (6.6)

def _metric(r: ReelMetrics, basis: str) -> float | None:
    v = r.views if basis == "views" else r.likes
    return float(v) if v is not None and v >= 0 else None


def _percentiles(values: dict[str, float], ref: list[tuple[str, float]]) -> dict[str, float]:
    """percentile of x = 100 * (others below + others equal / 2) / len(others); others = ref minus x itself.
    For a reel inside ref this is the average-rank percentile (top = 100, bottom = 0, ties averaged)."""
    out = {}
    ref_vals = sorted(v for _, v in ref)
    ref_ids = {k for k, _ in ref}
    for rid, x in values.items():
        lo = bisect.bisect_left(ref_vals, x)
        hi = bisect.bisect_right(ref_vals, x)
        less, eq = lo, hi - lo
        n_others = len(ref_vals)
        if rid in ref_ids:
            eq -= 1
            n_others -= 1
        out[rid] = 100.0 if n_others <= 0 else round(100.0 * (less + eq / 2.0) / n_others, 2)
    return out


def score_account(reels: Iterable[ReelMetrics], now: datetime) -> list[Score]:
    """Score every reel of ONE account against its own normal reel (6.6). Returns one Score per input reel."""
    now = _aware(now)
    rs = list(reels)
    age: dict[str, float | None] = {}
    for r in rs:
        age[r.id] = None if r.taken_at is None else max(0.0, (now - _aware(r.taken_at)).total_seconds() / 86400.0)

    def basis_of(r: ReelMetrics) -> str | None:
        if r.views is not None and r.views >= 0:
            return "views"
        if r.likes is not None and r.likes >= 0:
            return "likes"
        return None

    def adj_of(r: ReelMetrics, basis: str) -> float | None:
        m, a = _metric(r, basis), age[r.id]
        if m is None or a is None:
            return None
        return m / maturity(a)

    def eligible(r: ReelMetrics, basis: str) -> bool:
        a = age[r.id]
        return (r.owned and not r.is_collab and not r.is_pinned and a is not None and a >= MIN_AGE_DAYS
                and _metric(r, basis) is not None)

    peers_by_basis: dict[str, list[tuple[float, str, float]]] = {}
    for b in ("views", "likes"):
        lst = []
        for r in rs:
            if eligible(r, b):
                lst.append((_aware(r.taken_at).timestamp(), r.id, adj_of(r, b)))
        lst.sort()
        peers_by_basis[b] = lst

    # secondary-signal medians over eligible peers (account level)
    er_vals, cr_vals = [], []
    for r in rs:
        if not eligible(r, "views"):
            continue
        if r.views is not None and r.views >= 1000 and r.likes is not None and r.comments is not None:
            er_vals.append((r.likes + r.comments) / r.views)
    for r in rs:
        if not (r.owned and not r.is_collab and not r.is_pinned and age[r.id] is not None
                and age[r.id] >= MIN_AGE_DAYS):
            continue
        if r.likes is not None and r.likes >= 20 and r.comments is not None:
            cr_vals.append(r.comments / r.likes)
    med_er = st.median(er_vals) if er_vals else None
    med_cr = st.median(cr_vals) if cr_vals else None

    out: list[Score] = []
    for r in rs:
        b = basis_of(r)
        a = age[r.id]
        s = Score(reel_id=r.id, taken_at=r.taken_at, basis=b, age_days=None if a is None else round(a, 3),
                  adj=None, baseline=None, baseline_n=None, score=None, log2_score=None, percentile=None,
                  band="unscored", confidence=None, owned=r.owned and not r.is_collab, is_pinned=r.is_pinned,
                  is_collab=r.is_collab, starred=r.starred, views=r.views)
        # badges that do not need O
        if r.views is not None and r.views >= 1000 and r.likes is not None and r.comments is not None and med_er:
            s.rel_er = round(((r.likes + r.comments) / r.views) / med_er, 3)
        if r.likes is not None and r.likes >= 20 and r.comments is not None and med_cr:
            s.rel_comment_ratio = round((r.comments / r.likes) / med_cr, 3)
        if b is not None and a is not None:
            s.adj = adj_of(r, b)
        if b is None or a is None:
            s.band = "unscored"
        elif a < MIN_AGE_DAYS:
            s.band = "growing"
        else:
            me_ts = _aware(r.taken_at).timestamp()
            cands = [(abs(ts - me_ts), ts, adj) for ts, rid, adj in peers_by_basis[b] if rid != r.id]
            cands.sort(key=lambda x: (x[0], x[1]))  # ties -> older first (smaller timestamp)
            peers = cands[:WINDOW]
            s.baseline_n = len(peers)
            if len(peers) >= MIN_PEERS:
                base = st.median(p[2] for p in peers)
                s.baseline = base
                s.confidence = confidence_for(len(peers))
                if base and base > 0 and s.adj is not None:
                    o = s.adj / base
                    s.score = o
                    s.log2_score = math.log2(o) if o > 0 else None
                    s.band = band_for(o)
                else:
                    s.band, s.confidence = "unscored", None
            else:
                s.band = "unscored"
        badges = []
        if s.rel_er is not None and s.rel_er >= 1.5:
            badges.append("loved")
        if (s.rel_comment_ratio is not None and s.rel_comment_ratio >= 2 and r.cta_type != "comment_keyword"
                and not has_keyword_cta(r.caption)):
            badges.append("conversation")
        if s.score is not None and 0.7 <= s.score < 2 and s.rel_er is not None and s.rel_er >= 2:
            badges.append("hidden_gem")
        if r.views is not None and r.followers_at_pull and r.followers_at_pull > 0 and r.views >= r.followers_at_pull:
            badges.append("reach")
        s.badges = badges
        out.append(s)

    scored = {s.reel_id: s.score for s in out if s.score is not None}
    ref = [(s.reel_id, s.score) for s in out if s.score is not None and s.owned]
    if not ref:
        ref = [(k, v) for k, v in scored.items()]
    pct = _percentiles(scored, ref)
    for s in out:
        if s.score is not None:
            s.percentile = pct[s.reel_id]
            s.is_top10 = s.percentile >= 90.0
    return out


def baseline_views(reels: Iterable[ReelMetrics], now: datetime) -> int | None:
    """accounts.baseline_views: median adj views of all eligible reels (owned, not collab/pinned, >= 3 d)."""
    now = _aware(now)
    vals = []
    for r in reels:
        if not r.owned or r.is_collab or r.is_pinned or r.taken_at is None or r.views is None:
            continue
        a = (now - _aware(r.taken_at)).total_seconds() / 86400.0
        if a >= MIN_AGE_DAYS:
            vals.append(r.views / maturity(a))
    return int(round(st.median(vals))) if vals else None


# ---------------------------------------------------------------------------------------------- copy

def _fmt_x(v: float) -> str:
    return f"{v:.1f}x"


def why(score: Score | dict, *, now: datetime | None = None) -> str:
    """Plain-words line for a scored reel (6.6 step 10)."""
    s = score if isinstance(score, dict) else score.to_dict()
    band = s.get("band")
    age = s.get("age_days")
    if band == "growing":
        days = max(1, math.ceil(MIN_AGE_DAYS - float(age or 0)))
        return f"Still growing. Check back in {days} day{'s' if days != 1 else ''}."
    o = s.get("score")
    if o is None:
        if s.get("basis") is None:
            return "Instagram hides the numbers on this reel."
        return "Not enough reels yet to know what normal looks like here."
    what = "views" if s.get("basis") == "views" else "likes"
    parts = [f"{_fmt_x(o)} this creator's normal {what}" + (" (scored on likes)" if what == "likes" else "")]
    rel = s.get("rel_er")
    if rel is not None:
        parts.append(f"{_fmt_x(rel)} their usual likes per view")
    if age is not None:
        d = int(age)
        grown = "fully grown" if age >= 30 else "still growing a little"
        parts.append(f"{d} day{'s' if d != 1 else ''} old ({grown})")
    return " · ".join(parts)


def normal_line(baseline: int | None, n: int | None) -> str:
    if not baseline:
        return "We need at least 5 reels older than 3 days to know what a normal reel gets here."
    n = n or 0
    return f"A normal reel here gets about {baseline:,} views (based on {n} nearby reels)."


# ---------------------------------------------------------------------------------------------- tiers (6.0)

def _owned_ok(s: Score) -> bool:
    return s.owned and not s.is_pinned and not s.is_collab


def best_for_deep(scored: list[Score], n: int = 5) -> list[str]:
    """Top n reels for a deep read: band >= outlier by score, filled up with top-10% reels (owned, not pinned/collab)."""
    pool = [s for s in scored if s.score is not None and _owned_ok(s)]
    pool.sort(key=lambda s: (-s.score, s.reel_id))
    top = [s.reel_id for s in pool if band_rank(s.band) >= band_rank("outlier")][:n]
    if len(top) < n:
        for s in pool:
            if len(top) >= n:
                break
            if s.is_top10 and s.reel_id not in top:
                top.append(s.reel_id)
    return top


def is_priority(s: Score, best: set[str]) -> bool:
    if s.is_collab:
        return bool(s.starred) and s.score is not None
    if not _owned_ok(s) or s.score is None:
        return False
    return band_rank(s.band) >= band_rank("outlier") or s.is_top10 or s.reel_id in best


def _seed_int(*parts: Any) -> int:
    return int.from_bytes(hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()[:8], "big")


def strata_of(eligible: list[Score], strata: int = 5, *, per_pull: int = PER_PULL,
              n_account: int | None = None) -> dict[str, int]:
    """Assign eligible non-priority reels to equal time strata by taken_at quantile (oldest stratum = 0).
    The number of strata grows with the account's reels: `strata` per `per_pull` reels (one pull = new strata).
    Deterministic for the same inputs; used by pick_sample and again at insight time (6.7 weights)."""
    items = sorted((s for s in eligible if s.taken_at is not None),
                   key=lambda s: (_aware(s.taken_at).timestamp(), s.reel_id))
    if not items:
        return {}
    total = n_account if n_account is not None else len(items)
    k = max(1, strata * max(1, int(total / per_pull + 0.5)))  # round, so a refresh of a few reels adds no strata
    k = min(k, len(items))
    out = {}
    n = len(items)
    for i, s in enumerate(items):
        out[s.reel_id] = min(k - 1, (i * k) // n)
    return out


def pick_sample(scored: list[Score], *, per_account: int = 25, strata: int = 5, seed: str,
                previous: dict[str, str] | None = None, best: list[str] | None = None, process_all: bool = False,
                per_pull: int = PER_PULL) -> list[Tier]:
    """Tiers for one account (6.0 stage 2). Returns one Tier per input reel.

    priority = band >= outlier, top 10 %, or best_for_deep (owned, not pinned, not collab; collabs only when starred).
    sample   = per_account / strata reels in each time stratum of the remaining eligible scored reels (all of a
               stratum when it is that small); earlier picks (`previous[reel_id] == "sample"`) are kept. Strata grow
               with the account's reels (`strata` per `per_pull`), so the next pull adds new strata.
    weight   = eligible non-priority reels in the stratum / sampled in the stratum (sums to the eligible count).
    rest     = everything else (weight 0).  none = cannot be scored yet (growing/unscored, never media work).
    process_all = every eligible reel is sampled with weight 1 ("Process everything").
    """
    previous = previous or {}
    best_set = set(best if best is not None else best_for_deep(scored))
    tiers: dict[str, Tier] = {}
    eligible: list[Score] = []
    for s in scored:
        if is_priority(s, best_set):
            tiers[s.reel_id] = Tier(s.reel_id, "priority", 1.0)
        elif s.score is not None and _owned_ok(s):
            eligible.append(s)
        elif s.score is not None:
            tiers[s.reel_id] = Tier(s.reel_id, "rest", 0.0)
        else:
            tiers[s.reel_id] = Tier(s.reel_id, "none", 0.0)
    if process_all:
        for s in eligible:
            tiers[s.reel_id] = Tier(s.reel_id, "sample", 1.0, None)
        return [tiers[s.reel_id] for s in scored]
    per_stratum = max(1, per_account // max(1, strata))
    n_account = len(eligible) + sum(1 for t in tiers.values() if t.tier == "priority")
    st_of = strata_of(eligible, strata, per_pull=per_pull, n_account=n_account)
    groups: dict[int, list[Score]] = {}
    for s in eligible:
        groups.setdefault(st_of.get(s.reel_id, 0), []).append(s)
    for k in sorted(groups):
        members = sorted(groups[k], key=lambda s: s.reel_id)
        if len(members) <= per_stratum:
            picked = list(members)
        else:
            kept = [s for s in members if previous.get(s.reel_id) == "sample"]
            rest = [s for s in members if previous.get(s.reel_id) != "sample"]
            need = max(0, per_stratum - len(kept))
            rng = random.Random(_seed_int(seed, k, len(members)))
            add = rng.sample(rest, min(need, len(rest))) if need else []
            picked = kept + add
        w = len(members) / len(picked) if picked else 0.0
        pid = {s.reel_id for s in picked}
        for s in members:
            if s.reel_id in pid:
                tiers[s.reel_id] = Tier(s.reel_id, "sample", round(w, 6), k)
            else:
                tiers[s.reel_id] = Tier(s.reel_id, "rest", 0.0, k)
    return [tiers[s.reel_id] for s in scored]


# ---------------------------------------------------------------------------------------------- discovery (6.6.1)

def _dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return _aware(v)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    if isinstance(v, str):
        try:
            return _aware(datetime.fromisoformat(v.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def candidate_rank(followers: int | None, items: list[dict], now: datetime) -> dict:
    """Light check of a discovered account from its first clips page (<= 12 reels).
    Returns {median_views_12, posts_per_week, last_post_at, rank_score, not_posting}."""
    now = _aware(now)
    vals, times = [], []
    for it in (items or [])[:12]:
        t = _dt(it.get("taken_at"))
        if t is None:
            continue
        times.append(t)
        v = it.get("views")
        age = (now - t).total_seconds() / 86400.0
        if v is not None and age >= MIN_AGE_DAYS:
            vals.append(float(v) / maturity(age))
    med = st.median(vals) if vals else None
    if times:
        span_w = max(1.0, (max(times) - min(times)).total_seconds() / (7 * 86400.0))
        ppw = round(len(times) / span_w, 2)
        last = max(times)
    else:
        ppw, last = 0.0, None
    not_posting = last is None or (now - last).total_seconds() > 60 * 86400 or len(times) < 4
    rank = None
    if med is not None:
        rank = (math.log10(med + 1) + 0.5 * math.log10(med / max(followers or 0, 1000) + 0.01)
                + 0.2 * min(ppw, 7) / 7)
    if rank is None:
        rank = -50.0
    if not_posting:
        rank -= 100.0
    return {"median_views_12": int(round(med)) if med is not None else None, "posts_per_week": ppw,
            "last_post_at": last, "rank_score": round(rank, 4), "not_posting": not_posting}


def short_number(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            v = n / div
            s = f"{v:.1f}".rstrip("0").rstrip(".") if v < 10 else f"{v:.0f}"
            return f"{s}{suf}"
    return f"{int(n)}"


def candidate_why(c: dict) -> str:
    """'Median 84,000 views on the last 12 reels · posts 4x a week · 120K followers'."""
    parts = []
    if c.get("rank_score") is not None and c.get("rank_score") < -60 and c.get("checked_at"):
        parts.append("Not posting lately")
    if c.get("median_views_12") is not None:
        parts.append(f"Median {int(c['median_views_12']):,} views on the last 12 reels")
    ppw = c.get("posts_per_week")
    if ppw:
        parts.append(f"posts {ppw:.0f}x a week" if ppw >= 1 else "posts less than once a week")
    if c.get("followers") is not None:
        parts.append(f"{short_number(c['followers'])} followers")
    if not c.get("checked_at"):
        parts.append("not checked yet")
    return " · ".join(parts) if parts else "Found in your search"
