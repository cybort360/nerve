"""Integration test: one full orchestration cycle with mocked agents.

Uses the real in-memory state layer (mongomock) as the persistence double and
replaces the agent factories with stubs, so the test exercises the orchestrator's
real control flow — risk scoring, ready-task execution, audit, snapshot, and
resolution — without Gemini or live MCP servers. (No external creds required, so
this is intentionally not gated behind the NERVE_INTEGRATION flag.)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from agents.base_agent import AgentResult
from orchestrator.orchestrator import NERVEOrchestrator
from state import database as db
from state.models import Task


def _task(mission_id: str, description: str) -> Task:
    return Task(mission_id=mission_id, agent_role="execution", description=description)


class _StubExecutionAgent:
    """Execution agent stub that marks each task it runs as completed."""

    def __init__(self) -> None:
        self.run = AsyncMock(side_effect=self._run)

    async def _run(self, context: dict) -> AgentResult:
        task = context["task"]
        await db.update_task_status(task.task_id, "completed", result={"ok": True})
        return AgentResult(status="success", output={"task_id": task.task_id})


def _orchestrator_with_stubs(risk: float) -> tuple[NERVEOrchestrator, dict]:
    """Build an orchestrator whose agent factories return tracked stubs."""
    risk_agent = AsyncMock()
    risk_agent.run = AsyncMock(return_value=AgentResult(status="success", output={"risk": {"overall": risk}}))
    auditor = AsyncMock()
    auditor.run = AsyncMock(return_value=AgentResult(status="success", output={"findings": []}))
    execution = _StubExecutionAgent()

    orch = NERVEOrchestrator()
    orch.risk_agent_factory = lambda mid: risk_agent
    orch.auditor_agent_factory = lambda mid: auditor
    orch.execution_agent_factory = lambda mid: execution
    return orch, {"risk": risk_agent, "auditor": auditor, "execution": execution}


async def test_one_full_cycle_runs_tasks_audits_and_snapshots(mock_db):
    mission = await db.create_mission("fix checkout", "INCIDENT_RESPONSE")
    await db.add_tasks([
        _task(mission.mission_id, "get active problems"),
        _task(mission.mission_id, "list recent deployments"),
    ])
    await db.update_mission_status(mission.mission_id, "executing")
    fresh = await db.get_mission(mission.mission_id)

    orch, agents = _orchestrator_with_stubs(risk=0.0)
    await orch._execute_cycle(fresh)

    agents["risk"].run.assert_awaited_once()
    agents["auditor"].run.assert_awaited_once()
    assert agents["execution"].run.await_count == 2  # both ready tasks executed

    tasks = await db.get_tasks_for_mission(mission.mission_id)
    assert all(t.status == "completed" for t in tasks)

    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    assert events.count("TASK_STARTED") == 2
    assert "SNAPSHOT_TAKEN" in events

    snapshot = await db.get_snapshots_collection().find_one({"mission_id": mission.mission_id})
    assert snapshot is not None and snapshot["cycle"] == 1

    resolved = await db.get_mission(mission.mission_id)
    assert resolved.status == "resolved"  # all tasks completed


async def test_high_risk_triggers_replan(mock_db):
    mission = await db.create_mission("fix checkout", "INCIDENT_RESPONSE")
    await db.add_tasks([_task(mission.mission_id, "get active problems")])
    await db.update_mission_status(mission.mission_id, "executing")
    fresh = await db.get_mission(mission.mission_id)

    orch, _ = _orchestrator_with_stubs(risk=0.95)  # above default threshold 0.7
    planner_agent = AsyncMock()
    planner_agent.run = AsyncMock(
        return_value=AgentResult(
            status="success",
            output={"tasks": [
                _task(mission.mission_id, "replanned a").model_dump(mode="json"),
                _task(mission.mission_id, "replanned b").model_dump(mode="json"),
            ]},
        )
    )
    orch.planner_agent_factory = lambda mid: planner_agent

    await orch._execute_cycle(fresh)

    planner_agent.run.assert_awaited_once()
    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    assert "REPLAN_TRIGGERED" in events
