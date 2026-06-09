"""PlannerAgent: decomposes a goal into an ordered, dependency-aware task list.

Calls Gemini to produce a JSON task plan, validates it into :class:`Task`
models, and resolves index-based dependencies into task ids. Raises
:class:`~exceptions.PlanningFailedError` if generation, parsing, or validation
fails, or if the plan has fewer than the minimum number of tasks.
"""

from __future__ import annotations

import json
from time import perf_counter
from typing import Awaitable, Callable

from pydantic import ValidationError as PydanticValidationError
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from agents.base_agent import AgentResult, BaseAgent
from config import settings
from exceptions import PlanningFailedError
from state.models import MissionState, Task

GenerateFn = Callable[[str], Awaitable[str]]

MIN_TASKS = 2
VALID_ROLES = ("planner", "execution", "risk", "auditor")

_PROMPT_TEMPLATE = (
    "You are the PlannerAgent for an autonomous incident-response system.\n"
    "Decompose the GOAL into an ordered list of tasks. Respond with ONLY a JSON "
    "array. Each element must be an object with keys: 'description' (string), "
    "'agent_role' (one of {roles}), and 'depends_on' (array of zero-based indices "
    "of earlier tasks in this array). Produce at least {min_tasks} tasks.\n\n"
    "GOAL: {goal}\nCONTEXT: {context}\n"
)


class PlannerAgent(BaseAgent):
    """Turns a high-level goal into a list of insert-ready :class:`Task` models."""

    def __init__(
        self,
        mission_id: str,
        generate: GenerateFn | None = None,
        *,
        max_attempts: int = 3,
        backoff_min: float = 2.0,
        backoff_max: float = 15.0,
    ) -> None:
        """Initialize the planner.

        Args:
            mission_id: Mission whose goal is being decomposed.
            generate: Async text-generation function; defaults to Gemini.
            max_attempts: Gemini retry attempts (ARCHITECTURE.md retry policy).
            backoff_min: Minimum exponential backoff seconds.
            backoff_max: Maximum exponential backoff seconds.
        """
        super().__init__(name="planner", mission_id=mission_id)
        self._generate: GenerateFn = generate or self._default_generate
        self._max_attempts = max_attempts
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max

    async def run(self, context: dict) -> AgentResult:
        """Decompose ``context['goal']`` and return the tasks as output.

        Args:
            context: Must contain ``goal``; may contain ``context`` (mission ctx).

        Returns:
            AgentResult whose output holds serialized tasks.

        Raises:
            PlanningFailedError: Propagated for the orchestrator to handle.
        """
        start = perf_counter()
        tasks = await self.plan(context["goal"], context.get("context", {}))
        output = {"tasks": [t.model_dump(mode="json") for t in tasks]}
        observations = [f"decomposed goal into {len(tasks)} tasks"]
        self._log.info("planning_complete", task_count=len(tasks))
        return self._finish("success", output, observations, start)

    def observe(self, state: MissionState) -> list[str]:
        """Note the mission's status and how many tasks already exist."""
        return [
            f"mission status is {state.mission.status}",
            f"{len(state.tasks)} tasks already exist",
        ]

    async def plan(self, goal: str, context: dict) -> list[Task]:
        """Generate and validate a task plan for a goal.

        Args:
            goal: Raw goal text.
            context: Mission context passed to the model.

        Returns:
            List of validated :class:`Task` models with resolved dependencies.

        Raises:
            PlanningFailedError: On generation, parse, validation, or size failure.
        """
        prompt = _PROMPT_TEMPLATE.format(
            roles=", ".join(VALID_ROLES), min_tasks=MIN_TASKS, goal=goal, context=context
        )
        raw = await self._generate_with_retry(prompt)
        tasks = self._build_tasks(raw)
        if len(tasks) < MIN_TASKS:
            raise PlanningFailedError(
                "decomposition produced fewer than the minimum tasks",
                context={"mission_id": self.mission_id, "task_count": len(tasks)},
                recoverable=True,
            )
        return tasks

    def _build_tasks(self, raw: str) -> list[Task]:
        """Parse model output into Task models with resolved dependencies.

        Args:
            raw: Raw model text (optionally fenced JSON).

        Returns:
            List of :class:`Task` models.

        Raises:
            PlanningFailedError: If parsing or validation fails.
        """
        try:
            items = self._parse_json(raw)
            tasks = [
                Task(
                    mission_id=self.mission_id,
                    agent_role=item["agent_role"],
                    description=item["description"],
                )
                for item in items
            ]
            self._resolve_dependencies(tasks, items)
            return tasks
        except (json.JSONDecodeError, KeyError, TypeError, PydanticValidationError) as exc:
            raise PlanningFailedError(
                "failed to parse or validate plan",
                context={"mission_id": self.mission_id, "error": str(exc)},
                recoverable=True,
            ) from exc

    @staticmethod
    def _parse_json(raw: str) -> list[dict]:
        """Strip optional markdown fences and parse a JSON array."""
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(cleaned)
        if not isinstance(data, list):
            raise TypeError("plan is not a JSON array")
        return data

    @staticmethod
    def _resolve_dependencies(tasks: list[Task], items: list[dict]) -> None:
        """Map zero-based ``depends_on`` indices to concrete task ids in place."""
        for task, item in zip(tasks, items):
            for idx in item.get("depends_on", []) or []:
                if isinstance(idx, int) and 0 <= idx < len(tasks) and tasks[idx] is not task:
                    task.depends_on.append(tasks[idx].task_id)

    async def _generate_with_retry(self, prompt: str) -> str:
        """Call the generation function with the Gemini retry policy.

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
        except Exception as exc:  # noqa: BLE001 — translate any generation failure.
            raise PlanningFailedError(
                "gemini generation failed",
                context={"mission_id": self.mission_id, "error": str(exc)},
                recoverable=True,
            ) from exc
        raise PlanningFailedError("generation produced no output", context={"mission_id": self.mission_id})

    async def _default_generate(self, prompt: str) -> str:
        """Generate text via Vertex AI Gemini (imported lazily).

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
