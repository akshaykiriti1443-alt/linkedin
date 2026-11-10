"""/api/reels/*, /api/insights/*, /api/banks/*, /api/formats/* (4.9).

STUB written by WP1 so imports never fail. Owner: WP4. Replace freely; keep the public names.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


# Optional hook read by WP1's GET /api/home (api_files.py). Return any of:
# {"new_outliers": [...], "cards": [...], "first_look": [...], "formats": [...]}
def home_parts(user_id: str) -> dict:
    return {}
