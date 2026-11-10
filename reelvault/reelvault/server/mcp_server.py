"""MCPServer, TokenVerifier, tools (7.3). mount(app) mounts MCP LAST.

STUB written by WP1 so imports never fail. Owner: WP6. Replace freely; keep the public names.
"""
from __future__ import annotations

import json

from fastapi import FastAPI


def mount(app: FastAPI) -> None:
    """Stub: answers /mcp with 401 + WWW-Authenticate and serves the PRM document, like the real SDK app.

    WP6 replaces the body with the real MCPServer app, keeping these rules:
      * register the SDK lifespan:  app.state.lifespan_hooks.append(lambda: mcp.session_manager.run())
      * mount through app.mount_last(app, mcp_app, MCP_PATHS) so unknown paths stay JSON 404s.
    """
    from .app import mount_last
    from .settings import get_settings

    base = get_settings().public_url

    async def stub_app(scope, receive, send):
        path = scope.get("path", "")
        if path.startswith("/.well-known/oauth-protected-resource"):
            body = json.dumps({"resource": base + "/mcp", "authorization_servers": [base],
                               "scopes_supported": ["reelvault"], "bearer_methods_supported": ["header"]}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
        else:
            body = json.dumps({"error": "invalid_token", "error_description": "Authentication required"}).encode()
            hdr = f'Bearer error="invalid_token", resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", hdr.encode())]})
        await send({"type": "http.response.body", "body": body})

    mount_last(app, stub_app, MCP_PATHS)


MCP_PATHS = ("/mcp", "/.well-known/oauth-protected-resource")
