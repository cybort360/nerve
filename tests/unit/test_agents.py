"""Unit tests for the agent system (agents/).

State-layer functions and MCP clients are mocked. Covers planner decomposition
and failure modes, execution routing/retry/approval gate, risk scoring, and
auditor consistency checks.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.auditor_agent import AuditorAgent
from agents.execution_agent import ExecutionAgent
from agents.planner_agent import PlannerAgent
from agents.risk_agent import RiskAgent
from exceptions import MCPConnectionError, PlanningFailedError
from state.models import Action, Mission, MissionState, Task

MISSION_ID = "m-1"


@pytest.fixture
def db_mocks(monkeypatch):
    """Patch the state-layer functions the agents call with AsyncMocks."""
    from state import database

    mocks = SimpleNamespace(
        emit_event=AsyncMock(),
        update_task_status=AsyncMock(),
        get_action=AsyncMock(),
        update_action_status=AsyncMock(),
    )
    for name in ("emit_event", "update_task_status", "get_action", "update_action_status"):
        monkeypatch.setattr(database, name, getattr(mocks, name))
    return mocks


def _make_state(mission_status="executing", tasks=None) -> MissionState:
    mission = Mission(mission_id=MISSION_ID, goal="g", mission_type="INCIDENT_RESPONSE", status=mission_status)
    return MissionState(mission=mission, tasks=tasks or [], recent_events=[])


def _task(description="get problems", status="pending", **kw) -> Task:
    return Task(mission_id=MISSION_ID, agent_role="execution", description=description, status=status, **kw)


# --------------------------------------------------------------------------- #
# PlannerAgent
# --------------------------------------------------------------------------- #
async def test_planner_decomposes_with_dependencies():
    plan_json = json.dumps([
        {"description": "detect anomaly", "agent_role": "execution", "depends_on": []},
        {"description": "assemble context", "agent_role": "execution", "depends_on": [0]},
        {"description": "score risk", "agent_role": "risk", "depends_on": [1]},
    ])
    planner = PlannerAgent(MISSION_ID, generate=AsyncMock(return_value=plan_json))
    tasks = await planner.plan("fix checkout", {})

    assert len(tasks) == 3
    assert all(t.mission_id == MISSION_ID for t in tasks)
    assert tasks[1].depends_on == [tasks[0].task_id]
    assert tasks[2].depends_on == [tasks[1].task_id]


async def test_planner_run_returns_serialized_tasks():
    plan_json = json.dumps([
        {"description": "a", "agent_role": "execution", "depends_on": []},
        {"description": "b", "agent_role": "auditor", "depends_on": [0]},
    ])
    planner = PlannerAgent(MISSION_ID, generate=AsyncMock(return_value=plan_json))
    result = await planner.run({"goal": "g", "context": {}})

    assert result.status == "success"
    assert len(result.output["tasks"]) == 2
    assert planner.report()["status"] == "success"


async def test_planner_raises_when_too_few_tasks():
    one = json.dumps([{"description": "only one", "agent_role": "execution", "depends_on": []}])
    planner = PlannerAgent(MISSION_ID, generate=AsyncMock(return_value=one))
    with pytest.raises(PlanningFailedError):
        await planner.plan("g", {})


async def test_planner_raises_on_invalid_json():
    planner = PlannerAgent(MISSION_ID, generate=AsyncMock(return_value="not json at all"))
    with pytest.raises(PlanningFailedError):
        await planner.plan("g", {})


async def test_planner_strips_markdown_fences():
    fenced = "```json\n" + json.dumps([
        {"description": "a", "agent_role": "execution", "depends_on": []},
        {"description": "b", "agent_role": "risk", "depends_on": [0]},
    ]) + "\n```"
    planner = PlannerAgent(MISSION_ID, generate=AsyncMock(return_value=fenced))
    tasks = await planner.plan("g", {})
    assert len(tasks) == 2


# --------------------------------------------------------------------------- #
# ExecutionAgent
# --------------------------------------------------------------------------- #
def _exec_agent(dynatrace=None, gitlab=None) -> ExecutionAgent:
    return ExecutionAgent(
        MISSION_ID,
        dynatrace or SimpleNamespace(),
        gitlab or SimpleNamespace(),
        backoff_min=0,
        backoff_max=0,
    )


async def test_execution_runs_read_tool_and_completes(db_mocks):
    dynatrace = SimpleNamespace(get_problems=AsyncMock(return_value={"problems": [1]}))
    agent = _exec_agent(dynatrace=dynatrace)
    result = await agent.execute(_task("get active problems"))

    assert result.status == "success"
    assert result.output["result"] == {"problems": [1]}
    dynatrace.get_problems.assert_awaited_once()
    db_mocks.update_task_status.assert_awaited_once()
    assert db_mocks.update_task_status.await_args.args[1] == "completed"
    assert db_mocks.emit_event.await_args.args[1] == "TASK_COMPLETED"


async def test_execution_retries_then_fails(db_mocks):
    gitlab = SimpleNamespace(
        list_deployments=AsyncMock(side_effect=MCPConnectionError("dynatrace down"))
    )
    agent = _exec_agent(gitlab=gitlab)
    result = await agent.execute(_task("list deployments for service"))

    assert result.status == "failed"
    assert gitlab.list_deployments.await_count == 3  # retried to the attempt limit
    assert db_mocks.update_task_status.await_args.args[1] == "failed"
    assert db_mocks.emit_event.await_args.args[1] == "TASK_FAILED"


async def test_execution_blocks_when_action_pending(db_mocks):
    gitlab = SimpleNamespace(trigger_pipeline=AsyncMock(return_value={"ok": True}))
    db_mocks.get_action.return_value = Action(
        mission_id=MISSION_ID, action_type="gitlab_rollback", status="pending"
    )
    agent = _exec_agent(gitlab=gitlab)
    result = await agent.execute(_task("trigger rollback pipeline"), action_id="a-1")

    assert result.status == "failed"
    assert result.output["blocked"] == "pending_approval"
    gitlab.trigger_pipeline.assert_not_awaited()
    db_mocks.update_task_status.assert_not_awaited()
    assert db_mocks.emit_event.await_args.args[1] == "AGENT_OBSERVATION"


async def test_execution_runs_approved_action(db_mocks):
    gitlab = SimpleNamespace(trigger_pipeline=AsyncMock(return_value={"pipeline": "started"}))
    db_mocks.get_action.return_value = Action(
        mission_id=MISSION_ID, action_type="gitlab_rollback", status="approved"
    )
    agent = _exec_agent(gitlab=gitlab)
    result = await agent.execute(_task("trigger rollback pipeline"), action_id="a-1")

    assert result.status == "success"
    gitlab.trigger_pipeline.assert_awaited_once()
    db_mocks.update_action_status.assert_awaited_once()
    assert db_mocks.update_action_status.await_args.args[1] == "executed"
    emitted = {call.args[1] for call in db_mocks.emit_event.await_args_list}
    assert {"TASK_COMPLETED", "ACTION_EXECUTED"} <= emitted


async def test_execution_no_matching_tool_fails(db_mocks):
    agent = _exec_agent()
    result = await agent.execute(_task("do something completely unrelated"))
    assert result.status == "failed"
    assert result.output["error"] == "no_matching_tool"
    assert db_mocks.update_task_status.await_args.args[1] == "failed"


# --------------------------------------------------------------------------- #
# RiskAgent
# --------------------------------------------------------------------------- #
async def test_risk_clean_state_is_zero(db_mocks):
    tasks = [_task(status="completed"), _task(status="completed")]
    agent = RiskAgent(MISSION_ID)
    result = await agent.run({"state": _make_state(tasks=tasks), "failure_injections": []})

    risk = result.output["risk"]
    assert risk["overall"] == 0.0
    assert risk["threshold_exceeded"] is False
    assert set(risk["breakdown"]) == {"failed_tasks", "retries", "failure_injections", "contradictions"}
    assert db_mocks.emit_event.await_args.args[1] == "RISK_SCORE_UPDATED"


async def test_risk_high_state_exceeds_threshold(db_mocks):
    tasks = [_task(status="failed"), _task(status="failed")]
    injections = [{"type": "CONTRADICTORY_METRICS", "severity": 1.0}]
    agent = RiskAgent(MISSION_ID)
    score = agent.assess(_make_state(tasks=tasks), injections)

    assert score.overall > 0.7
    assert score.threshold_exceeded is True
    assert score.breakdown["failed_tasks"] == 1.0


# --------------------------------------------------------------------------- #
# AuditorAgent
# --------------------------------------------------------------------------- #
async def test_auditor_flags_stale_task(db_mocks):
    stale = _task(status="in_progress", updated_at=datetime.utcnow() - timedelta(minutes=10))
    agent = AuditorAgent(MISSION_ID)
    result = await agent.run({"state": _make_state(tasks=[stale])})

    findings = result.output["findings"]
    assert any(f["type"] == "stale_task" for f in findings)
    assert db_mocks.emit_event.await_args.args[1] == "AGENT_OBSERVATION"


async def test_auditor_clean_state_has_no_findings(db_mocks):
    tasks = [
        _task(status="completed"),
        _task(status="in_progress", updated_at=datetime.utcnow()),
    ]
    agent = AuditorAgent(MISSION_ID)
    findings = agent.audit(_make_state(mission_status="executing", tasks=tasks))
    assert findings == []


async def test_auditor_flags_completed_with_error(db_mocks):
    tasks = [_task(status="completed", error="boom")]
    agent = AuditorAgent(MISSION_ID)
    findings = agent.audit(_make_state(tasks=tasks))
    assert any(f["type"] == "completed_with_error" for f in findings)


async def test_auditor_flags_resolved_with_open_tasks(db_mocks):
    tasks = [_task(status="pending")]
    agent = AuditorAgent(MISSION_ID)
    findings = agent.audit(_make_state(mission_status="resolved", tasks=tasks))
    assert any(f["type"] == "resolved_with_open_tasks" for f in findings)
