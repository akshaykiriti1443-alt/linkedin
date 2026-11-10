"""Pure functions: lift tables, grids, combos, per-competitor signature (6.7).

STUB written by WP1 so imports never fail. Owner: WP4. Replace freely; keep the public names.
"""
from __future__ import annotations


def compute(units: list, *, hit: float = 3.0, alpha: int = 10, window_days: int = 365, recent_days: int = 90) -> dict:
    raise NotImplementedError("compute is not built yet (WP4)")

def sub_formats(units: list) -> list:
    raise NotImplementedError("sub_formats is not built yet (WP4)")

def can_make(vf: str, capabilities: dict) -> dict:
    raise NotImplementedError("can_make is not built yet (WP4)")
