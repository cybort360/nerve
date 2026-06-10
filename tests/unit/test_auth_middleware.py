"""Middleware gating, exercised against a minimal ASGI app (no Mongo/lifespan)."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from auth.middleware import AuthMiddleware, _is_public
from auth.tokens import COOKIE_NAME, create_access_token


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/showcase")
    async def showcase():
        return {"ok": True}

    @app.get("/missions/x")
    async def gated_api():
        return {"ok": True}

    @app.get("/")
    async def gated_page():
        return {"ok": True}

    return app


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_public_paths_allowed_without_cookie():
    async with await _client(_app()) as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/showcase")).status_code == 200


async def test_gated_api_returns_401_without_cookie():
    async with await _client(_app()) as c:
        r = await c.get("/missions/x")
        assert r.status_code == 401


async def test_gated_page_redirects_to_login_without_cookie():
    async with await _client(_app()) as c:
        r = await c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


async def test_valid_cookie_passes_gate():
    async with await _client(_app()) as c:
        c.cookies.set(COOKIE_NAME, create_access_token("u1"))
        assert (await c.get("/missions/x")).status_code == 200


def test_is_public_is_boundary_safe():
    assert _is_public("/showcase") is True
    assert _is_public("/showcase/x") is True
    assert _is_public("/health") is True
    assert _is_public("/health/ready") is True
    # look-alikes must NOT be public
    assert _is_public("/showcase-evil") is False
    assert _is_public("/healthcheck") is False
    assert _is_public("/missions") is False
    assert _is_public("/") is False


async def test_expired_token_is_gated():
    async with await _client(_app()) as c:
        c.cookies.set(COOKIE_NAME, create_access_token("u1", expires_minutes=-1))
        assert (await c.get("/missions/x")).status_code == 401


# --- WebSocket auth (BaseHTTPMiddleware does NOT gate WS, so endpoints must) ---
from auth.dependencies import WS_UNAUTHORIZED, reject_unauthenticated_ws  # noqa: E402


class _FakeWS:
    def __init__(self, cookie=None):
        self.cookies = {COOKIE_NAME: cookie} if cookie else {}
        self.closed_code = None

    async def close(self, code=1000):
        self.closed_code = code


async def test_ws_rejected_without_cookie():
    ws = _FakeWS()
    assert await reject_unauthenticated_ws(ws) is True
    assert ws.closed_code == WS_UNAUTHORIZED


async def test_ws_rejected_with_invalid_cookie():
    ws = _FakeWS("tampered-token")
    assert await reject_unauthenticated_ws(ws) is True
    assert ws.closed_code == WS_UNAUTHORIZED


async def test_ws_allowed_with_valid_cookie():
    ws = _FakeWS(create_access_token("u1"))
    assert await reject_unauthenticated_ws(ws) is False
    assert ws.closed_code is None
