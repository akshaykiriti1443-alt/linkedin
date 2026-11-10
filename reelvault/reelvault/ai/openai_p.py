"""OpenAI adapter, Responses API (DESIGN-SAAS.md 7.1.2). WP5.

responses.create(model, instructions=SYS, input=[{user: input_text, input_image...}], text={"format": {"type":
"json_schema", "name", "strict": True, "schema"}}, max_output_tokens, reasoning={"effort"}, store=False).
status "incomplete" -> Truncated; a refusal content item -> Refused.
"""

from __future__ import annotations

from .base import AIError, BadKey, Image, RateLimited, Refused, Request, Result, Truncated, Usage, plain_http_error

LABEL = "OpenAI"


def map_openai_error(sdk, e: Exception, label: str) -> AIError:
    """Shared by the OpenAI and OpenAI-compatible adapters."""
    if isinstance(e, (sdk.AuthenticationError, sdk.PermissionDeniedError)):
        return BadKey(f"That key didn't work. Copy it again from {label} and paste it here.")
    if isinstance(e, sdk.RateLimitError):
        ra = None
        try:
            ra = float(e.response.headers.get("retry-after"))
        except (AttributeError, TypeError, ValueError):
            ra = None
        err = plain_http_error(label, 429, str(getattr(e, "message", "") or e))
        if isinstance(err, RateLimited):
            err.retry_after = ra
        return err
    if isinstance(e, sdk.APIStatusError):
        return plain_http_error(label, e.status_code, str(getattr(e, "message", "") or e))
    if isinstance(e, (sdk.APITimeoutError, sdk.APIConnectionError)):
        return RateLimited(f"{label} did not answer in time. ReelVault will try again soon.")
    return AIError(f"{label} returned an error. Try again later.", detail=str(e)[:300])


class OpenAIProvider:
    name = "openai"
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
    ):
        import openai

        self._sdk = openai
        self.client = openai.OpenAI(api_key=api_key, max_retries=0, timeout=timeout, http_client=http_client)
        self.model = model
        self.vision = vision
        self.max_images = max_images

    def generate(self, req: Request) -> Result:
        content: list[dict] = [{"type": "input_text", "text": p} for p in req.parts if isinstance(p, str)]
        content += [
            {"type": "input_image", "image_url": f"data:{p.mime};base64,{p.b64()}", "detail": "auto"}
            for p in req.parts
            if isinstance(p, Image)
        ]
        if req.retry_note:
            content.append({"type": "input_text", "text": req.retry_note})
        kw: dict = {}
        if req.schema:
            kw["text"] = {
                "format": {"type": "json_schema", "name": req.schema_name[:64], "strict": True, "schema": req.schema}
            }
        if req.effort:
            kw["reasoning"] = {"effort": req.effort}
        try:
            r = self.client.responses.create(
                model=self.model,
                instructions=req.system,
                input=[{"role": "user", "content": content}],
                max_output_tokens=req.max_output_tokens,
                store=False,
                **kw,
            )
        except Exception as e:  # noqa: BLE001
            raise map_openai_error(self._sdk, e, LABEL) from None
        if getattr(r, "status", None) == "incomplete":
            raise Truncated("The answer was too long and got cut off.")
        for item in getattr(r, "output", None) or []:
            for c in getattr(item, "content", None) or []:
                if getattr(c, "type", "") == "refusal":
                    raise Refused("OpenAI declined this request. Try a different reel or topic.")
        u = getattr(r, "usage", None)
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) if u else 0
        usage = (
            Usage(int(getattr(u, "input_tokens", 0) or 0), int(getattr(u, "output_tokens", 0) or 0), int(cached or 0))
            if u
            else Usage()
        )
        return Result(r.output_text or "", usage, getattr(r, "model", None) or self.model, "end")

    def test(self) -> str:
        try:
            self.client.models.list()
        except Exception as e:  # noqa: BLE001
            raise map_openai_error(self._sdk, e, LABEL) from None
        return f"Works. {LABEL} answered."
