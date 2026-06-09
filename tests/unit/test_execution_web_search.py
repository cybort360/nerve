"""ExecutionAgent routes a web_search task to the web-search client."""
from __future__ import annotations

import pytest

from agents.execution_agent import ExecutionAgent
from mcp_tools.web_search import SearchResult, SearchResults
from state.models import Task


class _FakeWebSearch:
    def __init__(self):
        self.calls = []

    async def search(self, query: str, max_results: int | None = None) -> SearchResults:
        self.calls.append((query, max_results))
        return SearchResults(answer="ok", results=[SearchResult(title="t", url="u", content="c")])


@pytest.fixture
async def seeded_task(mock_db):
    from state import database as db
    mission = await db.create_mission("g", "GENERAL")
    task = Task(
        mission_id=mission.mission_id, agent_role="execution",
        description="Search for cheapest tickets", tool="web_search",
        tool_args={"query": "cheapest tickets"},
    )
    await db.add_tasks([task])
    return mission.mission_id, task


async def test_web_search_task_invokes_client(seeded_task):
    mission_id, task = seeded_task
    web = _FakeWebSearch()
    agent = ExecutionAgent(mission_id, dynatrace=None, gitlab=None, web_search=web)
    result = await agent.run({"task": task, "tool_args": task.tool_args})
    assert result.status == "success"
    assert web.calls == [("cheapest tickets", None)]


async def test_web_search_tool_set_but_client_missing_fails_cleanly(mock_db):
    from state import database as db
    mission = await db.create_mission("g", "GENERAL")
    task = Task(
        mission_id=mission.mission_id, agent_role="execution",
        description="list deployments and problems",  # would keyword-match if it fell through
        tool="web_search", tool_args={"query": "x"},
    )
    await db.add_tasks([task])
    agent = ExecutionAgent(mission.mission_id, dynatrace=None, gitlab=None)  # no web_search client
    result = await agent.run({"task": task, "tool_args": task.tool_args})
    assert result.status == "failed"
    # must NOT have silently routed to a dynatrace/gitlab tool


async def test_web_search_empty_tool_args_fails_gracefully(mock_db):
    from state import database as db
    mission = await db.create_mission("g", "GENERAL")
    task = Task(
        mission_id=mission.mission_id, agent_role="execution",
        description="search", tool="web_search", tool_args={},
    )
    await db.add_tasks([task])
    web = _FakeWebSearch()
    agent = ExecutionAgent(mission.mission_id, dynatrace=None, gitlab=None, web_search=web)
    # Empty tool_args -> search() called with no query -> TypeError -> converted to MCPError -> task fails, no uncaught exception
    result = await agent.run({"task": task, "tool_args": task.tool_args})
    assert result.status == "failed"
    assert web.calls == []  # client never successfully invoked
