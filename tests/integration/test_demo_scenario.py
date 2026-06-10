"""End-to-end verification of the seeded demo scenario.

Drives the demo pieces deterministically (no real timing/background polling):
seeded workflow run → pending rollback → approval → seeded rollback execution →
resolver resolution. Confirms the full event chain and the approval gate.
"""

from __future__ import annotations

import pytest

from config import settings
from failure_engine.demo_scenario import build_seeded_clients, _make_seeded_reason
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
    """Seeded workflow run produces a PENDING gitlab_rollback action (the demo's key milestone)."""
    mission = await db.create_mission("checkout incident", "INCIDENT_RESPONSE")
    engine = FailureEngine(mission_id=mission.mission_id)
    dynatrace, gitlab, _state, seeded_deployment = await build_seeded_clients(engine, mission.mission_id)
    workflow = IncidentAutopilotWorkflow(
        dynatrace, gitlab, reason=_make_seeded_reason(seeded_deployment), project_id=PROJECT_ID
    )

    await workflow.run(PROBLEM_ID, mission.mission_id)

    actions = {a["action_type"]: a for a in await db.get_actions_collection().find({"mission_id": mission.mission_id}).to_list(None)}
    assert actions["gitlab_issue"]["status"] == "executed"
    assert actions["gitlab_rollback"]["status"] == "pending"  # gated on approval

    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    for expected in ["INCIDENT_DETECTED", "CONTEXT_ASSEMBLED", "REASONING_COMPLETE", "ACTION_CREATED", "ACTION_EXECUTED", "RESOLUTION_MONITORING_STARTED", "MCP_TOOL_CALLED"]:
        assert expected in events, f"missing {expected}"


async def test_pending_rollback_with_empty_real_gitlab_deployments(mock_db, demo_enabled):
    """REGRESSION: rollback action must be created even when list_recent_deployments returns [].

    This exercises the exact bug scenario: the _DemoGitLabClient (used when GitLab is
    configured with real credentials) queries a real project that has no recent deployments.
    The seeded reason function must still return the seeded deployment — not blindly use
    whatever (possibly empty) list the workflow passes in.
    """
    from unittest.mock import AsyncMock, patch
    from mcp_tools.gitlab import GitLabClient

    mission = await db.create_mission("checkout incident", "INCIDENT_RESPONSE")
    engine = FailureEngine(mission_id=mission.mission_id)
    dynatrace, gitlab, _state, seeded_deployment = await build_seeded_clients(engine, mission.mission_id)

    # Simulate the real-GitLab path: list_recent_deployments returns [] (no matching deploys).
    with patch.object(type(gitlab), "list_recent_deployments", new_callable=lambda: lambda self: AsyncMock(return_value=[])) as _:
        gitlab.list_recent_deployments = AsyncMock(return_value=[])
        workflow = IncidentAutopilotWorkflow(
            dynatrace, gitlab, reason=_make_seeded_reason(seeded_deployment), project_id=PROJECT_ID
        )
        await workflow.run(PROBLEM_ID, mission.mission_id)

    actions = {a["action_type"]: a for a in await db.get_actions_collection().find({"mission_id": mission.mission_id}).to_list(None)}
    # The rollback MUST still be created even with 0 deployments from list_recent_deployments.
    assert "gitlab_rollback" in actions, (
        "No gitlab_rollback action found — the seeded reason function must return the "
        "seeded deployment regardless of what list_recent_deployments returns"
    )
    assert actions["gitlab_rollback"]["status"] == "pending"

    # Verify the CONTEXT_ASSEMBLED event reported 0 deployments (confirming the empty-list path).
    events = await db.get_recent_events_for_mission(mission.mission_id)
    context_assembled = next((e for e in events if e.event_type == "CONTEXT_ASSEMBLED"), None)
    assert context_assembled is not None
    assert context_assembled.payload["deployments"] == 0, (
        "Expected CONTEXT_ASSEMBLED to report 0 deployments (the real-GitLab empty path)"
    )

    # And the recommendation must be rollback (not downgraded to investigate).
    reasoning_event = next((e for e in events if e.event_type == "REASONING_COMPLETE"), None)
    assert reasoning_event is not None
    assert reasoning_event.payload["recommendation"] == "rollback"
    assert reasoning_event.payload["correlated_deployment_id"] == 42


async def test_full_chain_approval_rollback_resolution(mock_db, demo_enabled):
    """Approve → seeded rollback flips the service → resolver closes the incident."""
    mission = await db.create_mission("checkout incident", "INCIDENT_RESPONSE")
    engine = FailureEngine(mission_id=mission.mission_id)
    dynatrace, gitlab, state, seeded_deployment = await build_seeded_clients(engine, mission.mission_id)
    workflow = IncidentAutopilotWorkflow(
        dynatrace, gitlab, reason=_make_seeded_reason(seeded_deployment), project_id=PROJECT_ID
    )
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
    dynatrace, _gitlab, _state, _deployment = await build_seeded_clients(engine, mission.mission_id)
    await engine.inject(FailureScenario(failure_type=FailureType.CONTRADICTORY_METRICS, target="get_metrics", severity=0.9))

    from datetime import datetime, timedelta
    metrics = await dynatrace.get_service_metrics("checkout", datetime.utcnow() - timedelta(minutes=5), datetime.utcnow())
    assert metrics.raw_metrics.get("_contradictory") is True  # failure transform applied
