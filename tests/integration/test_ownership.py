"""SP2: mission/action access is scoped to the owner."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import actions, missions
from routes.schemas import ApproveRequest
from state import database as db


def _request(orchestrator=None, failure_engine=None):
    state = SimpleNamespace(orchestrator=orchestrator, failure_engine=failure_engine)
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_list_returns_only_my_missions(mock_db):
    alice = await db.create_user("alice@x.io", "h")
    bob = await db.create_user("bob@x.io", "h")
    await db.create_mission("alice-1", "GENERAL", owner_id=alice.user_id)
    await db.create_mission("bob-1", "GENERAL", owner_id=bob.user_id)
    out = await missions.list_missions(alice)
    assert {m.goal for m in out.missions} == {"alice-1"}


async def test_read_other_users_mission_is_404(mock_db):
    alice = await db.create_user("alice@x.io", "h")
    bob = await db.create_user("bob@x.io", "h")
    m = await db.create_mission("alice-secret", "GENERAL", owner_id=alice.user_id)
    with pytest.raises(HTTPException) as exc:
        await missions.read_mission_state(m.mission_id, _request(), bob)
    assert exc.value.status_code == 404


async def test_read_events_other_user_is_404(mock_db):
    alice = await db.create_user("alice@x.io", "h")
    bob = await db.create_user("bob@x.io", "h")
    m = await db.create_mission("a", "GENERAL", owner_id=alice.user_id)
    with pytest.raises(HTTPException) as exc:
        await missions.read_events(m.mission_id, 50, 0, bob)
    assert exc.value.status_code == 404


async def test_approve_other_users_action_is_404(mock_db):
    alice = await db.create_user("alice@x.io", "h")
    bob = await db.create_user("bob@x.io", "h")
    m = await db.create_mission("a", "INCIDENT_RESPONSE", owner_id=alice.user_id)
    action = await db.create_action(m.mission_id, "gitlab_rollback", {})
    with pytest.raises(HTTPException) as exc:
        await actions.approve_action(action.action_id, ApproveRequest(approved_by="bob"), _request(), bob)
    assert exc.value.status_code == 404
