"""AI layer for ReelVault Members (DESIGN-SAAS.md 7.1). WP5.

    get_provider(conn_row, *, task) -> Provider        decrypts the key just-in-time; picks model / cheap model
    make_provider(provider, *, api_key, model, ...)    same, without a DB row (key test before saving)
    structured(p, system, parts, model_cls, effort=None, *, max_output_tokens, check=None) -> (model, Usage)
    text(p, system, parts, *, max_output_tokens=4000) -> (str, Usage)
    portable_schema(model_cls, provider) -> dict       a JSON schema every provider accepts (no length/count keywords)

Errors: ai.base.AIError and subclasses (BadKey, RateLimited, QuotaExceeded, Refused, Truncated).
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .base import (
    AIError,
    BadKey,
    Image,
    Provider,
    QuotaExceeded,
    RateLimited,
    Refused,
    Request,
    Result,  # noqa: F401
    SchemaRejected,
    Truncated,
    Usage,
    extract_json,
)

log = logging.getLogger("reelvault.ai")

ALLOWED = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "anyOf",
    "$defs",
    "$ref",
    "description",
    "const",
}
NO_VISION_NOTE = (
    "You cannot see the frames. Use the VisualStats numbers and the local format guess; never infer the "
    "format from the script."
)
CHEAP_TASKS = ("suggest_topics", "retag_topics", "name_formats")


# ============================================================================================== schemas


def _clean(node: Any, *, in_props: bool = False) -> Any:
    if isinstance(node, list):
        return [_clean(v) for v in node]
    if not isinstance(node, dict):
        return node
    if in_props:  # keys are field names here (one may literally be "format" or "title")
        return {k: _clean(v) for k, v in node.items()}
    out = {}
    for k, v in node.items():
        if k not in ALLOWED:
            continue
        out[k] = _clean(v, in_props=k in ("properties", "$defs"))
    if "$ref" in out and len(out) > 1:  # strict modes reject keywords next to $ref
        out = {"$ref": out["$ref"]}
    return out


def _inline(node: Any, defs: dict, stack: tuple = ()) -> Any:
    if isinstance(node, list):
        return [_inline(v, defs, stack) for v in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].split("/")[-1]
        if name in stack:
            raise ValueError(f"recursive schema {name} cannot be inlined")
        return _inline(copy.deepcopy(defs[name]), defs, stack + (name,))
    out = {}
    for k, v in node.items():
        if k == "$defs":
            continue
        if k == "const":
            out["enum"] = [v]
            continue
        out[k] = (
            _inline(v, defs, stack) if k != "properties" else {pk: _inline(pv, defs, stack) for pk, pv in v.items()}
        )
    return out


def portable_schema(model_cls: type[BaseModel], provider: str) -> dict:
    """brain.strict_schema (closed objects, every field required) minus every keyword outside the allowlist.
    gemini: $ref inlined (no $defs) and const -> one-value enum. Lengths/counts live in the field guide."""
    from ..brain import strict_schema

    s = _clean(strict_schema(model_cls.model_json_schema()))
    if provider == "gemini":
        s = _inline(s, s.get("$defs", {}))
    return s


# ============================================================================================== providers


def _allow_private() -> bool:
    try:
        from ..server import db

        return bool(db.get_setting("allow_local_ai_urls"))
    except Exception:  # noqa: BLE001 - outside the server (desktop) there is no setting
        return False


def make_provider(
    provider: str,
    *,
    api_key: str | None,
    model: str,
    preset: str | None = None,
    base_url: str | None = None,
    options: dict | None = None,
    cheap_model: str | None = None,
    http_client=None,
    allow_private: bool | None = None,
    on_mode_change: Callable[[str], None] | None = None,
    vision: bool | None = None,
    max_images: int | None = None,
    timeout: float = 180.0,
) -> Provider:
    from . import presets

    opts = options or {}
    vis = vision if vision is not None else opts.get("vision")
    if vis is None:
        vis = presets.model_vision(provider, model, preset)
    mi = int(max_images or opts.get("max_images") or presets.max_images(provider, model, preset))
    if provider == "anthropic":
        from .anthropic_p import AnthropicProvider

        return AnthropicProvider(
            api_key or "",
            model,
            vision=bool(vis),
            max_images=mi,
            http_client=http_client,
            cheap_model=cheap_model or presets.cheap_model("anthropic"),
            timeout=timeout,
        )
    if provider == "gemini":
        from .gemini_p import GeminiProvider

        return GeminiProvider(
            api_key or "", model, vision=bool(vis), max_images=mi, http_client=http_client, timeout=timeout
        )
    if provider == "openai":
        from .openai_p import OpenAIProvider

        return OpenAIProvider(
            api_key or "", model, vision=bool(vis), max_images=mi, http_client=http_client, timeout=timeout
        )
    if provider == "openai_compat":
        import httpx

        from .compat_p import CompatProvider
        from .net import PinnedTransport

        pr = presets.preset_info(preset) or {}
        url = base_url or pr.get("base_url")
        if not url:
            raise AIError("This connection has no address. Add it again in Settings > AI.")
        ap = _allow_private() if allow_private is None else allow_private
        if http_client is None:
            http_client = httpx.Client(
                transport=PinnedTransport(allow_private=ap), timeout=httpx.Timeout(timeout, connect=10.0)
            )
        extra_body, headers = None, None
        if preset == "openrouter":
            prov = {"require_parameters": True}
            if opts.get("no_training", True):
                prov["data_collection"] = "deny"
            extra_body = {"provider": prov}
            headers = {"X-Title": "ReelVault Members"}
        return CompatProvider(
            api_key,
            model,
            url,
            label=pr.get("label") or "your AI provider",
            vision=bool(vis),
            max_images=mi,
            json_mode=opts.get("json_mode") or pr.get("json_mode") or "schema",
            extra_body=extra_body,
            extra_headers=headers,
            http_client=http_client,
            on_mode_change=on_mode_change,
            timeout=timeout,
        )
    raise AIError(f"Unknown AI provider {provider!r}.")


def _row(conn_row) -> dict:
    return dict(conn_row._mapping) if hasattr(conn_row, "_mapping") else dict(conn_row)


def pick_model(conn: dict, task: str) -> tuple[str, bool | None]:
    """(model id, vision override or None) for a task (7.1.4)."""
    from . import presets

    main, cheap = conn["model"], conn.get("cheap_model")
    opts = conn.get("options") or {}
    if task in CHEAP_TASKS and cheap:
        return cheap, None
    if task == "tag_reels" and cheap and presets.model_vision(conn["provider"], cheap, conn.get("preset")):
        return cheap, True
    return main, opts.get("vision")


def get_provider(conn_row, *, task: str) -> Provider:
    """Provider for one task on one ai_connections row. Decrypts the key here, right before the call (11.3)."""
    from ..server import security

    c = _row(conn_row)
    key = None
    if c.get("key_ciphertext"):
        try:
            key = security.decrypt_secret(
                c["key_ciphertext"],
                c["key_nonce"],
                c["key_kid"],
                security.connection_aad(c["user_id"], c["id"], c["provider"]),
            )
        except security.KeyUnavailable as e:
            raise BadKey(e.message) from None
    model, vis = pick_model(c, task)

    def remember(mode: str) -> None:
        from ..server import db

        with db.tx() as tx:
            row = tx.execute(db.ai_connections.select().where(db.ai_connections.c.id == c["id"])).first()
            if row is not None:
                tx.execute(
                    db.ai_connections.update()
                    .where(db.ai_connections.c.id == c["id"])
                    .values(options={**(row.options or {}), "json_mode": mode})
                )

    opts = dict(c.get("options") or {})
    if model != c["model"]:
        opts.pop("vision", None)
    p = make_provider(
        c["provider"],
        api_key=key,
        model=model,
        preset=c.get("preset"),
        base_url=c.get("base_url"),
        options=opts,
        cheap_model=c.get("cheap_model"),
        on_mode_change=remember,
        vision=vis,
    )
    p.conn_id = c["id"]  # type: ignore[attr-defined]
    p.secret = key  # type: ignore[attr-defined]  (for security.redact of error text; never logged)
    return p


# ============================================================================================== calls


def _validation_text(e: Exception) -> str:
    return str(e)[:1500]


def structured(
    p: Provider,
    system: str,
    parts: list,
    model_cls: type[BaseModel],
    effort: str | None = None,
    *,
    max_output_tokens: int = 16000,
    check: Callable[[BaseModel], None] | None = None,
    retry_note: str | None = None,
) -> tuple[BaseModel, Usage]:
    """JSON-schema output, pydantic validation (+ optional cross-check), one retry with the error fed back.

    Images: a text-only provider gets none (and the no-vision note); more images than p.max_images raises, because
    the CALLER sizes batches (6.1.1). Truncated answers get one shorter retry.
    """
    schema = portable_schema(model_cls, p.name)
    parts = list(parts)
    if not p.vision:
        parts = [x for x in parts if not isinstance(x, Image)]
        parts.append(NO_VISION_NOTE)
    n_img = sum(1 for x in parts if isinstance(x, Image))
    if n_img > p.max_images:
        raise AIError(f"Too many images for this AI ({n_img} > {p.max_images}).")
    req = Request(
        system=system,
        parts=parts,
        schema=schema,
        schema_name=model_cls.__name__,
        max_output_tokens=max_output_tokens,
        effort=effort,
        retry_note=retry_note,
    )
    total = Usage()
    last: str = ""
    for attempt in range(2):
        try:
            res = p.generate(req)
        except Truncated:
            if attempt:
                raise
            req.retry_note = (
                "Your last answer was cut off because it was too long. Return the complete JSON again "
                "and keep every text field short."
            )
            continue
        total = total.add(res.usage)
        try:
            obj = model_cls.model_validate(extract_json(res.text))
            if check is not None:
                check(obj)
            return obj, total
        except (ValueError, ValidationError) as e:  # pydantic errors and cross-check errors (work.Invalid)
            last = _validation_text(e)
            req.retry_note = f"Your previous answer was not valid:\n{last}\nReturn the complete JSON again."
    err = AIError("The AI's answer didn't fit the format twice.", detail=last)
    err.usage = total  # type: ignore[attr-defined]
    raise err


def text(p: Provider, system: str, parts: list, *, max_output_tokens: int = 4000) -> tuple[str, Usage]:
    parts = [x for x in parts if p.vision or not isinstance(x, Image)]
    res = p.generate(Request(system=system, parts=parts, max_output_tokens=max_output_tokens, effort=None))
    return res.text, res.usage


def schema_size(model_cls: type[BaseModel], provider: str = "anthropic") -> int:
    return len(json.dumps(portable_schema(model_cls, provider), separators=(",", ":")))
