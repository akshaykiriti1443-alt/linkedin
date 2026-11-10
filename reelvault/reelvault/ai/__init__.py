"""AI layer: get_provider(conn); structured(); text(); portable_schema() (7.1).

STUB written by WP1 so imports never fail. Owner: WP5. Replace freely; keep the public names.
"""
from __future__ import annotations


def portable_schema(model_cls, provider: str) -> dict:
    raise NotImplementedError("portable_schema is not built yet (WP5)")

def structured(*args, **kwargs):
    raise NotImplementedError("structured is not built yet (WP5)")

def text(*args, **kwargs):
    raise NotImplementedError("text is not built yet (WP5)")

def get_provider(conn_row, *, task: str):
    raise NotImplementedError("get_provider is not built yet (WP5)")
