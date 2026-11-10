"""The one error type every API module raises (DESIGN-SAAS.md 4.1). WP1.

    raise ApiError(404, "not_found", "We couldn't find that.")
renders as  {"error": "We couldn't find that.", "code": "not_found"}  with HTTP 404.
"""
from __future__ import annotations

from typing import Any

DEFAULT_MESSAGES = {
    "bad_input": "Something in the request is not right. Check it and try again.",
    "not_signed_in": "Please sign in.",
    "forbidden": "You can't do that.",
    "not_found": "We couldn't find that.",
    "busy": "That is already happening. Wait a moment and try again.",
    "too_many": "Too many tries. Wait a little and try again.",
    "limit_reached": "You reached today's limit. Try again tomorrow.",
    "upstream": "Another service didn't answer. Try again in a few minutes.",
    "server": "Something went wrong on our side. Try again in a minute.",
    "too_large": "That file or request is too big.",
}

STATUS_CODES = {400: "bad_input", 401: "not_signed_in", 403: "forbidden", 404: "not_found", 405: "bad_input",
                409: "busy", 413: "too_large", 422: "bad_input", 429: "too_many", 502: "upstream", 500: "server"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None, detail: Any = None,
                 headers: dict[str, str] | None = None):
        self.status = status
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, "Something went wrong.")
        self.detail = detail
        self.headers = headers or {}
        super().__init__(f"{status} {code}: {self.message}")

    def body(self) -> dict:
        out: dict[str, Any] = {"error": self.message, "code": self.code}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


def not_found(message: str = "We couldn't find that.") -> ApiError:
    return ApiError(404, "not_found", message)


def bad_input(message: str, detail: Any = None) -> ApiError:
    return ApiError(400, "bad_input", message, detail)


def forbidden(message: str = "You can't do that.", code: str = "forbidden") -> ApiError:
    return ApiError(403, code, message)


def too_many(message: str = "Too many tries. Wait a little and try again.", retry_after: int = 60,
             code: str = "too_many") -> ApiError:
    return ApiError(429, code, message, headers={"Retry-After": str(max(1, int(retry_after)))})
