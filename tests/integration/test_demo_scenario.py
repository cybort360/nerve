"""End-to-end verification of the seeded demo scenario.

Drives the demo pieces deterministically (no real timing/background polling):
seeded workflow run → pending rollback → approval → seeded rollback execution →
resolver resolution. Confirms the full event chain and the approval gate.
"""

from __future__ import annotations

import pytest

from config import settings
from failure_engine.demo_scenario import build_seeded_clients, _seeded_reason
from failure_engine.injector import FailureEngine, FailureScenario, FailureType
from modules.incident_autopilot.resolver import IncidentResolver
from modules.incident_autopilot.workflow import IncidentAutopilotWorkflow
from agents.execution_agent import ExecutionAgent
from state import database as db

PROJECT_ID = "123"
PROBLEM_ID = "DEMO-PROBLEM-1"


@pytest.fixture
def demo_enabled(monkeypatch):
    monkeypatch.setattr(settings, "failure_engine_enabled", True)
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "gitlab_project_id", PROJECT_ID)


async def test_seeded_workflow_files_issue_and_pending_rollback(mock_db, demo_enabled):
    mission = await db.create_mission("checkout incident", "INCIDENT_RESPONSE")
    engine = FailureEngine(mission_id=mission.mission_id)
    dynatrace, gitlab, _state = await build_seeded_clients(engine, mission.mission_id)
    workflow = IncidentAutopilotWorkflow(dynatrace, gitlab, reason=_seeded_reason, project_id=PROJECT_ID)

    await workflow.run(PROBLEM_ID, mission.mission_id)

    actions = {a["action_type"]: a for a in await db.get_actions_collection().find({"mission_id": mission.mission_id}).to_list(None)}
    assert actions["gitlab_issue"]["status"] == "executed"
    assert actions["gitlab_rollback"]["status"] == "pending"  # gated on approval

    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    for expected in ["INCIDENT_DETECTED", "CONTEXT_ASSEMBLED", "REASONING_COMPLETE", "ACTION_CREATED", "ACTION_EXECUTED", "RESOLUTION_MONITORING_STARTED", "MCP_TOOL_CALLED"]:
        assert expected in events, f"missing {expected}"


async def test_full_chain_approval_rollback_resolution(mock_db, demo_enabled):
    """Approve → seeded rollback flips the service → resolver closes the incident."""
    mission = await db.create_mission("checkout incident", "INCIDENT_RESPONSE")
    engine = FailureEngine(mission_id=mission.mission_id)
    dynatrace, gitlab, state = await build_seeded_clients(engine, mission.mission_id)
    workflow = IncidentAutopilotWorkflow(dynatrace, gitlab, reason=_seeded_reason, project_id=PROJECT_ID)
    await workflow.run(PROBLEM_ID, mission.mission_id)

    rollback = (await db.get_actions_for_mission(mission.mission_id, status="pending"))[0]

    # --- Approval gate: executing BEFORE approval must NOT run the rollback. ---
    agent = ExecutionAgent(mission.mission_id, dynatrace, gitlab, backoff_min=0, backoff_max=0)
    blocked_task = await db.create_task(mission.mission_id, "execution", "Execute rollback pipeline")
    blocked = await agent.execute(blocked_task, action_id=rollback.action_id)
    assert blocked.status == "failed"
    assert state.rolled_back is False  # service was NOT rolled back without approval

    # --- Approve, then execution proceeds and the seeded service recovers. ---
    await db.update_action_status(rollback.action_id, "approved", approved_by="demo-operator")
    task = await db.create_task(mission.mission_id, "execution", "Execute rollback pipeline")
    result = await agent.execute(task, action_id=rollback.action_id, tool_args={"project_id": PROJECT_ID, "ref": "main", "variables": {}})
    assert result.status == "success"
    assert state.rolled_back is True  # rollback executed after approval

    # --- Resolver now sees recovered metrics and closes the incident. ---
    resolver = IncidentResolver(dynatrace, gitlab, project_id=PROJECT_ID, poll_interval_seconds=0, required_consecutive=3, max_checks=5)
    await resolver.monitor_resolution(mission.mission_id, "checkout", 7, baseline_error_rate=0.02)

    resolved = await db.get_mission(mission.mission_id)
    assert resolved.status == "resolved"
    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    assert "ACTION_EXECUTED" in events
    assert "ISSUE_CLOSED" in events


async def test_contradictory_injection_perturbs_seeded_metrics(mock_db, demo_enabled):
    """An injected CONTRADICTORY_METRICS failure mutates the seeded metric read."""
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    engine = FailureEngine(mission_id=mission.mission_id)
    dynatrace, _gitlab, _state = await build_seeded_clients(engine, mission.mission_id)
    await engine.inject(FailureScenario(failure_type=FailureType.CONTRADICTORY_METRICS, target="get_metrics", severity=0.9))

    from datetime import datetime, timedelta
    metrics = await dynatrace.get_service_metrics("checkout", datetime.utcnow() - timedelta(minutes=5), datetime.utcnow())
    assert metrics.raw_metrics.get("_contradictory") is True  # failure transform applied
