"""The planning contract shared by the live planner and the deployable ADK agent.

This module is intentionally a dependency-light leaf: it imports nothing heavy
so it can be packaged and shipped to Vertex AI Agent Engine alongside
``planner_agent_def`` without dragging the rest of the application in. Both
``orchestrator.planner`` (direct-Gemini fallback) and
``orchestrator.planner_agent_def`` (the deployed/in-process ADK agent) import the
prompt and constants from here, so the two planning paths stay byte-identical.
"""

from __future__ import annotations

MIN_TASKS = 2
VALID_ROLES = ("planner", "execution", "risk", "auditor")

PROMPT_TEMPLATE = (
    "You are nerve-planner, decomposing an operational goal into executable tasks.\n"
    "Respond with ONLY a JSON array. Each element is an object with keys: "
    "'description' (string), 'agent_role' (one of {roles}), 'depends_on' "
    "(array of zero-based indices of earlier tasks), 'tool', and 'tool_args'.\n"
    "The ONLY available tool is 'web_search' with tool_args {{\"query\": <string>}}. "
    "EVERY task you emit MUST be an executable web_search task: set agent_role to "
    "'execution', tool to 'web_search', and a focused search query in "
    "tool_args.query.\n"
    "Do NOT create analysis, consolidation, comparison, ranking, or summary tasks "
    "and do NOT emit tasks without a tool — NERVE automatically synthesizes a final "
    "ranked recommendation from the search results after the searches complete. "
    "Decompose the goal into {min_tasks}+ distinct, non-overlapping web searches "
    "(make them parallel with empty depends_on unless one search genuinely needs "
    "another's result).\n\nGOAL: {goal}\nCONTEXT: {context}\n"
)


def build_prompt(goal: str, context: object) -> str:
    """Render the decomposition prompt for a goal and context.

    Args:
        goal: Raw goal text.
        context: Mission context (rendered into the prompt as text).

    Returns:
        The fully-formatted prompt string.
    """
    return PROMPT_TEMPLATE.format(
        roles=", ".join(VALID_ROLES), min_tasks=MIN_TASKS, goal=goal, context=context
    )
