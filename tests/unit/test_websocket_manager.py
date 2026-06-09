"""Unit tests for the WebSocket ConnectionManager.

Uses a fake WebSocket (AsyncMock-backed) — no real network — to verify connect/
disconnect bookkeeping, broadcast fan-out, stale-connection pruning, and that a
failed send never propagates.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from websocket_manager import ConnectionManager

MISSION = "m-1"


class FakeWebSocket:
    """Minimal stand-in for a Starlette WebSocket."""

    def __init__(self, *, fail_send: bool = False):
        self.accept = AsyncMock()
        self.sent: list[dict] = []
        self._fail_send = fail_send

    async def send_json(self, message: dict) -> None:
        if self._fail_send:
            raise RuntimeError("socket closed")
        self.sent.append(message)


async def test_connect_accepts_and_registers():
    mgr = ConnectionManager()
    ws = FakeWebSocket()
    await mgr.connect(MISSION, ws)
    ws.accept.assert_awaited_once()
    assert mgr.connection_count(MISSION) == 1
    assert mgr.total_connections() == 1


async def test_disconnect_removes_and_cleans_up_mission():
    mgr = ConnectionManager()
    ws = FakeWebSocket()
    await mgr.connect(MISSION, ws)
    mgr.disconnect(MISSION, ws)
    assert mgr.connection_count(MISSION) == 0
    # mission entry pruned entirely once empty
    assert mgr.total_connections() == 0


async def test_disconnect_unknown_is_noop():
    mgr = ConnectionManager()
    mgr.disconnect("nope", FakeWebSocket())  # must not raise
    assert mgr.total_connections() == 0


async def test_broadcast_fans_out_to_all_connections():
    mgr = ConnectionManager()
    a, b = FakeWebSocket(), FakeWebSocket()
    await mgr.connect(MISSION, a)
    await mgr.connect(MISSION, b)
    await mgr.broadcast(MISSION, {"event_type": "TASK_STARTED"})
    assert a.sent == [{"event_type": "TASK_STARTED"}]
    assert b.sent == [{"event_type": "TASK_STARTED"}]


async def test_broadcast_isolated_per_mission():
    mgr = ConnectionManager()
    a, b = FakeWebSocket(), FakeWebSocket()
    await mgr.connect("m-a", a)
    await mgr.connect("m-b", b)
    await mgr.broadcast("m-a", {"x": 1})
    assert a.sent == [{"x": 1}]
    assert b.sent == []  # other mission untouched


async def test_broadcast_to_no_connections_is_noop():
    mgr = ConnectionManager()
    await mgr.broadcast(MISSION, {"x": 1})  # must not raise
    assert mgr.connection_count(MISSION) == 0


async def test_broadcast_prunes_stale_connections():
    mgr = ConnectionManager()
    good, bad = FakeWebSocket(), FakeWebSocket(fail_send=True)
    await mgr.connect(MISSION, good)
    await mgr.connect(MISSION, bad)

    await mgr.broadcast(MISSION, {"event_type": "RISK_SCORE_UPDATED"})  # must not raise

    assert good.sent == [{"event_type": "RISK_SCORE_UPDATED"}]  # healthy socket got it
    assert mgr.connection_count(MISSION) == 1                    # dead socket pruned
    # a subsequent broadcast only reaches the surviving connection
    await mgr.broadcast(MISSION, {"event_type": "TASK_COMPLETED"})
    assert good.sent[-1] == {"event_type": "TASK_COMPLETED"}


async def test_emit_event_broadcasts_through_singleton(mock_db, monkeypatch):
    """emit_event pushes the persisted event to the shared singleton manager."""
    import websocket_manager
    from state import database as db

    captured: list[tuple[str, dict]] = []

    async def _capture(mission_id, message):
        captured.append((mission_id, message))

    monkeypatch.setattr(websocket_manager.connection_manager, "broadcast", _capture)

    mission = await db.create_mission("g", "GENERAL")
    await db.emit_event(mission.mission_id, "TASK_STARTED", {"task_id": "t1"}, "orchestrator")

    assert len(captured) >= 1
    mid, msg = captured[-1]
    assert mid == mission.mission_id
    assert msg["event_type"] == "TASK_STARTED"
    assert msg["source"] == "orchestrator"
    assert isinstance(msg["created_at"], str)  # JSON-serialized datetime


async def test_emit_event_survives_broadcast_failure(mock_db, monkeypatch):
    """A broadcast error must never break event persistence."""
    import websocket_manager
    from state import database as db

    async def _boom(*_a, **_k):
        raise RuntimeError("broadcast down")

    monkeypatch.setattr(websocket_manager.connection_manager, "broadcast", _boom)

    mission = await db.create_mission("g", "GENERAL")
    await db.emit_event(mission.mission_id, "TASK_STARTED", {}, "orchestrator")  # must not raise

    events = await db.get_recent_events_for_mission(mission.mission_id)
    assert any(e.event_type == "TASK_STARTED" for e in events)  # still persisted
