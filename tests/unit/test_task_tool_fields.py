from state.models import Task


def test_task_defaults_have_no_tool():
    t = Task(mission_id="m", agent_role="execution", description="do thing")
    assert t.tool is None
    assert t.tool_args == {}


def test_task_accepts_tool_intent():
    t = Task(
        mission_id="m",
        agent_role="execution",
        description="search the web",
        tool="web_search",
        tool_args={"query": "cheapest tickets"},
    )
    assert t.tool == "web_search"
    assert t.tool_args["query"] == "cheapest tickets"
