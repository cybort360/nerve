"""Unit tests for the Agent Builder (ADK) planning path and its fallback.

These tests never touch Vertex AI or Agent Engine: the ADK planner is injected
as ``decompose_fn`` (raw task dicts), and the direct-Gemini fallback is injected
as ``generate``. They verify the routing in ``MissionPlanner.decompose`` and the
event-extraction helper in ``orchestrator.planner_agent_def``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from orchestrator.planner import MissionPlanner, TaskDefinition
from orchestrator.planner_agent_def import _tasks_from_events, decompose_goal

_RAW_PLAN = [
    {"description": "search a", "agent_role": "execution", "depends_on": [],
     "tool": "web_search", "tool_args": {"query": "a"}},
    {"description": "search b", "agent_role": "execution", "depends_on": [],
     "tool": "web_search", "tool_args": {"query": "b"}},
]

_FALLBACK_PLAN = json.dumps([
    {"description": "fallback 1", "agent_role": "execution", "depends_on": []},
    {"description": "fallback 2", "agent_role": "execution", "depends_on": []},
])


async def test_agent_path_used_when_decompose_fn_injected():
    """An injected ADK planner is used and its dicts become TaskDefinitions."""
    agent = AsyncMock(return_value=_RAW_PLAN)
    generate = AsyncMock(return_value=_FALLBACK_PLAN)
    planner = MissionPlanner(generate=generate, decompose_fn=agent)

    definitions = await planner.decompose("find cheapest tickets", {"k": "v"})

    agent.assert_awaited_once_with("find cheapest tickets", {"k": "v"})
    generate.assert_not_awaited()  # fallback never reached
    assert [d.description for d in definitions] == ["search a", "search b"]
    assert all(isinstance(d, TaskDefinition) for d in definitions)


async def test_falls_back_to_gemini_when_agent_raises():
    """If the ADK planner errors, decompose falls back to direct Gemini."""
    agent = AsyncMock(side_effect=RuntimeError("agent engine 503"))
    generate = AsyncMock(return_value=_FALLBACK_PLAN)
    planner = MissionPlanner(generate=generate, decompose_fn=agent)

    definitions = await planner.decompose("g", {})

    agent.assert_awaited_once()
    generate.assert_awaited_once()
    assert [d.description for d in definitions] == ["fallback 1", "fallback 2"]


async def test_falls_back_when_agent_returns_empty():
    """An empty plan from the agent triggers the Gemini fallback."""
    planner = MissionPlanner(
        generate=AsyncMock(return_value=_FALLBACK_PLAN),
        decompose_fn=AsyncMock(return_value=[]),
    )
    definitions = await planner.decompose("g", {})
    assert [d.description for d in definitions] == ["fallback 1", "fallback 2"]


async def test_falls_back_when_agent_returns_invalid_task():
    """Invalid task dicts from the agent fall back rather than crashing."""
    bad = [{"description": "no role"}]  # missing required agent_role
    planner = MissionPlanner(
        generate=AsyncMock(return_value=_FALLBACK_PLAN),
        decompose_fn=AsyncMock(return_value=bad),
    )
    definitions = await planner.decompose("g", {})
    assert [d.description for d in definitions] == ["fallback 1", "fallback 2"]


def test_tasks_from_events_prefers_tool_response():
    """The tool's function_response is the source of truth for the plan."""
    events = [
        {"content": {"parts": [
            {"function_response": {"name": decompose_goal.__name__,
                                   "response": {"tasks": _RAW_PLAN}}},
        ]}},
        {"content": {"parts": [{"text": "[]"}]}},  # later prose ignored
    ]
    assert _tasks_from_events(events) == _RAW_PLAN


def test_tasks_from_events_handles_result_wrapped_response():
    """ADK may wrap a dict return under 'result' — still extract tasks."""
    events = [
        {"content": {"parts": [
            {"function_response": {"name": decompose_goal.__name__,
                                   "response": {"result": {"tasks": _RAW_PLAN}}}},
        ]}},
    ]
    assert _tasks_from_events(events) == _RAW_PLAN


def test_tasks_from_events_falls_back_to_final_text_json():
    """With no tool response, parse the agent's final JSON text."""
    events = [
        {"content": {"parts": [{"text": "```json\n" + json.dumps(_RAW_PLAN) + "\n```"}]}},
    ]
    assert _tasks_from_events(events) == _RAW_PLAN


def test_tasks_from_events_returns_empty_when_nothing_usable():
    assert _tasks_from_events([{"content": {"parts": [{"text": "no plan here"}]}}]) == []
    assert _tasks_from_events([]) == []
