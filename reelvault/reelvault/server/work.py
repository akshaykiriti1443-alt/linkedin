"""Shared task contexts + save functions (API key, MCP, desktop).

STUB written by WP1 so imports never fail. Owner: WP5. Replace freely; keep the public names.
"""
from __future__ import annotations


class Invalid(Exception):
    """Raised by save_result when the AI output does not validate."""


def ai_executor(user_id: str, kind: str) -> 'str | None':
    raise NotImplementedError("ai_executor is not built yet (WP5)")

def queue_deep(user_id: str, reel_ids: list[str]) -> list[str]:
    raise NotImplementedError("queue_deep is not built yet (WP5)")

def queue_profile_draft(user_id: str) -> str:
    raise NotImplementedError("queue_profile_draft is not built yet (WP5)")

def queue_topics(user_id: str, source: str = 'profile') -> str:
    raise NotImplementedError("queue_topics is not built yet (WP5)")

def queue_retag_topics(user_id: str) -> str:
    raise NotImplementedError("queue_retag_topics is not built yet (WP5)")

def queue_format_names(user_id: str, format_ids: list[str]) -> str:
    raise NotImplementedError("queue_format_names is not built yet (WP5)")

def create_output(user_id: str, kind: str, options: dict, *, purpose=None, brand_id: str = 'me') -> tuple[str, str]:
    raise NotImplementedError("create_output is not built yet (WP5)")

def quick_output(user_id: str, action: str, **refs) -> tuple[str, str]:
    raise NotImplementedError("quick_output is not built yet (WP5)")

def build_task(user_id: str, job_row) -> dict:
    raise NotImplementedError("build_task is not built yet (WP5)")

def task_images(user_id: str, job_row, dest: str) -> list:
    raise NotImplementedError("task_images is not built yet (WP5)")

def lease_tag_batch(conn, user_id: str, job_row, dest: str) -> list[str]:
    raise NotImplementedError("lease_tag_batch is not built yet (WP5)")

def save_result(user_id: str, job_row, output: dict, made_by: dict) -> dict:
    raise NotImplementedError("save_result is not built yet (WP5)")

def next_pending(user_id: str, kinds: list[str]):
    raise NotImplementedError("next_pending is not built yet (WP5)")

def mcp_tag_batch(user_id: str):
    raise NotImplementedError("mcp_tag_batch is not built yet (WP5)")

def mcp_reel_task(user_id: str, reel_id=None):
    raise NotImplementedError("mcp_reel_task is not built yet (WP5)")

def taxonomy_text(part) -> str:
    raise NotImplementedError("taxonomy_text is not built yet (WP5)")
