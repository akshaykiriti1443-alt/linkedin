"""OpenAI-compatible adapter: OpenRouter, Groq, Ollama Cloud, local Ollama / LM Studio, Gemini's /openai/ endpoint,
custom hosts (DESIGN-SAAS.md 7.1.2). WP5.

chat.completions.create(model, messages=[system, user: text + image_url data URLs], response_format=json_schema
strict, max_tokens). JSON mode steps down automatically, schema -> json_object -> prompt, when the host answers
400 about response_format or a schema keyword; `on_mode_change(mode)` lets the caller remember the working mode
on the connection (options.json_mode).

Network: built by ai.get_provider with `http_client=httpx.Client(transport=PinnedTransport(...))` so the SDK never
re-resolves DNS after the SSRF check (11.8).
"""

from __future__ import annotations

from typing import Callable

from .base import AIError, Image, Refused, Request, Result, SchemaRejected, Truncated, Usage, schema_prompt
from .openai_p import map_openai_error

STEP_DOWN = {"schema": "json_object", "json_object": "prompt"}


class CompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        api_key: str | None,
        model: str,
        base_url: str,
        *,
        label: str = "your AI provider",
        vision: bool = True,
        max_images: int = 10,
        json_mode: str = "schema",
        extra_body: dict | None = None,
        extra_headers: dict | None = None,
        http_client=None,
        timeout: float = 180.0,
        on_mode_change: Callable[[str], None] | None = None,
    ):
        import openai

        self._sdk = openai
        self.client = openai.OpenAI(
            api_key=api_key or "none",
            base_url=base_url,
            max_retries=0,
            timeout=timeout,
            http_client=http_client,
            default_headers=extra_headers or None,
        )
        self.model = model
        self.base_url = base_url
        self.label = label
        self.vision = vision
        self.max_images = max_images
        self.json_mode = json_mode if json_mode in ("schema", "json_object", "prompt") else "schema"
        self.extra_body = extra_body or {}
        self.on_mode_change = on_mode_change

    def _call(self, req: Request) -> Result:
        system = req.system
        content: list[dict] = [{"type": "text", "text": p} for p in req.parts if isinstance(p, str)]
        content += [
            {"type": "image_url", "image_url": {"url": f"data:{p.mime};base64,{p.b64()}"}}
            for p in req.parts
            if isinstance(p, Image)
        ]
        kw: dict = {}
        if req.schema and self.json_mode == "schema":
            kw["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": req.schema_name[:64], "strict": True, "schema": req.schema},
            }
        elif req.schema:
            if self.json_mode == "json_object":
                kw["response_format"] = {"type": "json_object"}
            system += schema_prompt(req.schema)
        if req.retry_note:
            content.append({"type": "text", "text": req.retry_note})
        user_content: list[dict] | str = content
        if all(c["type"] == "text" for c in content):
            user_content = "\n\n".join(c["text"] for c in content)  # widest compatibility for text-only hosts
        try:
            r = self.client.chat.completions.create(
                model=self.model,
                max_tokens=req.max_output_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user_content}],
                extra_body=self.extra_body or None,
                **kw,
            )
        except Exception as e:  # noqa: BLE001
            raise map_openai_error(self._sdk, e, self.label) from None
        if not getattr(r, "choices", None):
            raise AIError(f"{self.label} sent an empty answer. Try again later.")
        ch = r.choices[0]
        if ch.finish_reason == "length":
            raise Truncated("The answer was too long and got cut off.")
        if getattr(ch.message, "refusal", None):
            raise Refused(f"{self.label} declined this request. Try a different reel or topic.")
        u = getattr(r, "usage", None)
        usage = (
            Usage(int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0))
            if u
            else Usage()
        )
        return Result(ch.message.content or "", usage, getattr(r, "model", None) or self.model, "end")

    def generate(self, req: Request) -> Result:
        while True:
            try:
                return self._call(req)
            except SchemaRejected:
                nxt = STEP_DOWN.get(self.json_mode) if req.schema else None
                if not nxt:
                    raise
                self.json_mode = nxt
                if self.on_mode_change:
                    try:
                        self.on_mode_change(nxt)
                    except Exception:  # noqa: BLE001 - remembering the mode is best effort
                        pass

    def test(self) -> str:
        """7.1.3: GET {base}/models, then a 1-token chat when listing is not supported."""
        try:
            self.client.models.list()
            return f"Works. {self.label} answered."
        except Exception as e:  # noqa: BLE001
            err = map_openai_error(self._sdk, e, self.label)
            if err.code == "bad_key":
                raise err from None
        try:
            self.client.chat.completions.create(
                model=self.model, max_tokens=1, messages=[{"role": "user", "content": "Say ok"}]
            )
        except Exception as e:  # noqa: BLE001
            raise map_openai_error(self._sdk, e, self.label) from None
        return f"Works. {self.label} answered."
