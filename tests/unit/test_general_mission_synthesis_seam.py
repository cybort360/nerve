"""Integration: orchestrator _check_resolution triggers synthesis for GENERAL only."""
from __future__ import annotations

import modules.research_concierge.synthesis as synth_mod
from orchestrator.orchestrator import NERVEOrchestrator
from state import database as db
from state.models import MissionState, Task


async def _completed_state(mission) -> MissionState:
    """Seed one completed task and return a MissionState with it."""
    task = Task(
        mission_id=mission.mission_id,
        agent_role="execution",
        description="search",
        tool="web_search",
        tool_args={"query": "q"},
        status="completed",
        result={"answer": "found it"},
    )
    await db.add_tasks([task])
    return MissionState(mission=mission, tasks=[task])


async def test_general_mission_triggers_synthesis(mock_db, monkeypatch):
    """GENERAL mission: synthesis is called and mission ends resolved."""
    calls: list[str] = []

    async def fake_synth(mission_id: str, **kw) -> None:
        calls.append(mission_id)

    monkeypatch.setattr(synth_mod, "synthesize_and_handoff", fake_synth)

    mission = await db.create_mission("find cheap ticket", "GENERAL")
    state = await _completed_state(mission)
    orch = NERVEOrchestrator()
    await orch._check_resolution(mission, state)

    assert calls == [mission.mission_id], "synthesize_and_handoff was not called for GENERAL"
    refreshed = await db.get_mission(mission.mission_id)
    assert refreshed.status == "resolved"


async def test_incident_mission_skips_synthesis(mock_db, monkeypatch):
    """INCIDENT_RESPONSE mission: synthesis is NOT called; mission still resolves."""
    calls: list[str] = []

    async def fake_synth(mission_id: str, **kw) -> None:
        calls.append(mission_id)

    monkeypatch.setattr(synth_mod, "synthesize_and_handoff", fake_synth)

    mission = await db.create_mission("incident", "INCIDENT_RESPONSE")
    state = await _completed_state(mission)
    orch = NERVEOrchestrator()
    await orch._check_resolution(mission, state)

    assert calls == [], "synthesize_and_handoff must NOT be called for INCIDENT_RESPONSE"
    refreshed = await db.get_mission(mission.mission_id)
    assert refreshed.status == "resolved"
