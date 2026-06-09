"""Unit tests for the liveness/readiness probes (main.py).

The probe handlers are called directly with ``app.state.db`` swapped for a fake
Motor database whose ``command("ping")`` either succeeds or raises — no real
MongoDB, no live server.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main


async def test_health_live_always_ok():
    resp = await main.health_live()
    assert resp == {"status": "ok"}


async def test_health_ready_ok_when_ping_succeeds(monkeypatch):
    fake_db = SimpleNamespace(command=AsyncMock(return_value={"ok": 1.0}))
    monkeypatch.setattr(main.app.state, "db", fake_db, raising=False)

    resp = await main.health_ready()

    assert resp.status_code == 200
    assert resp.body == b'{"status":"ready","mongodb":"ok"}'
    fake_db.command.assert_awaited_once_with("ping")


async def test_health_ready_503_when_ping_raises(monkeypatch):
    fake_db = SimpleNamespace(command=AsyncMock(side_effect=RuntimeError("connection refused")))
    monkeypatch.setattr(main.app.state, "db", fake_db, raising=False)

    resp = await main.health_ready()

    assert resp.status_code == 503
    assert resp.body == b'{"status":"not_ready","mongodb":"unreachable"}'


async def test_health_ready_503_when_db_missing(monkeypatch):
    monkeypatch.setattr(main.app.state, "db", None, raising=False)
    resp = await main.health_ready()
    assert resp.status_code == 503  # db not initialized -> not ready, never raises
