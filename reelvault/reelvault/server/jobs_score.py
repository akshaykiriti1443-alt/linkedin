"""Job handlers: score_account, pick_sample, refresh_insights.

STUB written by WP1 so imports never fail. Owner: WP4. Replace freely; keep the public names.
"""
from __future__ import annotations

# Register handlers with: from .jobs import handler ; @handler('score_account', job_class='light')


def report_numbers(user_id: str, account_ids: 'list[str] | None') -> dict:
    raise NotImplementedError("report_numbers is not built yet (WP4)")

def best_for_deep(user_id: str, account_id: str, n: int = 5) -> list[str]:
    raise NotImplementedError("best_for_deep is not built yet (WP4)")

def ingest_candidate(user_id: str, cand_id: str, account: dict, items: list[dict]) -> None:
    raise NotImplementedError("ingest_candidate is not built yet (WP4)")

def start_discover(user_id: str, mode: str, *, query=None, seeds=None) -> str:
    raise NotImplementedError("start_discover is not built yet (WP4)")
