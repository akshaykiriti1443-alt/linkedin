"""Pure functions: maturity, score_account, bands, badges (6.6).

STUB written by WP1 so imports never fail. Owner: WP4. Replace freely; keep the public names.
"""
from __future__ import annotations


def maturity(age_days: float) -> float:
    raise NotImplementedError("maturity is not built yet (WP4)")

def score_account(reels: list, now) -> list:
    raise NotImplementedError("score_account is not built yet (WP4)")

def band_label(band: str) -> str:
    raise NotImplementedError("band_label is not built yet (WP4)")

def why(score) -> str:
    raise NotImplementedError("why is not built yet (WP4)")

def pick_sample(scored: list, *, per_account: int = 25, strata: int = 5, seed: str) -> list:
    raise NotImplementedError("pick_sample is not built yet (WP4)")
