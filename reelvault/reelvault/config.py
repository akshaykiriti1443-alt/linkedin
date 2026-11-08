"""Settings live in reelvault.toml next to where you run the tool."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CATEGORIES = [
    "Business & Entrepreneurship",
    "Marketing & Sales",
    "Content Creation & Social Media",
    "Personal Finance & Investing",
    "Productivity & Mindset",
    "Tech & AI",
    "Health & Fitness",
    "Food & Recipes",
    "Relationships & Communication",
    "Education & Learning",
    "Lifestyle & Travel",
    "Entertainment & Humor",
    "Other",
]


@dataclass
class Config:
    vault_dir: Path = Path("vault")
    instagram_username: str = ""
    session_file: str = ""
    cookies_file: str = "instagram.cookies.json"   # your Instagram login, saved by the app
    extension_seen: bool = False                   # the ReelVault Chrome extension has checked in
    brain: str = "claude-code"          # "claude-code" (your subscription) or "api" (API key)
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    whisper_model: str = "small"
    max_frames: int = 12
    frame_width: int = 768
    include_images: bool = False
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    profile_file: Path = Path("profile.md")

    # Folder helpers
    @property
    def inbox(self) -> Path:
        return self.vault_dir / "inbox"

    @property
    def library(self) -> Path:
        return self.vault_dir / "library"

    @property
    def db_path(self) -> Path:
        return self.vault_dir / "index.sqlite"

    def profile_text(self) -> str:
        if self.profile_file.exists():
            return self.profile_file.read_text(encoding="utf-8").strip()
        return "No creator profile given. Write for a general audience of small business owners and creators."


def load_config(path: str | os.PathLike = "reelvault.toml") -> Config:
    cfg = Config()
    p = Path(path)
    if p.exists():
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        for key, value in data.items():
            if not hasattr(cfg, key):
                print(f"Ignoring unknown setting '{key}' in {p}")
                continue
            if key in ("vault_dir", "profile_file"):
                value = Path(value)
            setattr(cfg, key, value)
    # Env vars win, so you can override one run without editing the file.
    cfg.instagram_username = os.environ.get("IG_USERNAME", cfg.instagram_username)
    cfg.model = os.environ.get("REELVAULT_MODEL", cfg.model)
    return cfg
