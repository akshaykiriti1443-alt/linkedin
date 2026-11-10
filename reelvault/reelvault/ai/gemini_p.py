"""Google Gemini adapter, native google-genai (DESIGN-SAAS.md 7.1.2). WP5.

models.generate_content(model, contents=[Content(user, [Part.from_bytes(jpg)..., Part.from_text(prompt)])],
config=GenerateContentConfig(system_instruction, max_output_tokens, response_mime_type="application/json",
response_json_schema=S)). Stateless: nothing is stored by Google's Interactions API.
"""

from __future__ import annotations

from .base import (
    AIError,
    Image,
    QuotaExceeded,
    RateLimited,
    Refused,
    Request,
    Result,
    Truncated,
    Usage,
    plain_http_error,
)

LABEL = "Google Gemini"


class GeminiProvider:
    name = "gemini"
    json_mode = "schema"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        vision: bool = True,
        max_images: int = 16,
        http_client=None,
        timeout: float = 180.0,
    ):
        from google import genai
        from google.genai import types

        self.types = types
        opts = {"timeout": int(timeout * 1000)}
        if http_client is not None:
            opts["httpx_client"] = http_client
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(**opts))
        self.model = model
        self.vision = vision
        self.max_images = max_images

    def _map(self, e: Exception) -> AIError:
        code = getattr(e, "code", None)
        msg = str(getattr(e, "message", "") or e)
        low = msg.lower()
        if code == 429 and ("perday" in low or "per day" in low or "free_tier" in low or "free tier" in low):
            return QuotaExceeded(
                f"Your {LABEL} free limit is used up for today. ReelVault continues tomorrow, "
                "or add billing in Google AI Studio."
            )
        if code == 400 and "location is not supported" in low:
            return QuotaExceeded(
                "The Gemini free tier is not offered where you are. Add billing or pick another option."
            )
        if code == 400 and ("api key not valid" in low or "api_key_invalid" in low):
            return plain_http_error(LABEL, 401, msg)
        if isinstance(code, int):
            return plain_http_error(LABEL, code, msg)
        name = type(e).__name__.lower()
        if "timeout" in name or "connect" in name:
            return RateLimited(f"{LABEL} did not answer in time. ReelVault will try again soon.")
        return AIError(f"{LABEL} returned an error. Try again later.", detail=msg[:300])

    def generate(self, req: Request) -> Result:
        t = self.types
        parts = [t.Part.from_bytes(data=p.data, mime_type=p.mime) for p in req.parts if isinstance(p, Image)]
        parts += [t.Part.from_text(text=p) for p in req.parts if isinstance(p, str)]
        if req.retry_note:
            parts.append(t.Part.from_text(text=req.retry_note))
        cfg = t.GenerateContentConfig(
            system_instruction=req.system,
            max_output_tokens=req.max_output_tokens,
            response_mime_type="application/json" if req.schema else None,
            response_json_schema=req.schema if req.schema else None,
        )
        try:
            r = self.client.models.generate_content(
                model=self.model, contents=[t.Content(role="user", parts=parts)], config=cfg
            )
        except Exception as e:  # noqa: BLE001
            raise self._map(e) from None
        cands = r.candidates or []
        fr = str(getattr(cands[0], "finish_reason", "") or "") if cands else "BLOCKED"
        if "MAX_TOKENS" in fr:
            raise Truncated("The answer was too long and got cut off.")
        if not cands or any(k in fr for k in ("SAFETY", "BLOCK", "PROHIBITED", "RECITATION", "SPII")):
            raise Refused("Gemini blocked this request. Try a different reel or topic.")
        um = r.usage_metadata
        usage = (
            Usage(
                int(getattr(um, "prompt_token_count", 0) or 0),
                int(getattr(um, "candidates_token_count", 0) or 0) + int(getattr(um, "thoughts_token_count", 0) or 0),
                int(getattr(um, "cached_content_token_count", 0) or 0),
            )
            if um
            else Usage()
        )
        return Result(r.text or "", usage, self.model, "end")

    def test(self) -> str:
        """7.1.3: list models (one page)."""
        try:
            pager = self.client.models.list(config={"page_size": 1})
            next(iter(pager), None)
        except Exception as e:  # noqa: BLE001
            raise self._map(e) from None
        return f"Works. {LABEL} answered."
