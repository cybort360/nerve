import json

from orchestrator.planner import MissionPlanner

PLAN_JSON = json.dumps([
    {"description": "Search for cheapest 2026 World Cup tickets",
     "agent_role": "execution", "depends_on": [],
     "tool": "web_search", "tool_args": {"query": "cheapest 2026 World Cup tickets"}},
    {"description": "Search hotels near the stadium with good reviews",
     "agent_role": "execution", "depends_on": [],
     "tool": "web_search", "tool_args": {"query": "cheap well-reviewed hotel near World Cup stadium"}},
])


async def test_planner_carries_tool_intents_onto_tasks():
    async def fake_generate(prompt: str) -> str:
        return PLAN_JSON

    planner = MissionPlanner(generate=fake_generate)
    tasks = await planner.plan("m1", "find cheap ticket + hotel", {})
    assert all(t.tool == "web_search" for t in tasks)
    assert tasks[0].tool_args["query"].startswith("cheapest")
