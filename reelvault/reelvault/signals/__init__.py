"""Local signals: process(video, out_dir, opts) -> Signals (6.1-6.5.1).

STUB written by WP1 so imports never fail. Owner: WP3. Replace freely; keep the public names.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Signals:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    transcript: dict = field(default_factory=dict)
    cuts: list = field(default_factory=list)
    frames: list = field(default_factory=list)
    sound: dict = field(default_factory=dict)
    edit: dict = field(default_factory=dict)
    visual: dict = field(default_factory=dict)


def process(video: Path, out_dir: Path, *, ig_audio: 'dict | None', words: bool, whisper_model: str) -> Signals:
    raise NotImplementedError("process is not built yet (WP3)")
