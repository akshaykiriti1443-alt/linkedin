"""Request, Image, Result, Usage, errors and the Provider protocol (DESIGN-SAAS.md 7.1.1). WP5.

Every adapter turns one `Request` into one `Result` and maps its SDK's errors onto the classes below, so the job
runner can decide: BadKey -> connection bad_key + jobs paused; RateLimited -> retry later; QuotaExceeded -> paused;
Refused -> failed with a plain message; Truncated -> one shorter retry, then failed.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


@dataclass
class Image:
    data: bytes
    mime: str = "image/jpeg"
    label: str = ""
    name: str = ""

    def b64(self) -> str:
        return base64.standard_b64encode(self.data).decode()


@dataclass
class Request:
    system: str
    parts: list = field(default_factory=list)  # str | Image, in order (one user turn)
    schema: dict | None = None
    schema_name: str = "Output"
    max_output_tokens: int = 16000
    effort: str | None = "medium"
    retry_note: str | None = None


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cached: int = 0

    def add(self, other: "Usage") -> "Usage":
        return Usage(self.input + other.input, self.output + other.output, self.cached + other.cached)


@dataclass
class Result:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    stop: str = "end"


class AIError(RuntimeError):
    """Any AI failure. `message` is plain words for the member (never contains a key)."""

    code = "ai_error"

    def __init__(self, message: str = "The AI could not answer. Try again later.", *, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


class BadKey(AIError):
    code = "bad_key"


class RateLimited(AIError):
    code = "rate_limited"

    def __init__(
        self,
        message: str = "The AI is busy. ReelVault will try again in a minute.",
        *,
        retry_after: float | None = None,
        detail: str = "",
    ):
        super().__init__(message, detail=detail)
        self.retry_after = retry_after


class QuotaExceeded(AIError):
    code = "quota"


class Refused(AIError):
    code = "refused"


class Truncated(AIError):
    code = "truncated"


class SchemaRejected(AIError):
    """The endpoint refused our response_format / schema (a 400). The caller steps the JSON mode down."""

    code = "schema_rejected"


JsonMode = Literal["schema", "json_object", "prompt"]


@runtime_checkable
class Provider(Protocol):
    name: str
    model: str
    vision: bool
    max_images: int
    json_mode: JsonMode

    def generate(self, req: Request) -> Result: ...


def extract_json(text: str):
    """The first JSON object in an answer. Tolerates ```json fences and prose around it."""
    t = (text or "").strip()
    m = re.search(r"```(?:json|JSON)?\s*\n(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    start = t.find("{")
    if start < 0:
        raise ValueError("The answer has no JSON object.")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start : i + 1])
    end = t.rfind("}")
    if end > start:
        return json.loads(t[start : end + 1])
    raise ValueError("The answer's JSON object is not complete.")


def schema_prompt(schema: dict) -> str:
    """Instruction for providers without native JSON schema support (json_object / prompt modes)."""
    return (
        "\n\nAnswer with ONE JSON object only, no prose and no code fences, matching this JSON schema:\n"
        + json.dumps(schema, separators=(",", ":"))
    )


def plain_http_error(provider_label: str, status: int | None, body: str = "") -> AIError:
    """Map an HTTP status from any provider to our error classes (plain member messages)."""
    low = (body or "").lower()
    if status in (401, 403):
        return BadKey(
            f"That key didn't work. Copy it again from {provider_label} and paste it here.", detail=body[:300]
        )
    if status == 402 or "insufficient_quota" in low or "billing" in low or "credit" in low:
        return QuotaExceeded(
            f"Your {provider_label} account has no credit left. Add credit or pick another option.", detail=body[:300]
        )
    if status == 429:
        if "quota" in low and ("per day" in low or "daily" in low or "perday" in low or "free" in low):
            return QuotaExceeded(
                f"Your {provider_label} free limit is used up for today. ReelVault continues tomorrow, or add billing.",
                detail=body[:300],
            )
        return RateLimited(f"{provider_label} is busy. ReelVault will try again soon.", detail=body[:300])
    if status == 400 and ("response_format" in low or "json_schema" in low or "schema" in low or "structured" in low):
        return SchemaRejected(f"{provider_label} did not accept the answer format.", detail=body[:300])
    if status and status >= 500:
        return RateLimited(f"{provider_label} had a problem. ReelVault will try again soon.", detail=body[:300])
    return AIError(f"{provider_label} returned an error. Try again later.", detail=body[:300])
