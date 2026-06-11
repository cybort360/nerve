"""The ``nerve-planner`` Google Cloud Agent Builder (ADK) agent.

This is NERVE's managed orchestration layer: an ADK ``Agent`` whose one job is to
take an operational goal and call the ``decompose_goal`` tool, which returns a
structured task plan. The same agent runs two ways:

* **Deployed** to Vertex AI Agent Engine (see ``scripts/deploy_planner_agent.py``).
  ``orchestrator.planner`` invokes it by resource name when
  ``AGENT_BUILDER_AGENT_ID`` is set.
* **In-process** via the ADK runner inside Cloud Run, when no deployed resource
  is configured.

Either way, ``orchestrator.planner.MissionPlanner`` falls back to direct Gemini
if this agent is unavailable, so planning never hard-fails on the managed layer.

DESIGN CONSTRAINT: this module and ``planning_contract`` are packaged and shipped
to the Agent Engine runtime, which has no MongoDB or NERVE ``config``. So nothing
here may import ``config`` or the state layer — runtime values come from
``os.environ`` (Agent Engine injects ``GOOGLE_CLOUD_PROJECT`` etc.).
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog

from orchestrator.planning_contract import build_prompt

log = structlog.get_logger()

AGENT_NAME = "nerve_planner"
_DEFAULT_MODEL = "gemini-2.5-flash"
_USER_ID = "nerve-orchestrator"

_AGENT_INSTRUCTION = (
    "You are nerve-planner, NERVE's mission planner. You receive an operational "
    "goal. Call the `decompose_goal` tool exactly once, passing the goal verbatim "
    "as `goal` (and any provided context as `context`). The tool returns the task "
    "plan as JSON. Return that JSON object as your final answer with no extra "
    "commentary."
)


def _model_name() -> str:
    """Return the Gemini model id from the environment."""
    return os.environ.get("GEMINI_MODEL", _DEFAULT_MODEL)


def _genai_client() -> Any:
    """Build a Vertex AI genai client from environment configuration."""
    from google import genai  # lazy: only needed when the tool actually runs

    return genai.Client(
        vertexai=True,
        project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None,
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
    )


def _strip_fence(text: str) -> str:
    """Strip a ```json fence from model output, if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = (
            cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        )
    return cleaned


def decompose_goal(goal: str, context: str = "") -> dict:
    """Decompose an operational goal into an ordered list of executable tasks.

    This is the function tool the nerve-planner agent calls. It asks Gemini to
    turn the goal into the NERVE task-plan contract and returns the parsed tasks.

    Args:
        goal: The operational goal to decompose.
        context: Optional JSON string of mission context.

    Returns:
        A dict ``{"tasks": [ ... ]}`` where each task has ``description``,
        ``agent_role``, ``depends_on``, ``tool``, and ``tool_args``.
    """
    prompt = build_prompt(goal, context or "{}")
    client = _genai_client()
    response = client.models.generate_content(model=_model_name(), contents=prompt)
    data = json.loads(_strip_fence(response.text or ""))
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    return {"tasks": tasks}


def build_planner_agent() -> Any:
    """Construct the nerve-planner ADK agent (deployable and in-process)."""
    from google.adk.agents import Agent
    from google.adk.tools import FunctionTool

    return Agent(
        name=AGENT_NAME,
        model=_model_name(),
        instruction=_AGENT_INSTRUCTION,
        tools=[FunctionTool(decompose_goal)],
    )


def build_adk_app() -> Any:
    """Wrap the planner agent in an AdkApp (queryable locally and when deployed)."""
    from vertexai.preview import reasoning_engines

    return reasoning_engines.AdkApp(agent=build_planner_agent())


def _tasks_from_events(events: list[dict]) -> list[dict]:
    """Extract the task list from an ADK event stream.

    Prefers the ``decompose_goal`` tool's structured ``function_response`` (the
    source of truth); falls back to JSON in the agent's final text response.

    Args:
        events: Serialized ADK events from ``stream_query``.

    Returns:
        The list of task dicts, or an empty list if none could be extracted.
    """
    final_text = ""
    for event in events:
        content = event.get("content") if isinstance(event, dict) else None
        for part in (content or {}).get("parts", []) or []:
            fr = part.get("function_response")
            if fr and fr.get("name") == decompose_goal.__name__:
                resp = fr.get("response") or {}
                tasks = resp.get("tasks")
                if tasks is None and isinstance(resp.get("result"), dict):
                    tasks = resp["result"].get("tasks")
                if isinstance(tasks, list):
                    return tasks
            if isinstance(part.get("text"), str):
                final_text = part["text"]
    if final_text:
        try:
            data = json.loads(_strip_fence(final_text))
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                return data["tasks"]
        except (json.JSONDecodeError, TypeError):
            return []
    return []


async def _stream_plan(queryable: Any, goal: str, context: dict) -> list[dict]:
    """Run a goal through an AdkApp / deployed agent and return raw task dicts."""
    import asyncio

    message = f"GOAL: {goal}\nCONTEXT: {json.dumps(context or {})}"

    def _collect() -> list[dict]:
        events = list(queryable.stream_query(user_id=_USER_ID, message=message))
        return _tasks_from_events(events)

    return await asyncio.to_thread(_collect)


async def decompose_via_agent(goal: str, context: dict) -> list[dict]:
    """Plan a goal through the Agent Builder agent (deployed or in-process).

    Host-side entry point used by ``orchestrator.planner``. Reads NERVE config to
    decide whether to call the deployed Agent Engine resource or run the agent
    in-process via the ADK runner.

    Args:
        goal: Raw goal text.
        context: Mission context.

    Returns:
        A list of raw task-definition dicts (validated by the caller).

    Raises:
        Exception: Propagated to the caller, which treats any failure as a
            signal to fall back to direct Gemini.
    """
    from config import settings  # host-side only; safe here (not shipped)

    resource_name = settings.agent_builder_agent_id
    if resource_name:
        import vertexai
        from vertexai import agent_engines

        vertexai.init(
            project=settings.google_cloud_project or None,
            location=settings.agent_builder_location,
        )
        remote = agent_engines.get(resource_name)
        log.info("planning_via_deployed_agent", resource=resource_name)
        return await _stream_plan(remote, goal, context)

    log.info("planning_via_in_process_agent")
    return await _stream_plan(build_adk_app(), goal, context)
