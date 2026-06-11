"""API route tests.

Route handlers are called directly with a fake Request (whose ``app.state`` holds
a mocked orchestrator / failure engine) and the in-memory state layer, so the
real HTTP contract is exercised without a live server, Mongo, or Gemini.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from config import settings
from failure_engine.injector import FailureEngine, FailureScenario, FailureType
from routes import actions, demo, failure, missions
from routes.schemas import ApproveRequest, CreateMissionRequest, RejectRequest
from state import database as db


def _request(orchestrator=None, failure_engine=None):
    state = SimpleNamespace(orchestrator=orchestrator, failure_engine=failure_engine)
    return SimpleNamespace(app=SimpleNamespace(state=state))


# --------------------------------------------------------------------------- #
# POST /missions
# --------------------------------------------------------------------------- #
async def test_create_mission_starts_loop(mock_db):
    user = await db.create_user("t@test.io", "h")
    orch = SimpleNamespace(run_mission=AsyncMock())
    body = CreateMissionRequest(goal="resolve checkout spike", mission_type="INCIDENT_RESPONSE")
    resp = await missions.create_mission(body, _request(orchestrator=orch), user)

    assert resp.status == "pending"
    stored = await db.get_mission(resp.mission_id)
    assert stored is not None and stored.goal == "resolve checkout spike"
    orch.run_mission.assert_awaited_once_with(resp.mission_id)
    events = [e.event_type for e in await db.get_recent_events_for_mission(resp.mission_id)]
    assert "MISSION_CREATED" in events


# --------------------------------------------------------------------------- #
# GET /missions/{id}
# --------------------------------------------------------------------------- #
async def test_read_mission_state_404(mock_db):
    user = await db.create_user("t@test.io", "h")
    with pytest.raises(HTTPException) as exc:
        await missions.read_mission_state("nope", _request(), user)
    assert exc.value.status_code == 404


async def test_read_mission_state_aggregates(mock_db):
    user = await db.create_user("t@test.io", "h")
    mission = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    await db.create_task(mission.mission_id, "execution", "get problems")
    await db.emit_event(mission.mission_id, "RISK_SCORE_UPDATED", {"overall": 0.42, "breakdown": {"failed_tasks": 0.0}}, "agent")
    state = await db.get_mission_state(mission.mission_id)
    await db.create_snapshot(state, cycle=1)
    await db.create_action(mission.mission_id, "gitlab_rollback", {"ref": "main"})

    resp = await missions.read_mission_state(mission.mission_id, _request(), user)
    assert resp.mission.mission_id == mission.mission_id
    assert len(resp.tasks) == 1
    assert resp.latest_snapshot is not None and resp.latest_snapshot.cycle == 1
    assert resp.risk == 0.42
    assert len(resp.pending_actions) == 1
    assert resp.active_failures == []  # engine disabled


async def test_read_events_pagination(mock_db):
    user = await db.create_user("t@test.io", "h")
    mission = await db.create_mission("g", "GENERAL", owner_id=user.user_id)
    for i in range(5):
        await db.emit_event(mission.mission_id, "TICK", {"i": i}, "orchestrator")
    page = await missions.read_events(mission.mission_id, limit=2, offset=0, user=user)
    assert len(page.events) == 2
    assert page.total == 5 and page.limit == 2 and page.offset == 0


# --------------------------------------------------------------------------- #
# POST /actions/{id}/approve | reject
# --------------------------------------------------------------------------- #
async def test_approve_action(mock_db, monkeypatch):
    user = await db.create_user("t@test.io", "h")
    monkeypatch.setattr(actions, "_maybe_trigger_execution", AsyncMock())
    mission = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    action = await db.create_action(mission.mission_id, "gitlab_rollback", {"ref": "main"})

    updated = await actions.approve_action(action.action_id, ApproveRequest(approved_by="oncall"), _request(), user)
    assert updated.status == "approved" and updated.approved_by == "oncall"
    actions._maybe_trigger_execution.assert_awaited_once()
    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    assert "ACTION_APPROVED" in events


async def test_approve_action_404(mock_db):
    user = await db.create_user("t@test.io", "h")
    with pytest.raises(HTTPException) as exc:
        await actions.approve_action("missing", ApproveRequest(approved_by="x"), _request(), user)
    assert exc.value.status_code == 404


async def test_approve_action_409_when_not_pending(mock_db, monkeypatch):
    user = await db.create_user("t@test.io", "h")
    monkeypatch.setattr(actions, "_maybe_trigger_execution", AsyncMock())
    mission = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    action = await db.create_action(mission.mission_id, "gitlab_issue", {})
    await db.update_action_status(action.action_id, "approved")

    with pytest.raises(HTTPException) as exc:
        await actions.approve_action(action.action_id, ApproveRequest(approved_by="x"), _request(), user)
    assert exc.value.status_code == 409


async def test_reject_action(mock_db):
    user = await db.create_user("t@test.io", "h")
    mission = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    action = await db.create_action(mission.mission_id, "gitlab_rollback", {})
    updated = await actions.reject_action(action.action_id, RejectRequest(approved_by="oncall", reason="not the cause"), user)
    assert updated.status == "rejected"
    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    assert "ACTION_REJECTED" in events


# --------------------------------------------------------------------------- #
# Failure engine routes
# --------------------------------------------------------------------------- #
async def test_inject_failure_when_enabled(mock_db, monkeypatch):
    monkeypatch.setattr(settings, "failure_engine_enabled", True)
    engine = FailureEngine()
    scenario = FailureScenario(failure_type=FailureType.CONTRADICTORY_METRICS, target="get_metrics", severity=0.8)
    resp = await failure.inject_failure(scenario, _request(failure_engine=engine), mission_id=None)
    assert resp["status"] == "injected"
    assert len(resp["active"]) == 1


async def test_inject_failure_403_when_disabled(mock_db):
    engine = FailureEngine()
    scenario = FailureScenario(failure_type=FailureType.SERVICE_OUTAGE, target="get_problems")
    with pytest.raises(HTTPException) as exc:
        await failure.inject_failure(scenario, _request(failure_engine=engine), mission_id=None)
    assert exc.value.status_code == 403


async def test_clear_failure_when_enabled(mock_db, monkeypatch):
    monkeypatch.setattr(settings, "failure_engine_enabled", True)
    engine = FailureEngine()
    await engine.inject(FailureScenario(failure_type=FailureType.NOISY_DATA, target="get_metrics"))
    resp = await failure.clear_failure(failure.ClearFailureRequest(failure_type=FailureType.NOISY_DATA), _request(failure_engine=engine))
    assert resp["active"] == []


# --------------------------------------------------------------------------- #
# Demo route
# --------------------------------------------------------------------------- #
async def test_demo_start_403_when_disabled(mock_db):
    with pytest.raises(HTTPException) as exc:
        await demo.start_demo(_request())
    assert exc.value.status_code == 403


async def test_demo_start_schedules_scenario(mock_db, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    fake = SimpleNamespace(prepare=AsyncMock(return_value="demo-mission-1"), run=AsyncMock())
    monkeypatch.setattr("failure_engine.demo_scenario.DemoScenario", lambda *a, **k: fake)
    resp = await demo.start_demo(_request(orchestrator=SimpleNamespace()))
    await asyncio.sleep(0)  # let the scheduled task run
    assert resp["status"] == "demo_started"
    assert resp["mission_id"] == "demo-mission-1"
    fake.prepare.assert_awaited_once()
    fake.run.assert_awaited_once()
