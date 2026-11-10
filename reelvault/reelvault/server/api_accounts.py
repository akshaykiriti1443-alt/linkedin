"""/api/accounts/*, /api/discover/* (4.8). GET /api/jobs/<id> lives in api_files.py (WP1).

STUB written by WP1 so imports never fail. Owner: WP4. Replace freely; keep the public names.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
