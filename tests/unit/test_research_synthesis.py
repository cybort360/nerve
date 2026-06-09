"""Synthesis creates a human_approval_request hand-off action."""
from __future__ import annotations

import pytest

from modules.research_concierge.synthesis import synthesize_and_handoff
from state import database as db
from state.models import Task


async def _seed_completed_general(goal="find cheap ticket + hotel"):
    mission = await db.create_mission(goal, "GENERAL")
    task = Task(
        mission_id=mission.mission_id, agent_role="execution",
        description="Search tickets", tool="web_search",
        tool_args={"query": "cheapest ticket"},
        status="completed",
        result={"answer": "Cheapest is $100 at https://x/buy", "results": []},
    )
    await db.add_tasks([task])
    return mission.mission_id


async def test_synthesis_creates_approval_action(mock_db):
    mission_id = await _seed_completed_general()

    async def fake_generate(prompt: str) -> str:
        return "Cheapest ticket: $100 https://x/buy. Best hotel: Y 8.9 https://x/hotel"

    await synthesize_and_handoff(mission_id, generate=fake_generate)

    actions = await db.get_actions_for_mission(mission_id, status="pending")
    assert len(actions) == 1
    assert actions[0].action_type == "human_approval_request"
    assert "Cheapest ticket" in actions[0].payload["recommendation"]


async def test_synthesis_survives_generation_failure(mock_db):
    mission_id = await _seed_completed_general()

    async def boom(prompt: str) -> str:
        raise RuntimeError("gemini down")

    await synthesize_and_handoff(mission_id, generate=boom)  # must NOT raise
    actions = await db.get_actions_for_mission(mission_id, status="pending")
    assert actions == []
