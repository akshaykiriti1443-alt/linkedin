"""Anthropic adapter (DESIGN-SAAS.md 7.1.2). WP5.

messages.create(model, max_tokens, system=[{text, cache_control: ephemeral}], messages=[{user: images..., text}],
output_config={"effort": E, "format": {"type": "json_schema", "schema": S}}). No forced tool_choice and no
`thinking` parameter. Reads the text blocks; stop_reason refusal -> Refused, max_tokens -> Truncated.
"""

from __future__ import annotations

from .base import (
    AIError,
    BadKey,
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

LABEL = "Anthropic"


class AnthropicProvider:
    name = "anthropic"
    json_mode = "schema"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        vision: bool = True,
        max_images: int = 20,
        http_client=None,
        timeout: float = 180.0,
        cheap_model: str | None = None,
    ):
        import anthropic

        self._sdk = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout, http_client=http_client)
        self.model = model
        self.cheap_model = cheap_model or model
        self.vision = vision
        self.max_images = max_images

    # ------------------------------------------------------------------ errors
    def _map(self, e: Exception) -> AIError:
        a = self._sdk
        if isinstance(e, (a.AuthenticationError, a.PermissionDeniedError)):
            return BadKey(f"That key didn't work. Copy it again from {LABEL} and paste it here.")
        if isinstance(e, a.RateLimitError):
            ra = None
            try:
                ra = float(e.response.headers.get("retry-after"))
            except (AttributeError, TypeError, ValueError):
                ra = None
            return RateLimited(f"{LABEL} is busy. ReelVault will try again soon.", retry_after=ra)
        if isinstance(e, a.APIStatusError):
            body = str(getattr(e, "message", "") or e)
            if "credit balance" in body.lower():
                return QuotaExceeded(f"Your {LABEL} account has no credit left. Add credit or pick another option.")
            return plain_http_error(LABEL, e.status_code, body)
        if isinstance(e, (a.APITimeoutError, a.APIConnectionError)):
            return RateLimited(f"{LABEL} did not answer in time. ReelVault will try again soon.")
        return AIError(f"{LABEL} returned an error. Try again later.", detail=str(e)[:300])

    # ------------------------------------------------------------------ calls
    def generate(self, req: Request) -> Result:
        images = [p for p in req.parts if isinstance(p, Image)]
        texts = [p for p in req.parts if isinstance(p, str)]
        content: list[dict] = []
        for im in images:
            content.append({"type": "image", "source": {"type": "base64", "media_type": im.mime, "data": im.b64()}})
        for t in texts:
            content.append({"type": "text", "text": t})
        if req.retry_note:
            content.append({"type": "text", "text": req.retry_note})
        output_config: dict = {}
        if req.effort:
            output_config["effort"] = req.effort
        if req.schema:
            output_config["format"] = {"type": "json_schema", "schema": req.schema}
        kw = {"output_config": output_config} if output_config else {}
        try:
            msg = self.client.messages.create(
                model=self.model,
                max_tokens=req.max_output_tokens,
                system=[{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                **kw,
            )
        except Exception as e:  # noqa: BLE001 - mapped to our classes
            raise self._map(e) from None
        if msg.stop_reason == "refusal":
            raise Refused("Claude declined this request. Try a different reel or topic.")
        if msg.stop_reason == "max_tokens":
            raise Truncated("The answer was too long and got cut off.")
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        u = msg.usage
        cached = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        created = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        return Result(
            text,
            Usage(int(u.input_tokens or 0) + cached + created, int(u.output_tokens or 0), cached),
            msg.model or self.model,
            "end",
        )

    def test(self) -> str:
        """7.1.3: a 1-token call with the cheap model."""
        try:
            self.client.messages.create(
                model=self.cheap_model, max_tokens=1, messages=[{"role": "user", "content": "Say ok"}]
            )
        except Exception as e:  # noqa: BLE001
            raise self._map(e) from None
        return f"Works. {LABEL} answered."
