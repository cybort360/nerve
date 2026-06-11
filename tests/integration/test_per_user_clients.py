"""SP3.3: the execution agent uses the mission owner's integration config."""
from __future__ import annotations

from orchestrator.orchestrator import NERVEOrchestrator
from state import database as db


async def test_agent_uses_owner_tavily_key(mock_db):
    user = await db.create_user("a@b.io", "h")
    await db.upsert_user_settings(user.user_id, {"tavily_api_key": "owner-tavily-key"})
    m = await db.create_mission("research", "GENERAL", owner_id=user.user_id)
    orch = NERVEOrchestrator()
    agent = await orch._default_execution_agent(m.mission_id)
    assert agent.web_search._api_key == "owner-tavily-key"


async def test_agent_falls_back_to_global_when_owner_unset(mock_db):
    from config import settings as g
    user = await db.create_user("a@b.io", "h")
    m = await db.create_mission("research", "GENERAL", owner_id=user.user_id)  # no user_settings stored
    orch = NERVEOrchestrator()
    agent = await orch._default_execution_agent(m.mission_id)
    assert agent.web_search._api_key == g.tavily_api_key


async def test_agent_uses_owner_gitlab_token(mock_db):
    user = await db.create_user("a@b.io", "h")
    await db.upsert_user_settings(user.user_id, {"gitlab_token": "owner-glpat"})
    m = await db.create_mission("incident", "INCIDENT_RESPONSE", owner_id=user.user_id)
    orch = NERVEOrchestrator()
    agent = await orch._default_execution_agent(m.mission_id)
    # GitLabClient stores auth headers via BaseMCPClient as self._auth_headers
    assert agent.gitlab._auth_headers.get("PRIVATE-TOKEN") == "owner-glpat"
