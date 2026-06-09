"""End-to-end test of the Incident Autopilot module with fixture data.

Mocks the Dynatrace/GitLab MCP clients and injects the reasoning result, so the
full workflow control flow runs without Gemini or live MCP servers. (No real
credentials needed, so this is not gated behind the NERVE_INTEGRATION flag.)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp_tools.dynatrace import DynatraceProblemDetail, ServiceMetrics
from mcp_tools.gitlab import GitLabDeployment, GitLabIssue
from modules.incident_autopilot.resolver import IncidentResolver
from modules.incident_autopilot.workflow import CorrelationResult, IncidentAutopilotWorkflow
from state import database as db

PROBLEM_ID = "P-42"
PROJECT_ID = "123"
NOW = datetime.utcnow()


def _problem() -> DynatraceProblemDetail:
    return DynatraceProblemDetail(
        problem_id=PROBLEM_ID,
        title="High error rate on checkout",
        severity="AVAILABILITY",
        status="OPEN",
        impacted_services=["checkout"],
        root_cause="payment_processor.py change",
        timeline=[{"timestamp": NOW.isoformat(), "description": "error spike +340%"}],
        start_time=NOW - timedelta(minutes=30),
    )


def _deployment() -> GitLabDeployment:
    return GitLabDeployment(
        id=42, status="success", ref="main", sha="abc123", environment="production",
        created_at=NOW - timedelta(minutes=61),
    )


def _issue() -> GitLabIssue:
    return GitLabIssue(id=900, iid=7, title="[NERVE] High error rate on checkout", state="opened",
                       web_url="https://gitlab.test/issues/7", labels=["incident"])


def _metrics(error_rate: float) -> ServiceMetrics:
    return ServiceMetrics(service_id="checkout", error_rate=error_rate, from_time=NOW - timedelta(hours=1), to_time=NOW)


def _clients(deployments):
    dynatrace = SimpleNamespace(
        get_problem_details=AsyncMock(return_value=_problem()),
        get_service_metrics=AsyncMock(return_value=_metrics(0.34)),
    )
    gitlab = SimpleNamespace(
        list_recent_deployments=AsyncMock(return_value=deployments),
        create_issue=AsyncMock(return_value=_issue()),
        create_merge_request=AsyncMock(),
        trigger_pipeline=AsyncMock(),
        close_issue=AsyncMock(),
    )
    return dynatrace, gitlab


async def _actions(mission_id: str) -> list[dict]:
    return await db.get_actions_collection().find({"mission_id": mission_id}).to_list(length=None)


async def test_rollback_path_creates_pending_rollback_and_files_issue(mock_db):
    mission = await db.create_mission("checkout incident", "INCIDENT_RESPONSE")
    dynatrace, gitlab = _clients([_deployment()])
    fake_resolver = SimpleNamespace(monitor_resolution=AsyncMock())
    reason = AsyncMock(return_value=CorrelationResult(
        correlated_deployment=_deployment(), confidence=0.9, reasoning="deploy 42 caused it", recommendation="rollback"
    ))
    workflow = IncidentAutopilotWorkflow(
        dynatrace, gitlab, reason=reason, resolver=fake_resolver, project_id=PROJECT_ID
    )

    await workflow.run(PROBLEM_ID, mission.mission_id)
    await asyncio.sleep(0)  # let the resolver background task start

    # Issue filed with the canonical labels.
    gitlab.create_issue.assert_awaited_once()
    assert gitlab.create_issue.await_args.args[3] == ["incident", "p1", "nerve-created"]

    # Two actions: issue (executed) and rollback (still pending, never executed).
    actions = {a["action_type"]: a for a in await _actions(mission.mission_id)}
    assert actions["gitlab_issue"]["status"] == "executed"
    assert actions["gitlab_rollback"]["status"] == "pending"

    # Rollback was NOT executed.
    gitlab.trigger_pipeline.assert_not_awaited()
    gitlab.create_merge_request.assert_not_awaited()

    # Resolver scheduled, and key events emitted.
    fake_resolver.monitor_resolution.assert_awaited_once()
    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    assert "INCIDENT_DETECTED" in events
    assert "REASONING_COMPLETE" in events
    assert events.count("ACTION_CREATED") == 2
    assert "ACTION_EXECUTED" in events
    assert "RESOLUTION_MONITORING_STARTED" in events


async def test_no_deployment_defaults_to_investigate_no_rollback(mock_db):
    mission = await db.create_mission("checkout incident", "INCIDENT_RESPONSE")
    dynatrace, gitlab = _clients([])  # no deployments
    fake_resolver = SimpleNamespace(monitor_resolution=AsyncMock())
    # Reasoner says rollback, but there is no deployment -> must downgrade to investigate.
    reason = AsyncMock(return_value=CorrelationResult(
        correlated_deployment=None, confidence=0.3, reasoning="no correlation", recommendation="rollback"
    ))
    workflow = IncidentAutopilotWorkflow(
        dynatrace, gitlab, reason=reason, resolver=fake_resolver, project_id=PROJECT_ID
    )

    await workflow.run(PROBLEM_ID, mission.mission_id)

    action_types = {a["action_type"] for a in await _actions(mission.mission_id)}
    assert action_types == {"gitlab_issue"}  # no rollback action created
    gitlab.create_issue.assert_awaited_once()  # issue still filed


async def test_resolver_resolves_after_consecutive_normal_checks(mock_db):
    mission = await db.create_mission("checkout incident", "INCIDENT_RESPONSE")
    baseline = 0.02
    # Three consecutive readings within 10% of baseline -> resolve.
    dynatrace = SimpleNamespace(
        get_service_metrics=AsyncMock(side_effect=[_metrics(0.020), _metrics(0.021), _metrics(0.019)])
    )
    gitlab = SimpleNamespace(close_issue=AsyncMock())
    resolver = IncidentResolver(
        dynatrace, gitlab, project_id=PROJECT_ID, poll_interval_seconds=0, required_consecutive=3, max_checks=5
    )

    await resolver.monitor_resolution(mission.mission_id, "checkout", 7, baseline)

    gitlab.close_issue.assert_awaited_once_with(PROJECT_ID, 7)
    resolved = await db.get_mission(mission.mission_id)
    assert resolved.status == "resolved"
    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    assert "ISSUE_CLOSED" in events
    assert "MISSION_STATUS_CHANGED" in events


async def test_resolver_resets_consecutive_on_spike(mock_db):
    mission = await db.create_mission("checkout incident", "INCIDENT_RESPONSE")
    baseline = 0.02
    # normal, normal, SPIKE (reset), normal, normal, normal -> resolves on the 6th check.
    readings = [_metrics(0.02), _metrics(0.02), _metrics(0.5), _metrics(0.02), _metrics(0.021), _metrics(0.019)]
    dynatrace = SimpleNamespace(get_service_metrics=AsyncMock(side_effect=readings))
    gitlab = SimpleNamespace(close_issue=AsyncMock())
    resolver = IncidentResolver(
        dynatrace, gitlab, project_id=PROJECT_ID, poll_interval_seconds=0, required_consecutive=3, max_checks=6
    )

    await resolver.monitor_resolution(mission.mission_id, "checkout", 7, baseline)

    gitlab.close_issue.assert_awaited_once()  # only resolved after the post-spike streak
    assert dynatrace.get_service_metrics.await_count == 6
