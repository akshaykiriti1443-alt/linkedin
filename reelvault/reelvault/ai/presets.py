"""Provider catalog: ids, labels, default models, help text, vision flags and rough estimates (DESIGN-SAAS.md 4.5,
7.1.2). WP5.

Prices are rough guidance for the "about $N" lines only (never a bill). Update them here when providers change.

    catalog() -> {"providers": [...], "presets": [...]}         GET /api/ai/catalog
    provider_info(provider) / preset_info(preset)
    default_model(provider, preset=None, free_tier=False) / cheap_model(...)
    model_vision(provider, model, preset=None) -> bool
    max_images(provider, model, preset=None) -> int
    estimate(mode, provider=None, model=None, *, reels_to_tag, deep_reads, free_tier=False) -> Estimate
"""

from __future__ import annotations

import math

# ($ per million input tokens, $ per million output tokens)
PRICES = {
    "claude-sonnet-5-5": (3.0, 15.0),
    "claude-opus-5-5": (9.0, 45.0),
    "claude-haiku-5-5": (1.0, 5.0),
    "gemini-3.8-flash": (0.30, 2.50),
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gpt-6.1-terra": (2.0, 8.0),
    "gpt-6-luna": (0.40, 1.60),
}
# Tokens per reel: quick tag (one sheet + numbers + excerpts) and one deep read (frames + full transcript + cues).
TAG_TOKENS = (2600, 450)
DEEP_TOKENS = (12000, 4000)
FREE_TAGS_PER_DAY = 200

PROVIDERS = [
    {
        "id": "gemini",
        "label": "Google Gemini",
        "free": "Free key, no card (not in the EU, EEA, UK or Switzerland)",
        "key_url": "https://aistudio.google.com/apikey",
        "default_model": "gemini-3.8-flash",
        "free_model": "gemini-3.5-flash-lite",
        "cheap_model": "gemini-3.5-flash-lite",
        "max_images": 16,
        "models": [
            {"id": "gemini-3.8-flash", "label": "Gemini Flash (recommended)", "vision": True},
            {"id": "gemini-3.5-flash-lite", "label": "Gemini Flash-Lite (free tier, fastest)", "vision": True},
        ],
        "notes": [
            "Free keys: Google may use your content to improve its products.",
            "In the EU, EEA, UK and Switzerland the free tier is not offered: add billing or pick another option.",
        ],
    },
    {
        "id": "anthropic",
        "label": "Anthropic (Claude)",
        "free": None,
        "key_url": "https://console.anthropic.com/settings/keys",
        "default_model": "claude-sonnet-5-5",
        "cheap_model": "claude-haiku-5-5",
        "max_images": 20,
        "models": [
            {"id": "claude-sonnet-5-5", "label": "Claude Sonnet (recommended)", "vision": True},
            {"id": "claude-opus-5-5", "label": "Claude Opus (best quality, about 3x the cost)", "vision": True},
            {"id": "claude-haiku-5-5", "label": "Claude Haiku (cheapest)", "vision": True},
        ],
        "notes": ["You pay Anthropic for what you use. ReelVault shows rough costs before you start."],
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "free": None,
        "key_url": "https://platform.openai.com/api-keys",
        "default_model": "gpt-6.1-terra",
        "cheap_model": "gpt-6-luna",
        "max_images": 20,
        "models": [
            {"id": "gpt-6.1-terra", "label": "GPT-6.1 Terra (recommended)", "vision": True},
            {"id": "gpt-6-luna", "label": "GPT-6 Luna (cheaper)", "vision": True},
        ],
        "notes": ["You pay OpenAI for what you use."],
    },
    {
        "id": "openai_compat",
        "label": "Other (OpenRouter, Groq, Ollama and more)",
        "free": "Some free models",
        "key_url": None,
        "default_model": None,
        "cheap_model": None,
        "max_images": 10,
        "models": [],
        "notes": ["Pick a preset below. Small free models often break the answer format."],
    },
]

PRESETS = [
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_url": "https://openrouter.ai/keys",
        "json_mode": "schema",
        "vision": True,
        "max_images": 10,
        "default_model": "google/gemini-3.8-flash",
        "local": False,
        "notes": [
            "Free models (':free') allow about 50 requests a day under $10 of credit, 1,000 a day after.",
            "Free hosts may train on what you send.",
        ],
    },
    {
        "id": "groq",
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "key_url": "https://console.groq.com/keys",
        "json_mode": "schema",
        "vision": False,
        "max_images": 3,
        "default_model": "openai/gpt-oss-120b",
        "vision_models": ["qwen/qwen3.8-27b"],
        "local": False,
        "notes": ["Most Groq models can't see images. Visual formats then come from measurements only."],
    },
    {
        "id": "ollama_cloud",
        "label": "Ollama Cloud",
        "base_url": "https://ollama.com/v1",
        "key_url": "https://ollama.com/settings/keys",
        "json_mode": "json_object",
        "vision": False,
        "max_images": 3,
        "default_model": "gpt-oss:120b",
        "local": False,
        "notes": [],
    },
    {
        "id": "ollama_local",
        "label": "Ollama on this server",
        "base_url": "http://localhost:11434/v1",
        "key_url": None,
        "json_mode": "json_object",
        "vision": False,
        "max_images": 3,
        "default_model": "llama3.3",
        "local": True,
        "notes": ["Only on self-hosted servers. Small models (under 7B) often break the format."],
    },
    {
        "id": "lmstudio",
        "label": "LM Studio on this server",
        "base_url": "http://localhost:1234/v1",
        "key_url": None,
        "json_mode": "schema",
        "vision": False,
        "max_images": 3,
        "default_model": "local-model",
        "local": True,
        "notes": ["Only on self-hosted servers. Small models (under 7B) often break the format."],
    },
    {
        "id": "gemini_openai",
        "label": "Gemini (OpenAI-style endpoint)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_url": "https://aistudio.google.com/apikey",
        "json_mode": "schema",
        "vision": True,
        "max_images": 16,
        "default_model": "gemini-3.8-flash",
        "local": False,
        "notes": [],
    },
    {
        "id": "custom",
        "label": "Custom address",
        "base_url": None,
        "key_url": None,
        "json_mode": "schema",
        "vision": False,
        "max_images": 3,
        "default_model": None,
        "local": False,
        "notes": ["The address must start with https://."],
    },
]

VISION_HINTS = (
    "vision",
    "gemini",
    "gpt-4o",
    "gpt-5",
    "gpt-6",
    "claude",
    "qwen-vl",
    "qwen2.5-vl",
    "llava",
    "pixtral",
    "llama-4",
    "qwen3.8-27b",
    "gemma-3",
    "gemma3",
)


def provider_info(provider: str) -> dict | None:
    return next((p for p in PROVIDERS if p["id"] == provider), None)


def preset_info(preset: str | None) -> dict | None:
    return next((p for p in PRESETS if p["id"] == preset), None) if preset else None


def catalog() -> dict:
    provs = []
    for p in PROVIDERS:
        q = dict(p)
        q["models"] = [dict(m, cost_per_reel=cost_line(m["id"])) for m in p["models"]]
        q["estimate"] = estimate(
            "api", p["id"], p.get("default_model"), reels_to_tag=175, deep_reads=25, free_tier=p["id"] == "gemini"
        )
        provs.append(q)
    return {"providers": provs, "presets": [dict(p) for p in PRESETS]}


def default_model(provider: str, preset: str | None = None, *, free_tier: bool = False) -> str | None:
    if provider == "openai_compat":
        pr = preset_info(preset)
        return pr.get("default_model") if pr else None
    p = provider_info(provider) or {}
    if free_tier and p.get("free_model"):
        return p["free_model"]
    return p.get("default_model")


def cheap_model(provider: str, preset: str | None = None) -> str | None:
    if provider == "openai_compat":
        return None
    return (provider_info(provider) or {}).get("cheap_model")


def model_vision(provider: str, model: str | None, preset: str | None = None) -> bool:
    """Can this model see images? Catalog first; for openai_compat a conservative name check."""
    m = (model or "").lower()
    if provider in ("anthropic", "gemini", "openai"):
        return True
    pr = preset_info(preset)
    if pr and m in [v.lower() for v in pr.get("vision_models", [])]:
        return True
    if pr and pr["id"] in ("groq", "ollama_cloud", "ollama_local", "lmstudio", "custom"):
        return any(h in m for h in ("vision", "-vl", "llava", "pixtral", "qwen3.8-27b", "gemma3", "gemma-3"))
    return any(h in m for h in VISION_HINTS)


def max_images(provider: str, model: str | None = None, preset: str | None = None) -> int:
    if provider == "openai_compat":
        pr = preset_info(preset) or {}
        return int(pr.get("max_images") or 3)
    return int((provider_info(provider) or {}).get("max_images") or 10)


def _cost(model: str | None, tags: int, deep: int) -> float | None:
    pr = PRICES.get(model or "")
    if not pr:
        return None
    pin, pout = pr
    t = tags * (TAG_TOKENS[0] * pin + TAG_TOKENS[1] * pout) / 1e6
    d = deep * (DEEP_TOKENS[0] * pin + DEEP_TOKENS[1] * pout) / 1e6
    return t + d


def cost_line(model: str | None) -> str | None:
    c = _cost(model, 1, 0)
    d = _cost(model, 0, 1)
    if c is None or d is None:
        return None
    return f"{_money(c)} a reel tagged, {_money(d)} a deep read"


def _money(v: float) -> str:
    """'about $0.03', or 'less than 1 cent' (fix round 1: '$0.00' read like a bug)."""
    return "less than 1 cent" if v < 0.005 else f"about ${v:.2f}"


def _money(x: float) -> str:
    if x < 1:
        return "under $1"
    return f"about ${math.ceil(x)}"


def estimate(
    mode: str | None,
    provider: str | None = None,
    model: str | None = None,
    *,
    reels_to_tag: int,
    deep_reads: int,
    free_tier: bool = False,
    competitors: int | None = None,
) -> dict:
    """The Estimate JSON of 4.5 (rounded, labelled 'about')."""
    tags, deep = max(0, int(reels_to_tag)), max(0, int(deep_reads))
    who = f"your first {competitors} competitors" if competitors else "your competitors"
    out = {"reels_to_tag": tags, "deep_reads": deep, "lines": [], "cost_usd": None, "chats": None, "days": None}
    if mode == "claude_connector":
        chats = max(1, math.ceil(tags / 24 + deep / 10)) if tags or deep else 0
        out["chats"] = chats
        out["lines"] = [
            f"Your Claude plan: about {chats} Claude chats to finish {who}. Nothing runs until you open Claude.",
            "Want it faster? Add a free Gemini key just for tagging.",
        ]
        return out
    if mode == "desktop":
        hours = max(1, math.ceil((tags * 40 + deep * 150) / 3600)) if tags or deep else 0
        out["lines"] = [
            f"The desktop app works through {tags} reels while it is open: about {hours} hours of work "
            "on your Claude plan.",
            "Nothing runs while the app is closed.",
        ]
        return out
    if provider == "gemini" and free_tier:
        days = round(max(0.5, (tags + deep * 3) / FREE_TAGS_PER_DAY), 1) if tags or deep else 0
        out["days"] = days
        lo, hi = max(1, math.floor(days)), max(1, math.ceil(days * 1.3))
        span = f"{lo}-{hi}" if hi > lo else f"{lo}"
        out["lines"] = [
            f"Free Gemini: about {FREE_TAGS_PER_DAY} reels tagged a day, so your first report in about "
            f"{span} day{'s' if hi > 1 else ''}."
        ]
        out["cost_usd"] = [0, 0]
        return out
    c = _cost(model or default_model(provider or ""), tags, deep)
    label = (provider_info(provider or "") or {}).get("label") or "Your AI"
    if c is None:
        out["lines"] = [f"{label}: the cost depends on the model you pick. Check your provider's prices."]
        return out
    lo, hi = max(0, math.floor(c * 0.7)), max(1, math.ceil(c * 1.4))
    out["cost_usd"] = [lo, hi]
    mins = max(10, math.ceil((tags / 10 * 25 + deep * 60) / 60 / 2))
    when = f"about {math.ceil(mins / 60)} hour{'s' if mins > 60 else ''}" if mins >= 60 else f"about {mins} minutes"
    short = model.split("-")[1].title() if model and model.startswith("claude-") else (model or "")
    out["lines"] = [f"{label}{f' ({short})' if short else ''}: {_money(c)} for {who}, report in {when}."]
    return out
