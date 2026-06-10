"""HTTP middleware that gates every request outside a public allowlist.

A request is allowed through when its path is public, or when it carries a valid
session cookie. Otherwise page (text/html) requests are redirected to /login and
everything else (APIs, WebSocket upgrades) gets 401.
"""
from __future__ import annotations

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from auth.tokens import COOKIE_NAME, decode_token

log = structlog.get_logger()

# Public path prefixes — reachable without a session.
PUBLIC_PREFIXES = (
    "/auth", "/login", "/health", "/healthz", "/webhooks",
    "/showcase", "/live-classic", "/favicon.ico", "/docs", "/openapi.json", "/redoc",
)


def _is_public(path: str) -> bool:
    """True if the path equals a public prefix or sits under one (boundary-safe)."""
    return any(path == p or path.startswith(p + "/") for p in PUBLIC_PREFIXES)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


class AuthMiddleware(BaseHTTPMiddleware):
    """Require a valid session cookie for all non-public paths."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_public(path):
            return await call_next(request)
        if decode_token(request.cookies.get(COOKIE_NAME)) is not None:
            return await call_next(request)
        if _wants_html(request):
            return RedirectResponse(url="/login", status_code=302)
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})
