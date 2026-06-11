"""SP6.1: orchestrator.run_incident routes incidents to the structured workflow."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from mcp_tools.dynatrace import DynatraceClient
from mcp_tools.gitlab import GitLabClient
from modules.incident_autopilot import workflow as wf
from orchestrator.orchestrator import NERVEOrchestrator
from state import database as db


async def test_run_incident_without_dynatrace_marks_failed(mock_db, monkeypatch):
    import config
    # Ensure global env has no Dynatrace so the "no Dynatrace" guard fires.
    monkeypatch.setattr(config.settings, "dynatrace_environment_url", "")
    monkeypatch.setattr(config.settings, "dynatrace_api_token", "")
    owner = await db.create_user("o@x.io", "h")  # no user-level dynatrace configured
    m = await db.create_mission("incident", "INCIDENT_RESPONSE", owner_id=owner.user_id)
    orch = NERVEOrchestrator()
    await orch.run_incident(m.mission_id, "PROB-1", owner.user_id)
    assert (await db.get_mission(m.mission_id)).status == "failed"
    assert m.mission_id not in orch._incident_workflows  # nothing launched


async def test_run_incident_with_dynatrace_launches_workflow(mock_db, monkeypatch):
    owner = await db.create_user("o@x.io", "h")
    await db.upsert_user_settings(owner.user_id, {
        "dynatrace_environment_url": "https://abc.live.dynatrace.com",
        "dynatrace_api_token": "dt-token",
        "gitlab_url": "https://gitlab.com", "gitlab_token": "glpat-x", "gitlab_project_id": "77",
    })
    m = await db.create_mission("incident", "INCIDENT_RESPONSE", owner_id=owner.user_id)
    monkeypatch.setattr(DynatraceClient, "connect", AsyncMock())
    monkeypatch.setattr(GitLabClient, "connect", AsyncMock())
    captured = {}
    async def fake_run(self, problem_id, mission_id):
        captured["problem_id"] = problem_id
        captured["mission_id"] = mission_id
        captured["project_id"] = self._project_id
    monkeypatch.setattr(wf.IncidentAutopilotWorkflow, "run", fake_run)

    orch = NERVEOrchestrator()
    await orch.run_incident(m.mission_id, "PROB-9", owner.user_id)
    assert m.mission_id in orch._incident_workflows
    await orch._incident_workflows[m.mission_id][-1]  # await the launch task
    assert captured == {"problem_id": "PROB-9", "mission_id": m.mission_id, "project_id": "77"}
    assert (await db.get_mission(m.mission_id)).status != "failed"


async def test_run_incident_never_raises_on_connect_error(mock_db, monkeypatch):
    owner = await db.create_user("o@x.io", "h")
    await db.upsert_user_settings(owner.user_id, {
        "dynatrace_environment_url": "https://abc.live.dynatrace.com", "dynatrace_api_token": "dt-token",
    })
    m = await db.create_mission("incident", "INCIDENT_RESPONSE", owner_id=owner.user_id)
    monkeypatch.setattr(DynatraceClient, "connect", AsyncMock(side_effect=RuntimeError("boom")))
    orch = NERVEOrchestrator()
    await orch.run_incident(m.mission_id, "PROB-1", owner.user_id)  # must not raise
    assert (await db.get_mission(m.mission_id)).status == "failed"
