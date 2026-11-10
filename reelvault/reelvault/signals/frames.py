"""Cut-aware frame picking; jpeg_for/sheet_for per destination (6.1.1).

STUB written by WP1 so imports never fail. Owner: WP3. Replace freely; keep the public names.
"""
from __future__ import annotations

from pathlib import Path


def jpeg_for(master: Path, dest: str, *, cap_bytes: int) -> bytes:
    raise NotImplementedError("jpeg_for is not built yet (WP3)")

def sheet_for(reel_dir: Path, dest: str, *, cap_bytes: int) -> bytes:
    raise NotImplementedError("sheet_for is not built yet (WP3)")

def key_frames(frames: list[dict], visual: dict) -> list[dict]:
    raise NotImplementedError("key_frames is not built yet (WP3)")
