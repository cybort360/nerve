"""Middleware gating, exercised against a minimal ASGI app (no Mongo/lifespan)."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from auth.middleware import AuthMiddleware
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
