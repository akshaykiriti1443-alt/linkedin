"""FastAPI factory for ReelVault Members (DESIGN-SAAS.md 2.2, 4.11, 13). WP1 owns this file.

    uvicorn reelvault.server.app:create_app --factory          (RV_ROLE=web; the default)
    create_app("all")   web + an in-process worker (tests, dev quick start)

Order is a contract (tests/server/test_app_routes.py guards it):
  1. every router in ROUTERS (exact list of section 13)
  2. the SPA at explicit paths only: GET / -> web/index.html, /static/* (no catch-all; routing is hash-based)
  3. MCP LAST via mcp_server.mount(app), which must use mount_last() so unknown paths stay JSON 404s.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Callable, Iterable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import (api_accounts, api_admin, api_ai, api_auth, api_ext, api_files, api_outputs, api_profile, api_reels,
               db, mcp_server, oauth, storage)
from .errors import STATUS_CODES, ApiError
from .settings import VERSION, get_settings

log = logging.getLogger("reelvault.app")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

ROUTERS = [api_auth.router, api_admin.router, api_files.router, api_ext.router, api_accounts.router,
           api_reels.router, api_ai.router, api_outputs.router, api_profile.router, oauth.router]

DEVICE_PREFIXES = ("/api/ext/v2/", "/api/worker/")
MAX_JSON_BODY = 8 * 1024 * 1024
MAX_ANY_BODY = 60 * 1024 * 1024

CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self' https://claude.ai https://claude.com http://localhost:* http://127.0.0.1:*")


# ============================================================================================== logging

class JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"t": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname.lower(),
               "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    root = logging.getLogger()
    if not any(getattr(h, "_rv", False) for h in root.handlers):
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonLines())
        h._rv = True  # type: ignore[attr-defined]
        root.addHandler(h)
    root.setLevel(level)


# ============================================================================================== runtime prep

def prepare_runtime(*, migrate: bool = True) -> None:
    """Checks + folders + DB schema + first admin / community code. Safe to call from web and worker."""
    s = get_settings()
    problems = s.problems()
    if problems:
        raise SystemExit("\n".join(problems))
    try:
        storage.ensure_dirs()
    except PermissionError as e:
        raise SystemExit(f"ReelVault cannot write to {s.data_dir}. Give the volume to user 10001 (the reelvault "
                         f"user), or run the container as root once so it can do that itself. "
                         f"See deploy/hosts/paas-with-volume.md. ({e.strerror}: {e.filename})") from None
    if s.is_sqlite:
        db.check_local_fs(storage.db_dir())
    db.init()
    if migrate:
        db.migrate()
    api_admin.bootstrap()


# ============================================================================================== ASGI helpers

def iter_routes(app_or_router, prefix: str = ""):
    """Yield (full_path, methods:set, route) for every route, flattening included routers (FastAPI >= 0.160 wraps
    them in _IncludedRouter). Mounts are yielded with methods=set()."""
    for r in getattr(app_or_router, "routes", []):
        orig = getattr(r, "original_router", None)
        if orig is not None:
            ctx = getattr(r, "include_context", None)
            yield from iter_routes(orig, prefix + (getattr(ctx, "prefix", "") or ""))
            continue
        yield prefix + getattr(r, "path", ""), set(getattr(r, "methods", None) or ()), r


def mount_last(app: FastAPI, asgi_app, paths: Iterable[str]) -> None:
    """Mount `asgi_app` at '/' (LAST) but forward only requests under `paths`; everything else is a JSON 404.
    WP6 mounts the MCP Streamable HTTP app with this (MCP_PATHS = ("/mcp", "/.well-known/oauth-protected-resource"))."""
    prefixes = tuple(paths)

    async def dispatch(scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return
        path = scope.get("path", "")
        if any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes):
            await asgi_app(scope, receive, send)
            return
        body = json.dumps({"error": "We couldn't find that.", "code": "not_found"}).encode()
        await send({"type": "http.response.start", "status": 404,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    app.mount("/", dispatch, name="mcp")


class EdgeMiddleware:
    """Pure-ASGI: body-size guard, CORS for device paths only (11.4), security headers (11.2)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        origin = headers.get("origin", "")
        device_path = path.startswith(DEVICE_PREFIXES)
        cors_ok = device_path and origin.startswith("chrome-extension://")

        if device_path and scope["method"] == "OPTIONS":
            hdrs = [(b"access-control-allow-methods", b"GET, POST"),
                    (b"access-control-allow-headers", b"Authorization, Content-Type"),
                    (b"access-control-max-age", b"600"), (b"vary", b"Origin")]
            if cors_ok:
                hdrs.append((b"access-control-allow-origin", origin.encode()))
            await send({"type": "http.response.start", "status": 204 if cors_ok else 403, "headers": hdrs})
            await send({"type": "http.response.body", "body": b""})
            return

        cl = headers.get("content-length", "")
        ctype = headers.get("content-type", "")
        limit = MAX_ANY_BODY if ctype.startswith("multipart/") else MAX_JSON_BODY
        if cl.isdigit() and int(cl) > limit:  # fast path only: the real cap is counted in receive_capped below
            body = json.dumps({"error": "That file or request is too big.", "code": "too_large"}).encode()
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return

        seen = [0]

        async def receive_capped():
            # Count the bytes as they stream in: a chunked body (no Content-Length) must not get past the cap.
            # An HTTPException (413) raised here passes through FastAPI's body parsing (it re-raises
            # HTTPException) and our _http_error handler renders the usual JSON {"code": "too_large"}.
            message = await receive()
            if message.get("type") == "http.request":
                seen[0] += len(message.get("body", b"") or b"")
                if seen[0] > limit:
                    raise StarletteHTTPException(413)
            return message

        https = get_settings().is_https

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                hs = list(message.get("headers", []))
                names = {k.lower() for k, _ in hs}
                hs.append((b"x-content-type-options", b"nosniff"))
                ct = next((v for k, v in hs if k.lower() == b"content-type"), b"")
                if ct.startswith(b"text/html"):
                    if b"content-security-policy" not in names:
                        hs.append((b"content-security-policy", CSP.encode()))
                    hs.append((b"referrer-policy", b"same-origin"))
                    hs.append((b"x-frame-options", b"DENY"))
                if https:
                    hs.append((b"strict-transport-security", b"max-age=31536000"))
                if cors_ok:
                    hs.append((b"access-control-allow-origin", origin.encode()))
                    hs.append((b"vary", b"Origin"))
                message = {**message, "headers": hs}
            await send(message)

        await self.app(scope, receive_capped, send_wrapped)


# ============================================================================================== error handlers

def _install_errors(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_req: Request, e: ApiError):
        return JSONResponse(e.body(), status_code=e.status, headers=e.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_req: Request, e: StarletteHTTPException):
        code = STATUS_CODES.get(e.status_code, "server" if e.status_code >= 500 else "bad_input")
        msg = {404: "We couldn't find that.", 405: "That action is not allowed here.",
               413: "That file or request is too big."}.get(e.status_code)
        body = ApiError(e.status_code, code, msg).body()
        return JSONResponse(body, status_code=e.status_code, headers=getattr(e, "headers", None) or {})

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, e: RequestValidationError):
        fields = []
        for err in e.errors()[:10]:
            loc = ".".join(str(x) for x in err.get("loc", []) if x not in ("body", "query", "path"))
            fields.append({"field": loc, "problem": str(err.get("msg", ""))[:200]})
        return JSONResponse({"error": "Something in the request is not right. Check it and try again.",
                             "code": "bad_input", "detail": {"fields": fields}}, status_code=400)

    @app.exception_handler(Exception)
    async def _crash(_req: Request, e: Exception):
        log.exception("unhandled error: %s", type(e).__name__)
        return JSONResponse({"error": "Something went wrong on our side. Try again in a minute.", "code": "server"},
                            status_code=500)


# ============================================================================================== factory

def _start_inprocess_worker(app: FastAPI) -> Callable[[], None]:
    from . import jobs

    s = get_settings()
    stop = threading.Event()
    pools = {"media": max(1, min(2, s.whisper_concurrency)), "ai": max(1, min(3, s.ai_concurrency)),
             "light": max(1, s.light_concurrency)}
    th = threading.Thread(target=jobs.run_worker, args=(stop,), kwargs={"pools": pools, "migrate": False},
                          name="rv-worker", daemon=True)
    th.start()
    app.state.worker_stop = stop

    def stopper() -> None:
        stop.set()
        th.join(timeout=15)

    return stopper


def create_app(role: str | None = None, *, prepare: bool = True) -> FastAPI:
    """role: 'web' (default, HTTP only) or 'all' (HTTP + in-process worker). Other roles are served as 'web'."""
    s = get_settings()
    role = role or s.role
    if role not in ("web", "all"):
        role = "web"
    setup_logging()
    if prepare:
        prepare_runtime()

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        stopper = None
        async with AsyncExitStack() as stack:
            for hook in list(app_.state.lifespan_hooks):
                await stack.enter_async_context(hook())
            if role == "all":
                stopper = _start_inprocess_worker(app_)
            try:
                yield
            finally:
                if stopper:
                    stopper()

    app = FastAPI(title="ReelVault Members", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.role = role
    app.state.lifespan_hooks = []  # list[Callable[[], AsyncContextManager]] (WP6: mcp.session_manager.run)
    _install_errors(app)
    app.add_middleware(EdgeMiddleware)

    for r in ROUTERS:
        app.include_router(r)

    index = WEB_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    def spa_index():
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    static = WEB_DIR / "static"
    static.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static), name="static")

    mcp_server.mount(app)  # LAST
    return app
