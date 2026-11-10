"""/api/ai/* (AI settings, 4.5) and /api/worker/* (desktop worker, 4.7). WP5.

Session routes (web UI): catalog, estimate, state, mode, connections (key tested before saving), desktop pairing.
Device routes (Authorization: Bearer <desktop token>): hello, lease, files, result, fail. The desktop app's
members_worker.py uses them; every job is filtered by the device's member and lease owner.

Keys: encrypted with security.encrypt_secret (AD = user_id|connection_id|provider), never returned, never logged;
provider error text is redacted before it is stored as last_error.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from .. import ai
from ..ai import base as aib
from ..ai import presets
from . import db, jobs, ratelimit, security, work
from .errors import ApiError, bad_input, not_found, read_body
from .settings import get_settings

log = logging.getLogger("reelvault.api_ai")
router = APIRouter()

MODES = ("api", "claude_connector", "desktop")
MAX_CONNECTIONS = 6
TEST_TIMEOUT_S = 15.0
WORKER_KINDS = work.AI_KINDS
ONLINE_S = 90
NO_VISION_WARNING = (
    "This model can't see images. Visual formats will come from measurements only and be marked "
    "'unverified'. Add a free Gemini key just for tagging to fix that."
)


async def _json(request: Request, limit: int = 64 * 1024) -> dict:
    raw = await read_body(request, limit, "That request is too big.")  # counted while streaming (chunked bodies too)
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise bad_input("The body must be JSON.") from None
    if not isinstance(body, dict):
        raise bad_input("The body must be a JSON object.")
    return body


# ============================================================================================== JSON shapes


def conn_json(c, row) -> dict:
    month = db.utcnow().strftime("%Y-%m")
    u = c.execute(select(db.ai_usage).where(db.ai_usage.c.conn_id == row.id, db.ai_usage.c.month == month)).first()
    user = work.user_row(c, row.user_id)
    opts = row.options or {}
    vision = opts.get("vision")
    if vision is None:
        vision = presets.model_vision(row.provider, row.model, row.preset)
    return {
        "id": row.id,
        "provider": row.provider,
        "preset": row.preset,
        "base_url": row.base_url,
        "model": row.model,
        "cheap_model": row.cheap_model,
        "vision": bool(vision),
        "key": f"••••{row.key_last4}" if row.key_last4 else None,
        "status": row.status,
        "last_tested_at": db.iso(row.last_tested_at),
        "last_error": row.last_error,
        "is_default": bool(row.is_default),
        "use_for": "tagging" if user is not None and user.tagging_conn_id == row.id else "all",
        "options": {k: v for k, v in opts.items() if k in ("effort", "no_training", "free_tier", "max_images")},
        "usage_this_month": {
            "calls": u.calls if u else 0,
            "in": u.tokens_in if u else 0,
            "out": u.tokens_out if u else 0,
        },
    }


def _devices(c, user_id: str) -> list[dict]:
    d = db.devices
    now = db.utcnow()
    out = []
    for row in c.execute(
        select(d)
        .where(d.c.user_id == user_id, d.c.kind == "desktop", d.c.revoked_at.is_(None))
        .order_by(d.c.created_at.desc())
    ):
        st = row.state or {}
        out.append(
            {
                "id": row.id,
                "name": row.name,
                "version": row.version,
                "last_seen_at": db.iso(row.last_seen_at),
                "online": bool(row.last_seen_at and (now - row.last_seen_at).total_seconds() < ONLINE_S),
                "claude": st.get("claude") or {},
                "working": bool(st.get("working", True)),
            }
        )
    return out


def _connector(c, user_id: str) -> dict:
    t = db.oauth_tokens
    last = c.execute(select(func.max(t.c.created_at)).where(t.c.user_id == user_id, t.c.revoked_at.is_(None))).scalar()
    since = db.utcnow() - timedelta(days=7)
    return {
        "url": get_settings().public_url.rstrip("/") + "/mcp",
        "connected": bool(last and last >= since),
        "last_used_at": db.iso(last),
    }


def _queue(user_id: str) -> dict:
    j = db.jobs
    with db.read() as c:
        rows = dict(
            c.execute(
                select(j.c.executor, func.count())
                .where(
                    j.c.user_id == user_id,
                    j.c.kind.in_(work.AI_KINDS),
                    j.c.kind != "tag_reels",
                    j.c.state.in_(("waiting", "running")),
                )
                .group_by(j.c.executor)
            ).all()
        )
        left = int(
            c.execute(
                select(func.count())
                .select_from(db.reels)
                .where(db.reels.c.user_id == user_id, db.reels.c.tag_state.in_(("queued", "leased")))
            ).scalar()
            or 0
        )
    ex = work.ai_executor(user_id, "tag_reels")
    return {
        "waiting_for_claude": int(rows.get("mcp", 0)) + (left if ex == "mcp" else 0),
        "waiting_for_desktop": int(rows.get("desktop", 0)) + (left if ex == "desktop" else 0),
        "reels_left_to_tag": left,
    }


def state(user_id: str) -> dict:
    with db.read() as c:
        u = work.user_row(c, user_id)
        out = {
            "mode": u.ai_mode,
            "tagging_conn_id": u.tagging_conn_id,
            "connections": [conn_json(c, r) for r in work.connections(c, user_id)],
            "connector": _connector(c, user_id),
            "desktop": {"devices": _devices(c, user_id)},
        }
    out["queue"] = _queue(user_id)
    return out


def member_counts(user_id: str, mode: str | None) -> tuple[int, int, int | None]:
    """(reels to tag, deep reads, competitors) for the member's Estimate; 5 assumed competitors when none."""
    r, a = db.reels, db.accounts
    with db.read() as c:
        accs = int(
            c.execute(
                select(func.count()).select_from(a).where(a.c.user_id == user_id, a.c.role == "competitor")
            ).scalar()
            or 0
        )
        tags = int(
            c.execute(
                select(func.count())
                .select_from(r)
                .where(
                    r.c.user_id == user_id,
                    r.c.tag_state != "done",
                    (r.c.tier.in_(("priority", "sample"))) | (r.c.tag_state.in_(("queued", "leased"))),
                )
            ).scalar()
            or 0
        )
        deep = int(
            c.execute(
                select(func.count())
                .select_from(r)
                .where(r.c.user_id == user_id, r.c.deep_state.in_(("queued", "waiting")))
            ).scalar()
            or 0
        )
    sample = 15 if mode in ("claude_connector", "desktop") else 25
    if accs == 0:
        return 5 * (15 + sample), 25, 5
    if tags == 0:
        tags = accs * (15 + sample)
    return tags, max(deep, min(accs * 5, 50)), accs


# ============================================================================================== settings routes


@router.get("/api/ai/catalog")
def catalog(user=Depends(security.current_user)):
    return presets.catalog()


@router.get("/api/ai/estimate")
def estimate(request: Request, user=Depends(security.current_user)):
    q = request.query_params
    mode = q.get("mode") or user.ai_mode or "api"
    if mode not in MODES:
        raise bad_input("mode must be api, claude_connector or desktop.")
    provider = q.get("provider")
    if provider and provider not in ("anthropic", "gemini", "openai", "openai_compat"):
        raise bad_input("Unknown provider.")
    model = q.get("model") or (presets.default_model(provider) if provider else None)
    free = q.get("free_tier") in ("1", "true") or (
        provider == "gemini" and q.get("free_tier") is None and not q.get("model")
    )
    tags, deep, comps = member_counts(user.id, mode)
    return presets.estimate(
        mode, provider, model, reels_to_tag=tags, deep_reads=deep, free_tier=free, competitors=comps
    )


@router.get("/api/ai")
def get_ai(user=Depends(security.current_user)):
    return state(user.id)


@router.put("/api/ai/mode")
async def put_mode(request: Request, user=Depends(security.current_user)):
    body = await _json(request)

    def run():
        mode = body.get("mode")
        if mode is not None and mode not in MODES:
            raise bad_input("Pick one: your Claude plan, an API key, or the desktop app.")
        vals = {"ai_mode": mode}
        with db.tx() as c:
            if "tagging_conn_id" in body:
                tid = body.get("tagging_conn_id")
                if tid is not None and work._get(c, db.ai_connections, user.id, str(tid)) is None:
                    raise not_found("That AI connection is not yours.")
                vals["tagging_conn_id"] = tid
            if mode == "api" and not work.connections(c, user.id):
                raise bad_input("Add an API key first.")
            c.execute(db.users.update().where(db.users.c.id == user.id).values(**vals))
            work.resume_ai_jobs(user.id, conn=c)
        return state(user.id)

    return await run_in_threadpool(run)


def _clean_key(v) -> str | None:
    if v is None or v == "":
        return None
    if not isinstance(v, str) or len(v) > 500 or re.search(r"\s", v.strip()):
        raise bad_input("That key doesn't look right. Copy it again and paste only the key.")
    return v.strip()


def _check_url(preset: str | None, base_url: str | None) -> str | None:
    pr = presets.preset_info(preset) or {}
    allow_local = bool(db.get_setting("allow_local_ai_urls"))
    if pr.get("local") and not allow_local:
        raise bad_input("Local AI servers only work on self-hosted ReelVault or through the desktop app.")
    url = (base_url or "").strip() if preset == "custom" else (pr.get("base_url") or "")
    if not url:
        raise bad_input("Paste the address of your AI provider (it starts with https://).")
    u = urlparse(url)
    if u.scheme != "https" and not (allow_local and u.scheme == "http"):
        raise bad_input("The address must start with https://.")
    if not u.hostname or u.username or u.password or len(url) > 300:
        raise bad_input("That address doesn't look right.")
    if preset == "custom":
        from ..ai.net import UnsafeAddress, resolve_checked

        try:
            resolve_checked(u.hostname, u.port or (443 if u.scheme == "https" else 80), allow_private=allow_local)
        except UnsafeAddress:
            raise bad_input("ReelVault can't call private or local addresses.") from None
        except OSError:
            raise bad_input("We couldn't find that address. Check it and try again.") from None
    return url.rstrip("/")


def _options(body_opts, provider: str, model: str, preset: str | None) -> dict:
    o = body_opts if isinstance(body_opts, dict) else {}
    out = {
        "effort": o.get("effort") if o.get("effort") in ("low", "medium", "high") else "medium",
        "no_training": bool(o.get("no_training", True)),
        "free_tier": bool(o.get("free_tier", False)),
    }
    mi = o.get("max_images")
    if isinstance(mi, int) and 1 <= mi <= 20:
        out["max_images"] = mi
    out["vision"] = presets.model_vision(provider, model, preset)  # members can't switch vision on
    return out


def _test(provider: str, key: str | None, model: str, preset, base_url, options) -> tuple[bool, str, str]:
    """(ok, member message, status). 7.1.3, 15 s timeout."""
    t0 = time.monotonic()
    try:
        p = ai.make_provider(
            provider,
            api_key=key,
            model=model,
            preset=preset,
            base_url=base_url,
            options=options,
            timeout=TEST_TIMEOUT_S,
        )
        p.test()
    except aib.BadKey as e:
        return False, e.message, "bad_key"
    except aib.QuotaExceeded as e:
        return False, e.message, "quota"
    except aib.AIError as e:
        return False, security.redact(e.message, [key] if key else []), "error"
    except Exception as e:  # noqa: BLE001 - never show a stack trace or the key
        log.warning("key test crashed: %s", security.redact(str(e), [key] if key else [])[:200])
        return False, "We couldn't reach that AI provider. Check the address and try again.", "error"
    label = (presets.preset_info(preset) or presets.provider_info(provider) or {}).get("label", "Your AI")
    return True, f"Works. {label} answered in {time.monotonic() - t0:.1f} s.", "ok"


@router.post("/api/ai/connections", status_code=201)
async def create_connection(request: Request, user=Depends(security.current_user)):
    body = await _json(request)
    save_anyway = request.query_params.get("save_anyway") in ("1", "true")

    def run():
        provider = body.get("provider")
        if provider not in ("anthropic", "gemini", "openai", "openai_compat"):
            raise bad_input("Pick an AI provider.")
        preset = body.get("preset") if provider == "openai_compat" else None
        if provider == "openai_compat" and not presets.preset_info(preset):
            raise bad_input("Pick a preset: OpenRouter, Groq, Ollama Cloud, or a custom address.")
        base_url = _check_url(preset, body.get("base_url")) if provider == "openai_compat" else None
        opts_in = body.get("options") if isinstance(body.get("options"), dict) else {}
        model = (
            body.get("model") or presets.default_model(provider, preset, free_tier=bool(opts_in.get("free_tier"))) or ""
        ).strip()
        if not model or len(model) > 120 or not re.fullmatch(r"[\w.:/@+-]+", model):
            raise bad_input("Pick a model.")
        cheap = body.get("cheap_model") or presets.cheap_model(provider, preset) or None
        if cheap and (len(cheap) > 120 or not re.fullmatch(r"[\w.:/@+-]+", cheap)):
            raise bad_input("That second model name doesn't look right.")
        key = _clean_key(body.get("api_key"))
        local = bool((presets.preset_info(preset) or {}).get("local"))
        if key is None and not local:
            raise bad_input("Paste your API key.")
        use_for = body.get("use_for") or "all"
        if use_for not in ("all", "tagging"):
            raise bad_input("use_for must be all or tagging.")
        options = _options(opts_in, provider, model, preset)
        with db.read() as c:
            if len(work.connections(c, user.id)) >= MAX_CONNECTIONS:
                raise bad_input(f"You can keep up to {MAX_CONNECTIONS} AI connections. Delete one first.")
        ratelimit.hit("ai_key_test", user.id, 30, "hour", message="Too many key tests. Try again in an hour.")
        ok, msg, status = _test(provider, key, model, preset, base_url, options)
        if not ok and not save_anyway:
            raise ApiError(400, "bad_input", msg, detail={"test": {"ok": False, "message": msg}})
        cid = db.new_id("aic")
        ct = nonce = kid = None
        if key:
            ct, nonce, kid = security.encrypt_secret(key, security.connection_aad(user.id, cid, provider))
        now = db.utcnow()
        with db.tx() as c:
            has_default = any(r.is_default for r in work.connections(c, user.id))
            is_default = use_for == "all" and (not has_default or bool(body.get("is_default", True)))
            if is_default:
                c.execute(
                    db.ai_connections.update().where(db.ai_connections.c.user_id == user.id).values(is_default=False)
                )
            c.execute(
                db.ai_connections.insert().values(
                    id=cid,
                    user_id=user.id,
                    provider=provider,
                    preset=preset,
                    base_url=base_url,
                    model=model,
                    cheap_model=cheap,
                    key_ciphertext=ct,
                    key_nonce=nonce,
                    key_kid=kid,
                    key_last4=key[-4:] if key else None,
                    options=options,
                    is_default=is_default,
                    status=status,
                    last_tested_at=now,
                    last_error=None if ok else msg,
                    created_at=now,
                )
            )
            uvals = {}
            if use_for == "tagging":
                uvals["tagging_conn_id"] = cid
            u = work.user_row(c, user.id)
            if use_for == "all" and u.ai_mode is None:
                uvals["ai_mode"] = "api"
            if uvals:
                c.execute(db.users.update().where(db.users.c.id == user.id).values(**uvals))
            db.audit(
                "ai_key_saved",
                user_id=user.id,
                actor=user.id,
                detail={"connection": cid, "provider": provider, "preset": preset, "model": model},
                ip=security.client_ip(request),
                conn=c,
            )
            work.resume_ai_jobs(user.id, conn=c)
            out = {
                "connection": conn_json(
                    c, c.execute(select(db.ai_connections).where(db.ai_connections.c.id == cid)).first()
                ),
                "test": {"ok": ok, "message": msg},
            }
        if not options.get("vision"):
            out["warning"] = NO_VISION_WARNING
        return out

    return await run_in_threadpool(run)


def _owned_conn(user_id: str, cid: str):
    with db.read() as c:
        row = work._get(c, db.ai_connections, user_id, cid)
    if row is None:
        raise not_found()
    return row


def _key_of(row) -> str | None:
    if not row.key_ciphertext:
        return None
    try:
        return security.decrypt_secret(
            row.key_ciphertext, row.key_nonce, row.key_kid, security.connection_aad(row.user_id, row.id, row.provider)
        )
    except security.KeyUnavailable as e:
        raise bad_input(e.message) from None


@router.post("/api/ai/connections/{cid}/test")
async def test_connection(cid: str, request: Request, user=Depends(security.current_user)):
    def run():
        row = _owned_conn(user.id, cid)
        ratelimit.hit("ai_key_test", user.id, 30, "hour", message="Too many key tests. Try again in an hour.")
        key = _key_of(row)
        ok, msg, status = _test(row.provider, key, row.model, row.preset, row.base_url, row.options or {})
        with db.tx() as c:
            c.execute(
                db.ai_connections.update()
                .where(db.ai_connections.c.id == cid)
                .values(status=status, last_tested_at=db.utcnow(), last_error=None if ok else msg)
            )
            if ok:
                work.resume_ai_jobs(user.id, conn=c)
        return {"ok": ok, "message": msg}

    return await run_in_threadpool(run)


@router.patch("/api/ai/connections/{cid}")
async def patch_connection(cid: str, request: Request, user=Depends(security.current_user)):
    row0 = await run_in_threadpool(_owned_conn, user.id, cid)
    body = await _json(request)
    save_anyway = request.query_params.get("save_anyway") in ("1", "true")

    def run():
        vals: dict = {}
        model = body.get("model", row0.model)
        if not isinstance(model, str) or not re.fullmatch(r"[\w.:/@+-]{1,120}", model):
            raise bad_input("Pick a model.")
        vals["model"] = model
        if "cheap_model" in body:
            cm = body.get("cheap_model")
            if cm and (not isinstance(cm, str) or not re.fullmatch(r"[\w.:/@+-]{1,120}", cm)):
                raise bad_input("That second model name doesn't look right.")
            vals["cheap_model"] = cm or None
        opts = dict(row0.options or {})
        if isinstance(body.get("options"), dict) or "model" in body:
            new = _options({**opts, **(body.get("options") or {})}, row0.provider, model, row0.preset)
            opts.update(new)
            vals["options"] = opts
        key = _clean_key(body.get("api_key")) if "api_key" in body else None
        if key:
            ratelimit.hit("ai_key_test", user.id, 30, "hour", message="Too many key tests. Try again in an hour.")
            ok, msg, status = _test(row0.provider, key, model, row0.preset, row0.base_url, opts)
            if not ok and not save_anyway:
                raise ApiError(400, "bad_input", msg, detail={"test": {"ok": False, "message": msg}})
            ct, nonce, kid = security.encrypt_secret(key, security.connection_aad(user.id, cid, row0.provider))
            vals.update(
                key_ciphertext=ct,
                key_nonce=nonce,
                key_kid=kid,
                key_last4=key[-4:],
                status=status,
                last_tested_at=db.utcnow(),
                last_error=None if ok else msg,
            )
        with db.tx() as c:
            if body.get("is_default") is True:
                c.execute(
                    db.ai_connections.update().where(db.ai_connections.c.user_id == user.id).values(is_default=False)
                )
                vals["is_default"] = True
            c.execute(
                db.ai_connections.update()
                .where(db.ai_connections.c.id == cid, db.ai_connections.c.user_id == user.id)
                .values(**vals)
            )
            if key:
                db.audit(
                    "ai_key_saved",
                    user_id=user.id,
                    actor=user.id,
                    detail={"connection": cid},
                    ip=security.client_ip(request),
                    conn=c,
                )
            work.resume_ai_jobs(user.id, conn=c)
            return {
                "connection": conn_json(
                    c, c.execute(select(db.ai_connections).where(db.ai_connections.c.id == cid)).first()
                )
            }

    return await run_in_threadpool(run)


@router.delete("/api/ai/connections/{cid}")
def delete_connection(cid: str, user=Depends(security.current_user)):
    a = db.ai_connections
    with db.tx() as c:
        row = work._get(c, a, user.id, cid)
        if row is None:
            raise not_found()
        c.execute(a.delete().where(a.c.id == cid, a.c.user_id == user.id))
        u = work.user_row(c, user.id)
        if u.tagging_conn_id == cid:
            c.execute(db.users.update().where(db.users.c.id == user.id).values(tagging_conn_id=None))
        if row.is_default:
            nxt = c.execute(
                select(a.c.id)
                .where(a.c.user_id == user.id, a.c.id != (u.tagging_conn_id or ""))
                .order_by(a.c.created_at)
                .limit(1)
            ).first()
            if nxt:
                c.execute(a.update().where(a.c.id == nxt[0]).values(is_default=True))
        db.audit("ai_key_deleted", user_id=user.id, actor=user.id, detail={"connection": cid}, conn=c)
        work.resume_ai_jobs(user.id, conn=c)
    return {"ok": True}


@router.post("/api/ai/desktop/pair")
def desktop_pair(user=Depends(security.current_user)):
    from . import api_ext

    ratelimit.hit("desktop_pair", user.id, 20, "hour", message="Too many codes. Try again in an hour.")
    code, exp = api_ext.new_pairing_code(user.id, "desktop")
    return {"code": code, "expires_at": db.iso(exp), "server": get_settings().public_url.rstrip("/")}


@router.delete("/api/ai/desktop/{device_id}")
def desktop_delete(device_id: str, user=Depends(security.current_user)):
    d = db.devices
    with db.tx() as c:
        row = c.execute(select(d).where(d.c.id == device_id, d.c.user_id == user.id, d.c.kind == "desktop")).first()
        if row is None:
            raise not_found()
        c.execute(d.update().where(d.c.id == device_id).values(revoked_at=db.utcnow()))
        db.audit("device_revoked", user_id=user.id, actor=user.id, detail={"device": device_id}, conn=c)
    return {"ok": True}


# ============================================================================================== desktop worker


def _worker_job(dev, job_id: str):
    j = db.jobs
    with db.read() as c:
        row = c.execute(select(j).where(j.c.id == job_id, j.c.user_id == dev.user_id)).first()
    if row is None or row.executor != "desktop" or row.state != "running" or row.lease_owner != dev.id:
        raise not_found("This job is not leased to this computer any more.")
    return row


@router.post("/api/worker/hello")
async def worker_hello(request: Request, dev=Depends(security.desktop_device)):
    body = await _json(request, 16 * 1024)

    def run():
        cl = body.get("claude") if isinstance(body.get("claude"), dict) else {}
        claude = {"found": bool(cl.get("found")), "signed_in": bool(cl.get("signed_in"))}
        v = body.get("version")
        with db.tx() as c:
            st = dict(work._get(c, db.devices, dev.user_id, dev.id).state or {})
            st["claude"] = claude
            if isinstance(body.get("working"), bool):
                st["working"] = body["working"]
            vals = {"last_seen_at": db.utcnow(), "state": st}
            if isinstance(v, str) and re.fullmatch(r"[\w.+-]{1,40}", v):
                vals["version"] = v
            c.execute(db.devices.update().where(db.devices.c.id == dev.id).values(**vals))
        work.ensure_tag_slot(dev.user_id)
        return {
            "ok": True,
            "has_work": _executor_has_desktop(dev.user_id),
        }

    return await run_in_threadpool(run)


def _executor_has_desktop(user_id: str) -> bool:
    j = db.jobs
    with db.read() as c:
        return (
            c.execute(
                select(j.c.id).where(j.c.user_id == user_id, j.c.executor == "desktop", j.c.state == "waiting").limit(1)
            ).first()
            is not None
        )


@router.post("/api/worker/lease")
async def worker_lease(request: Request, dev=Depends(security.desktop_device)):
    body = await _json(request, 16 * 1024)

    def run():
        kinds = body.get("kinds") if isinstance(body.get("kinds"), list) else list(WORKER_KINDS)
        kinds = [k for k in kinds if k in WORKER_KINDS]
        if not kinds:
            raise bad_input("kinds must list AI task kinds.")
        work.ensure_tag_slot(dev.user_id)
        for _ in range(3):
            job = jobs.lease_external(
                dev.user_id,
                "desktop",
                kinds,
                dev.id,
                work.LEASE_DESKTOP_S,
                prepare=work.prepare_external(dev.user_id, "desktop"),
            )
            if job is None:
                return {"job": None, "poll_after_s": 30}
            try:
                task = work.build_task(dev.user_id, job, dest="paid_api")
            except work.Invalid as e:
                jobs.fail(job.id, "bad_task", e.message, owner=dev.id)
                continue
            jobs.set_progress(job.id, 0, 1, "Your desktop app is working on it", owner=dev.id)
            if job.kind == "make_output":
                _output_status(dev.user_id, job, "working")
            return {"job": {"id": job.id, "kind": job.kind, "task": task, "lease_until": db.iso(job.lease_until)}}
        return {"job": None, "poll_after_s": 30}

    return await run_in_threadpool(run)


def _output_status(user_id: str, job, status: str) -> None:
    oid = (job.payload or {}).get("output_id")
    if not oid:
        return
    with db.tx() as c:
        c.execute(
            db.outputs.update()
            .where(db.outputs.c.id == oid, db.outputs.c.user_id == user_id, db.outputs.c.status != "done")
            .values(status=status, updated_at=db.utcnow())
        )


@router.get("/api/worker/files/{job_id}/{name}")
def worker_file(job_id: str, name: str, dev=Depends(security.desktop_device)):
    if not re.fullmatch(r"[\w.\-]{1,120}\.jpg", name):
        raise not_found()
    job = _worker_job(dev, job_id)
    data = work.image_bytes(dev.user_id, job, name)
    if data is None:
        raise not_found()
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/api/worker/jobs/{job_id}/result")
async def worker_result(job_id: str, request: Request, dev=Depends(security.desktop_device)):
    body = await _json(request, 2 * 1024 * 1024)

    def run():
        job = _worker_job(dev, job_id)
        output = body.get("output")
        if not isinstance(output, (dict, str)):
            raise bad_input("output must be the JSON answer.")
        model = body.get("model") if isinstance(body.get("model"), str) else None
        made_by = {"via": "desktop", "provider": "claude_code", "model": (model or "")[:60] or None}
        try:
            res = work.save_result(dev.user_id, job, output, made_by, vision=True)
        except work.Invalid as e:
            raise ApiError(
                422, "invalid_output", "The answer didn't fit the format.", detail={"validation": e.message[:1500]}
            ) from None
        with db.tx() as c:
            c.execute(db.devices.update().where(db.devices.c.id == dev.id).values(last_seen_at=db.utcnow()))
        out = {"ok": True}
        if res.get("next_chunk") is not None:
            out["next_chunk"] = res["next_chunk"]
        return out

    return await run_in_threadpool(run)


@router.post("/api/worker/jobs/{job_id}/fail")
async def worker_fail(job_id: str, request: Request, dev=Depends(security.desktop_device)):
    body = await _json(request, 16 * 1024)

    def run():
        job = _worker_job(dev, job_id)
        msg = body.get("message") if isinstance(body.get("message"), str) else ""
        msg = re.sub(r"[\x00-\x1f]", " ", msg).strip()[:300] or "The desktop app could not finish this step."
        final = job.attempts >= job.max_attempts
        with db.tx() as c:
            if job.kind == "tag_reels":
                work.release_tag_batch(c, job, state="failed" if final else "queued")
                if final:
                    work.release_dedupe(job.id, conn=c)
                    work.ensure_tag_slot(dev.user_id, conn=c)
            jobs.fail(
                job.id,
                "desktop_failed",
                msg,
                run_after=None if final else db.utcnow() + timedelta(minutes=5),
                owner=dev.id,
                conn=c,
            )
        if final and job.kind == "analyze_reel":
            from .jobs_ai import _deep_failed

            _deep_failed(dev.user_id, (job.payload or {}).get("reel_id"))
        if final and job.kind == "make_output":
            _output_status(dev.user_id, job, "failed")
        return {"ok": True}

    return await run_in_threadpool(run)
