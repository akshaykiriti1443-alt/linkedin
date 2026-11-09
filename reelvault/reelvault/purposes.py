"""Why you saved a reel. Every reel has one or more purposes; the AI guesses, you confirm.

    learn   Learn it          saved for the advice
    hook    Borrow the hook   the first seconds stopped you
    format  Use the format    you like how it is built
    remake  Remake it better  you want your own stronger version
"""

from __future__ import annotations

PURPOSES = {"learn": "Learn it", "hook": "Borrow the hook", "format": "Use the format", "remake": "Remake it better"}

ORDER = list(PURPOSES)


def is_purpose(p) -> bool:
    return isinstance(p, str) and p in PURPOSES


def clean(items) -> list[str]:
    """Known purpose ids only, no repeats, in the standard order."""
    got = {p for p in (items or []) if is_purpose(p)}
    return [p for p in ORDER if p in got]


def clean_guess(guess) -> dict | None:
    """{"purpose": "...", "reason": "..."} or None when the guess is missing or not a known purpose."""
    if not isinstance(guess, dict):
        return None
    p = str(guess.get("purpose") or "").strip().lower()
    if not is_purpose(p):
        return None
    return {"purpose": p, "reason": str(guess.get("reason") or "").strip()}
