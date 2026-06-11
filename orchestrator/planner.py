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

import structlog
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from config import settings
from exceptions import PlanningFailedError
from state.models import AgentRole, Task

GenerateFn = Callable[[str], Awaitable[str]]
# Async planner: (goal, context) -> list[TaskDefinition]. Backed by the deployed
# or in-process ADK agent; swapped for a fake in tests.
DecomposeFn = Callable[[str, dict], Awaitable[list["TaskDefinition"]]]

from orchestrator.planning_contract import (  # noqa: E402 — after type aliases
    MIN_TASKS,
    VALID_ROLES,
    build_prompt,
)

log = structlog.get_logger()


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
        decompose_fn: DecomposeFn | None = None,
        max_attempts: int = 3,
        backoff_min: float = 2.0,
        backoff_max: float = 15.0,
    ) -> None:
        """Initialize the planner.

        Planning prefers the Google Cloud Agent Builder (ADK) ``nerve-planner``
        agent — deployed to Vertex AI Agent Engine when ``agent_builder_agent_id``
        is configured, otherwise run in-process via the ADK runner. If the agent
        is unavailable (no credentials, deploy hiccup, quota), planning falls back
        to direct Gemini so a mission never stalls on the managed layer.

        Args:
            generate: Async text-generation function for the direct-Gemini
                fallback; defaults to Vertex AI Gemini. Injecting this also
                disables the ADK path (used by tests for deterministic output).
            decompose_fn: Async ``(goal, context) -> list[dict]`` planner backed
                by the ADK agent; defaults to the deployed/in-process agent.
                Injecting this overrides the default ADK runner (used by tests).
            max_attempts: Gemini retry attempts (ARCHITECTURE.md retry policy).
            backoff_min: Minimum exponential backoff seconds.
            backoff_max: Maximum exponential backoff seconds.
        """
        self._generate: GenerateFn = generate or self._default_generate
        self._decompose_fn = decompose_fn
        # The ADK agent is the primary planner unless a caller injected a direct
        # generator (tests) or Agent Builder is disabled in config.
        self._use_adk = (
            decompose_fn is None
            and generate is None
            and settings.agent_builder_enabled
        )
        self._max_attempts = max_attempts
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max

    async def decompose(self, goal: str, context: dict) -> list[TaskDefinition]:
        """Decompose a goal into structured task definitions.

        This is the function-tool surface exposed by ``POST /internal/decompose``.
        Tries the Agent Builder (ADK) agent first, then falls back to direct
        Gemini if the agent path is unavailable or returns nothing usable.

        Args:
            goal: Raw goal text.
            context: Mission context passed to the model.

        Returns:
            List of :class:`TaskDefinition`.

        Raises:
            PlanningFailedError: If both the ADK agent and direct Gemini fail.
        """
        definitions = await self._decompose_via_agent(goal, context)
        if definitions:
            return definitions
        prompt = build_prompt(goal, context)
        raw = await self._generate_with_retry(prompt)
        return self._parse(raw)

    async def _decompose_via_agent(
        self, goal: str, context: dict
    ) -> list[TaskDefinition] | None:
        """Run the ADK agent planner, returning ``None`` to signal fallback.

        Never raises: any failure (missing credentials, agent error, invalid
        output) is logged and turned into ``None`` so ``decompose`` falls back
        to direct Gemini.

        Args:
            goal: Raw goal text.
            context: Mission context.

        Returns:
            Validated task definitions, or ``None`` if the agent is unavailable.
        """
        fn = self._decompose_fn
        if fn is None:
            if not self._use_adk:
                return None
            try:
                from orchestrator.planner_agent_def import decompose_via_agent
            except Exception as exc:  # noqa: BLE001 — ADK optional at runtime
                log.warning("adk_import_failed_falling_back", error=str(exc))
                return None
            fn = decompose_via_agent
        try:
            raw = await fn(goal, context)
            definitions = [TaskDefinition.model_validate(item) for item in raw]
            if not definitions:
                return None
            log.info("planned_via_agent_builder", task_count=len(definitions))
            return definitions
        except Exception as exc:  # noqa: BLE001 — translate any agent failure
            log.warning("agent_builder_planner_unavailable", error=str(exc))
            return None

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
