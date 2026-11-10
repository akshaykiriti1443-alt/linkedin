"""Loads data/taxonomy.json; ids, labels, cues, legacy map (section 10).

STUB written by WP1 so imports never fail. Owner: WP5. Replace freely; keep the public names.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).parent / "data" / "taxonomy.json"


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(PATH.read_text()) if PATH.exists() else {}
