"""Unit test for the orchestrator's task retry handling (TASK_RETRYING)."""

from __future__ import annotations

from config import settings
from orchestrator.orchestrator import NERVEOrchestrator
from state import database as db


async def test_failed_task_under_limit_is_requeued_and_emits_retrying(mock_db):
    mission = await db.create_mission("g", "GENERAL")
    task = await db.create_task(mission.mission_id, "execution", "call mcp")
    await db.update_task_status(task.task_id, "failed", error="MCP down")

    orch = NERVEOrchestrator()
    tasks = await db.get_tasks_for_mission(mission.mission_id)
    await orch._handle_retries(mission.mission_id, tasks)

    refreshed = await db.get_task(task.task_id)
    assert refreshed.status == "pending"  # re-queued for another attempt
    assert refreshed.retry_count == 1
    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    assert "TASK_RETRYING" in events


async def test_failed_task_at_limit_stays_failed(mock_db):
    mission = await db.create_mission("g", "GENERAL")
    task = await db.create_task(mission.mission_id, "execution", "call mcp")
    await db.update_task_status(task.task_id, "failed", error="MCP down", retry_count=settings.max_task_retries)

    orch = NERVEOrchestrator()
    tasks = await db.get_tasks_for_mission(mission.mission_id)
    await orch._handle_retries(mission.mission_id, tasks)

    refreshed = await db.get_task(task.task_id)
    assert refreshed.status == "failed"  # retry limit reached: terminal
