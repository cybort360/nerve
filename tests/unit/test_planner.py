"""Unit tests for MissionPlanner and the /internal/decompose route."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from exceptions import PlanningFailedError
from orchestrator.planner import MissionPlanner, TaskDefinition

MISSION_ID = "m-1"

_PLAN = json.dumps([
    {"description": "detect anomaly", "agent_role": "execution", "depends_on": []},
    {"description": "assemble context", "agent_role": "execution", "depends_on": [0]},
    {"description": "score risk", "agent_role": "risk", "depends_on": [1]},
])


async def test_decompose_returns_task_definitions():
    planner = MissionPlanner(generate=AsyncMock(return_value=_PLAN))
    definitions = await planner.decompose("fix checkout", {})
    assert len(definitions) == 3
    assert all(isinstance(d, TaskDefinition) for d in definitions)
    assert definitions[1].depends_on == [0]


async def test_plan_builds_tasks_with_resolved_dependencies():
    planner = MissionPlanner(generate=AsyncMock(return_value=_PLAN))
    tasks = await planner.plan(MISSION_ID, "fix checkout", {})
    assert len(tasks) == 3
    assert all(t.mission_id == MISSION_ID for t in tasks)
    assert tasks[1].depends_on == [tasks[0].task_id]
    assert tasks[2].depends_on == [tasks[1].task_id]


async def test_plan_raises_on_too_few_tasks():
    one = json.dumps([{"description": "only one", "agent_role": "execution", "depends_on": []}])
    planner = MissionPlanner(generate=AsyncMock(return_value=one))
    with pytest.raises(PlanningFailedError):
        await planner.plan(MISSION_ID, "g", {})


async def test_decompose_raises_on_invalid_json():
    planner = MissionPlanner(generate=AsyncMock(return_value="not json"))
    with pytest.raises(PlanningFailedError):
        await planner.decompose("g", {})


async def test_generation_failure_becomes_planning_failed():
    planner = MissionPlanner(generate=AsyncMock(side_effect=RuntimeError("vertex down")), backoff_min=0, backoff_max=0)
    with pytest.raises(PlanningFailedError):
        await planner.decompose("g", {})


async def test_decompose_route_returns_definitions(monkeypatch):
    import main

    monkeypatch.setattr(main._decompose_planner, "_generate", AsyncMock(return_value=_PLAN))
    result = await main.decompose(main.DecomposeRequest(goal="fix checkout", context={}))
    assert len(result) == 3
    assert result[0].description == "detect anomaly"


async def test_decompose_route_raises_http_502_on_failure(monkeypatch):
    import main
    from fastapi import HTTPException

    monkeypatch.setattr(main._decompose_planner, "_generate", AsyncMock(return_value="garbage"))
    with pytest.raises(HTTPException) as exc:
        await main.decompose(main.DecomposeRequest(goal="g", context={}))
    assert exc.value.status_code == 502
