"""MissionPlanner: decomposes a goal into a task plan.

INTENDED PRODUCTION DESIGN (deferred):
    Planning should run through a Google Cloud Agent Builder agent (``nerve-planner``)
    that reasons over the goal and calls back our ``POST /internal/decompose`` route
    as a function tool to obtain structured task definitions.

CURRENT IMPLEMENTATION:
    Agent Builder requires billing to be enabled, which it is not yet, so this
    class decomposes goals with **direct Gemini API calls**. The class is the
    single seam for planning: swapping in the Agent Builder / google-adk agent
    later only requires changing this file (ideally only ``_decompose_raw``).

# TODO: replace with google-adk Agent once billing is enabled.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from config import settings
from exceptions import PlanningFailedError
from state.models import AgentRole, Task

GenerateFn = Callable[[str], Awaitable[str]]

MIN_TASKS = 2
VALID_ROLES = ("planner", "execution", "risk", "auditor")

_PROMPT_TEMPLATE = (
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


class TaskDefinition(BaseModel):
    """A single decomposed task definition (the /internal/decompose contract)."""

    description: str
    agent_role: AgentRole
    depends_on: list[int] = Field(default_factory=list)
    tool: str | None = None
    tool_args: dict = Field(default_factory=dict)


class MissionPlanner:
    """Decomposes goals into tasks. The one file to change when swapping planners."""

    def __init__(
        self,
        generate: GenerateFn | None = None,
        *,
        max_attempts: int = 3,
        backoff_min: float = 2.0,
        backoff_max: float = 15.0,
    ) -> None:
        """Initialize the planner.

        Args:
            generate: Async text-generation function; defaults to direct Gemini.
            max_attempts: Gemini retry attempts (ARCHITECTURE.md retry policy).
            backoff_min: Minimum exponential backoff seconds.
            backoff_max: Maximum exponential backoff seconds.
        """
        self._generate: GenerateFn = generate or self._default_generate
        self._max_attempts = max_attempts
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max

    async def decompose(self, goal: str, context: dict) -> list[TaskDefinition]:
        """Decompose a goal into structured task definitions.

        This is the function-tool surface exposed by ``POST /internal/decompose``.

        Args:
            goal: Raw goal text.
            context: Mission context passed to the model.

        Returns:
            List of :class:`TaskDefinition`.

        Raises:
            PlanningFailedError: On generation, parse, or validation failure.
        """
        prompt = _PROMPT_TEMPLATE.format(
            roles=", ".join(VALID_ROLES), min_tasks=MIN_TASKS, goal=goal, context=context
        )
        raw = await self._generate_with_retry(prompt)
        return self._parse(raw)

    async def plan(self, mission_id: str, goal: str, context: dict) -> list[Task]:
        """Decompose a goal and build insert-ready Task models for a mission.

        Args:
            mission_id: Mission the tasks belong to.
            goal: Raw goal text.
            context: Mission context.

        Returns:
            List of validated :class:`Task` models with resolved dependencies.

        Raises:
            PlanningFailedError: On failure or if fewer than ``MIN_TASKS`` result.
        """
        definitions = await self.decompose(goal, context)
        tasks = self._to_tasks(mission_id, definitions)
        if len(tasks) < MIN_TASKS:
            raise PlanningFailedError(
                "decomposition produced fewer than the minimum tasks",
                context={"mission_id": mission_id, "task_count": len(tasks)},
                recoverable=True,
            )
        return tasks

    @staticmethod
    def _to_tasks(mission_id: str, definitions: list[TaskDefinition]) -> list[Task]:
        """Convert definitions into Task models, resolving index dependencies."""
        tasks = [
            Task(
                mission_id=mission_id,
                agent_role=d.agent_role,
                description=d.description,
                tool=d.tool,
                tool_args=d.tool_args,
            )
            for d in definitions
        ]
        for task, definition in zip(tasks, definitions):
            for idx in definition.depends_on:
                if 0 <= idx < len(tasks) and tasks[idx] is not task:
                    task.depends_on.append(tasks[idx].task_id)
        return tasks

    def _parse(self, raw: str) -> list[TaskDefinition]:
        """Parse model output into validated task definitions.

        Raises:
            PlanningFailedError: If the output is not a valid task-definition array.
        """
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(cleaned)
            if not isinstance(data, list):
                raise TypeError("plan is not a JSON array")
            return [TaskDefinition.model_validate(item) for item in data]
        except (json.JSONDecodeError, TypeError, PydanticValidationError) as exc:
            raise PlanningFailedError(
                "failed to parse or validate plan",
                context={"error": str(exc)},
                recoverable=True,
            ) from exc

    async def _generate_with_retry(self, prompt: str) -> str:
        """Run generation with the Gemini retry policy, translating failures.

        Raises:
            PlanningFailedError: If generation fails after all attempts.
        """
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=1, min=self._backoff_min, max=self._backoff_max),
                reraise=True,
            ):
                with attempt:
                    return await self._generate(prompt)
        except Exception as exc:  # noqa: BLE001 — translate any generation failure
            raise PlanningFailedError(
                "planner generation failed",
                context={"error": str(exc)},
                recoverable=True,
            ) from exc
        raise PlanningFailedError("generation produced no output", context={})

    async def _default_generate(self, prompt: str) -> str:
        """Generate text via Vertex AI Gemini (imported lazily).

        # TODO: replace with google-adk Agent once billing is enabled. The
        # Agent Builder agent would reason over the goal and call back
        # POST /internal/decompose; swapping it in only touches this method.

        Args:
            prompt: Prompt text.

        Returns:
            The model's text response.
        """
        import asyncio

        from vertexai.generative_models import GenerativeModel  # lazy: heavy import

        model = GenerativeModel(settings.gemini_model)
        response = await asyncio.to_thread(model.generate_content, prompt)
        return response.text


async def gemini_generate(prompt: str) -> str:
    """Public Gemini text-generation seam for modules outside the planner.

    Wraps the planner's default generator so callers don't reach into a private
    method. This is the single seam to update when migrating to google-adk.

    Args:
        prompt: Prompt text.

    Returns:
        The model's text response.
    """
    return await MissionPlanner()._default_generate(prompt)
