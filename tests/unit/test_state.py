"""Unit tests for the state layer (state/database.py).

Covers every CRUD function, the ``emit_event`` helper, snapshotting, and the
aggregate ``get_mission_state`` view, backed by an in-memory mongomock database.
All failures must surface as typed StateError subclasses.
"""

from __future__ import annotations

import pytest

from exceptions import DocumentNotFoundError
from state import database as db
from state.models import Action, Event, Mission, MissionState, Task


# --------------------------------------------------------------------------- #
# Missions
# --------------------------------------------------------------------------- #
async def test_create_and_get_mission(mock_db):
    created = await db.create_mission("checkout 500s", "INCIDENT_RESPONSE", {"svc": "checkout"})
    assert isinstance(created, Mission)
    assert created.status == "pending"
    assert created.context == {"svc": "checkout"}

    fetched = await db.get_mission(created.mission_id)
    assert fetched is not None
    assert fetched.mission_id == created.mission_id
    assert fetched.goal == "checkout 500s"


async def test_get_mission_returns_none_when_absent(mock_db):
    assert await db.get_mission("does-not-exist") is None


async def test_update_mission_status(mock_db):
    mission = await db.create_mission("g", "GENERAL")
    updated = await db.update_mission_status(mission.mission_id, "executing")
    assert updated.status == "executing"
    # Compare round-tripped fields (consistent ms precision) rather than the
    # in-memory pre-write value, which carries finer microsecond precision.
    assert updated.updated_at >= updated.created_at


async def test_update_mission_status_missing_raises(mock_db):
    with pytest.raises(DocumentNotFoundError) as exc:
        await db.update_mission_status("missing", "executing")
    assert exc.value.context["mission_id"] == "missing"


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
async def test_create_task_links_to_mission(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    task = await db.create_task(mission.mission_id, "execution", "roll back deploy")
    assert isinstance(task, Task)
    assert task.status == "pending"

    parent = await db.get_mission(mission.mission_id)
    assert task.task_id in parent.task_ids


async def test_get_task_and_absent(mock_db):
    mission = await db.create_mission("g", "GENERAL")
    task = await db.create_task(mission.mission_id, "planner", "decompose")
    assert (await db.get_task(task.task_id)).task_id == task.task_id
    assert await db.get_task("nope") is None


async def test_update_task_status_with_result_and_error(mock_db):
    mission = await db.create_mission("g", "GENERAL")
    task = await db.create_task(mission.mission_id, "execution", "call mcp")
    updated = await db.update_task_status(
        task.task_id, "failed", result={"code": 503}, error="MCP down", retry_count=2
    )
    assert updated.status == "failed"
    assert updated.result == {"code": 503}
    assert updated.error == "MCP down"
    assert updated.retry_count == 2


async def test_update_task_status_missing_raises(mock_db):
    with pytest.raises(DocumentNotFoundError):
        await db.update_task_status("missing", "completed")


async def test_get_tasks_for_mission(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    await db.create_task(mission.mission_id, "planner", "a")
    await db.create_task(mission.mission_id, "execution", "b")
    other = await db.create_mission("h", "GENERAL")
    await db.create_task(other.mission_id, "risk", "c")

    tasks = await db.get_tasks_for_mission(mission.mission_id)
    assert len(tasks) == 2
    assert {t.description for t in tasks} == {"a", "b"}


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
async def test_create_and_get_action(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    action = await db.create_action(
        mission.mission_id, "gitlab_rollback", {"pipeline": 42}
    )
    assert isinstance(action, Action)
    assert action.status == "pending"
    assert (await db.get_action(action.action_id)).payload == {"pipeline": 42}
    assert await db.get_action("nope") is None


async def test_update_action_status_approval_flow(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    action = await db.create_action(mission.mission_id, "gitlab_issue", {})
    approved = await db.update_action_status(
        action.action_id, "approved", approved_by="oncall@nerve"
    )
    assert approved.status == "approved"
    assert approved.approved_by == "oncall@nerve"


async def test_update_action_status_missing_raises(mock_db):
    with pytest.raises(DocumentNotFoundError):
        await db.update_action_status("missing", "approved")


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
async def test_emit_event_and_fetch(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    await db.emit_event(
        mission.mission_id, "TASK_STARTED", {"n": 1}, "orchestrator", task_id="t1"
    )
    events = await db.get_recent_events_for_mission(mission.mission_id)
    assert len(events) == 1
    assert isinstance(events[0], Event)
    assert events[0].event_type == "TASK_STARTED"
    assert events[0].task_id == "t1"
    assert events[0].source == "orchestrator"


async def test_get_recent_events_respects_limit_and_isolation(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    other = await db.create_mission("h", "GENERAL")
    for i in range(5):
        await db.emit_event(mission.mission_id, "TICK", {"i": i}, "orchestrator")
    await db.emit_event(other.mission_id, "TICK", {}, "orchestrator")

    limited = await db.get_recent_events_for_mission(mission.mission_id, limit=3)
    assert len(limited) == 3
    assert all(e.mission_id == mission.mission_id for e in limited)


# --------------------------------------------------------------------------- #
# Snapshots + aggregate state
# --------------------------------------------------------------------------- #
async def test_get_mission_state_aggregates(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    await db.create_task(mission.mission_id, "planner", "a")
    await db.emit_event(mission.mission_id, "MISSION_CREATED", {}, "orchestrator")

    state = await db.get_mission_state(mission.mission_id)
    assert isinstance(state, MissionState)
    assert state.mission.mission_id == mission.mission_id
    assert len(state.tasks) == 1
    assert len(state.recent_events) == 1


async def test_get_mission_state_none_when_absent(mock_db):
    assert await db.get_mission_state("missing") is None


# --------------------------------------------------------------------------- #
# UserSettings (per-user integration config with encrypted secrets)
# --------------------------------------------------------------------------- #
async def test_user_settings_roundtrip_and_secret_encrypted(mock_db):
    await db.upsert_user_settings("u1", {"gitlab_token": "glpat-abc", "gitlab_url": "https://gl.example", "tavily_api_key": "tvly-x"})
    got = await db.get_user_settings("u1")
    assert got.gitlab_token == "glpat-abc"          # decrypted on read
    assert got.gitlab_url == "https://gl.example"
    # raw stored doc must NOT contain the plaintext secret
    raw = await db.get_user_settings_collection().find_one({"user_id": "u1"})
    assert raw["gitlab_token"] != "glpat-abc"
    assert raw["gitlab_url"] == "https://gl.example"  # non-secret stored plaintext


async def test_get_user_settings_missing(mock_db):
    assert await db.get_user_settings("nobody") is None


async def test_create_snapshot_summarizes_state(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    await db.create_task(mission.mission_id, "planner", "a")
    await db.update_mission_status(mission.mission_id, "executing")
    state = await db.get_mission_state(mission.mission_id)

    snapshot = await db.create_snapshot(state, cycle=1, failure_injections_active=[{"t": "x"}])
    assert snapshot.cycle == 1
    assert snapshot.state_summary["mission_status"] == "executing"
    assert snapshot.state_summary["task_count"] == 1
    assert snapshot.state_summary["task_status_counts"] == {"pending": 1}
    assert snapshot.failure_injections_active == [{"t": "x"}]

    stored = await db.get_snapshots_collection().find_one({"snapshot_id": snapshot.snapshot_id})
    assert stored is not None
    assert stored["cycle"] == 1


# --------------------------------------------------------------------------- #
# Mission ownership (SP2 Task A)
# --------------------------------------------------------------------------- #
async def test_create_mission_stores_owner(mock_db):
    m = await db.create_mission("g", "GENERAL", owner_id="user-1")
    assert m.owner_id == "user-1"
    fetched = await db.get_mission(m.mission_id)
    assert fetched.owner_id == "user-1"


async def test_create_mission_defaults_owner_none(mock_db):
    m = await db.create_mission("g", "GENERAL")
    assert m.owner_id is None


async def test_list_recent_missions_filters_by_owner(mock_db):
    await db.create_mission("a", "GENERAL", owner_id="user-1")
    await db.create_mission("b", "GENERAL", owner_id="user-2")
    await db.create_mission("c", "GENERAL", owner_id="user-1")
    mine = await db.list_recent_missions(owner_id="user-1")
    assert {m.goal for m in mine} == {"a", "c"}
    all_missions = await db.list_recent_missions()  # no filter → all
    assert len(all_missions) == 3


async def test_get_owned_mission(mock_db):
    m = await db.create_mission("g", "GENERAL", owner_id="user-1")
    assert (await db.get_owned_mission(m.mission_id, "user-1")).mission_id == m.mission_id
    assert await db.get_owned_mission(m.mission_id, "user-2") is None       # wrong owner
    assert await db.get_owned_mission("nonexistent", "user-1") is None      # missing
