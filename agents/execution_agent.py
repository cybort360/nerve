"""ExecutionAgent: runs a task by calling the appropriate MCP tool.

Routes a task to a Dynatrace or GitLab MCP client method based on its
description, wraps the call in the standard MCP retry policy, and persists the
outcome through the state layer. Mutating actions are never executed without a
prior approved :class:`~state.models.Action` (CLAUDE.md invariant 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, Awaitable, Callable

from pydantic import BaseModel
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from agents.base_agent import (
    EVENT_ACTION_EXECUTED,
    EVENT_AGENT_OBSERVATION,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    SOURCE_AGENT,
    AgentResult,
    BaseAgent,
)
from exceptions import MCPError, MCPToolCallError
from state import database as db
from state.models import MissionState, Task

ToolFn = Callable[..., Awaitable[dict]]

APPROVED_STATUS = "approved"
ACTION_EXECUTED_STATUS = "executed"


@dataclass
class ToolPlan:
    """A resolved MCP call: the bound client method plus its metadata."""

    func: ToolFn
    mutating: bool
    name: str
    action_type: str | None


def _to_jsonable(value: Any) -> Any:
    """Convert a typed MCP result (Pydantic model/list) into a storable structure.

    Typed client methods return Pydantic models; task results must be plain
    JSON-safe data before they are persisted through the state layer.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return value
    return value


def _is_recoverable_mcp(exc: BaseException) -> bool:
    """Return True if ``exc`` is a recoverable MCP error worth retrying."""
    return isinstance(exc, MCPError) and exc.recoverable


class ExecutionAgent(BaseAgent):
    """Executes one task by invoking the MCP tool its description implies."""

    def __init__(
        self,
        mission_id: str,
        dynatrace: Any,
        gitlab: Any,
        *,
        web_search: Any | None = None,
        max_attempts: int = 3,
        backoff_min: float = 2.0,
        backoff_max: float = 10.0,
    ) -> None:
        """Initialize the execution agent.

        Args:
            mission_id: Mission this agent serves.
            dynatrace: Dynatrace MCP client (async methods).
            gitlab: GitLab MCP client (async methods).
            web_search: Web-search client (async ``search`` method).
            max_attempts: MCP retry attempts (ARCHITECTURE.md retry policy).
            backoff_min: Minimum exponential backoff seconds.
            backoff_max: Maximum exponential backoff seconds.
        """
        super().__init__(name="execution", mission_id=mission_id)
        self.dynatrace = dynatrace
        self.gitlab = gitlab
        self.web_search = web_search
        self._max_attempts = max_attempts
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max

    async def run(self, context: dict) -> AgentResult:
        """Execute the task carried in ``context``.

        Args:
            context: Must contain ``task`` (Task or dict). May contain
                ``tool_args`` and ``action_id``.

        Returns:
            AgentResult describing the execution outcome.
        """
        task = context["task"]
        if isinstance(task, dict):
            task = Task.model_validate(task)
        return await self.execute(
            task, context.get("tool_args"), context.get("action_id")
        )

    def observe(self, state: MissionState) -> list[str]:
        """Note how many tasks are still pending execution."""
        pending = sum(1 for t in state.tasks if t.status == "pending")
        return [f"{pending} tasks pending execution"]

    async def execute(
        self,
        task: Task,
        tool_args: dict | None = None,
        action_id: str | None = None,
    ) -> AgentResult:
        """Resolve the tool, enforce approval, and invoke it.

        Args:
            task: Task to execute.
            tool_args: Keyword arguments for the MCP call.
            action_id: Associated action id, required for mutating tools.

        Returns:
            AgentResult with the result or failure reason.
        """
        start = perf_counter()
        plan = self._resolve_tool(task.tool)
        if plan is None and task.tool is not None:
            return await self._fail(task, "tool_unavailable", {"tool": task.tool}, start)
        if plan is None:
            plan = self._resolve(task.description)
        if plan is None:
            return await self._fail(task, "no_matching_tool", {"description": task.description}, start)
        if plan.mutating:
            blocked = await self._approval_block(task, action_id, start)
            if blocked is not None:
                return blocked
        return await self._invoke(task, plan, tool_args or {}, action_id, start)

    async def _invoke(
        self, task: Task, plan: ToolPlan, tool_args: dict, action_id: str | None, start: float
    ) -> AgentResult:
        """Call the MCP tool with retries and persist a success outcome."""
        try:
            raw = await self._call_with_retry(plan.func, tool_args)
        except MCPError as exc:
            return await self._fail(task, str(exc), exc.context, start)
        result = _to_jsonable(raw)
        await db.update_task_status(task.task_id, "completed", result=result)
        await db.emit_event(
            self.mission_id,
            EVENT_TASK_COMPLETED,
            {"task_id": task.task_id, "tool": plan.name},
            SOURCE_AGENT,
            task_id=task.task_id,
        )
        if plan.mutating and action_id is not None:
            await self._finalize_action(action_id)
        self._log.info("task_executed", task_id=task.task_id, tool=plan.name)
        return self._finish("success", {"result": result, "tool": plan.name}, [f"executed {plan.name}"], start)

    async def _call_with_retry(self, func: ToolFn, tool_args: dict) -> Any:
        """Invoke an MCP tool with the standard retry policy.

        Retries only recoverable MCP errors (e.g. connection, rate-limit);
        non-recoverable errors (auth) fail immediately.
        """
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=1, min=self._backoff_min, max=self._backoff_max),
            retry=retry_if_exception(_is_recoverable_mcp),
            reraise=True,
        ):
            with attempt:
                try:
                    return await func(**tool_args)
                except TypeError as exc:
                    raise MCPToolCallError(
                        f"bad tool arguments: {exc}",
                        context={"args": list(tool_args)},
                        recoverable=False,
                    ) from exc
        raise MCPToolCallError("retry loop exhausted", context={"mission_id": self.mission_id})

    async def _approval_block(
        self, task: Task, action_id: str | None, start: float
    ) -> AgentResult | None:
        """Return a blocked result if no approved action backs a mutating task."""
        if action_id is None:
            return await self._block(task, None, start)
        action = await db.get_action(action_id)
        if action is None or action.status != APPROVED_STATUS:
            return await self._block(task, action_id, start)
        return None

    async def _block(self, task: Task, action_id: str | None, start: float) -> AgentResult:
        """Record that execution was withheld pending approval; leave task pending."""
        payload = {"reason": "pending_approval", "task_id": task.task_id, "action_id": action_id}
        await db.emit_event(
            self.mission_id, EVENT_AGENT_OBSERVATION, payload, SOURCE_AGENT, task_id=task.task_id
        )
        self._log.info("execution_blocked", task_id=task.task_id, action_id=action_id)
        return self._finish(
            "failed", {"blocked": "pending_approval", "action_id": action_id}, ["action awaiting approval"], start
        )

    async def _finalize_action(self, action_id: str) -> None:
        """Mark an action executed and emit ACTION_EXECUTED."""
        await db.update_action_status(action_id, ACTION_EXECUTED_STATUS, executed_at=datetime.utcnow())
        await db.emit_event(self.mission_id, EVENT_ACTION_EXECUTED, {"action_id": action_id}, SOURCE_AGENT)

    async def _fail(self, task: Task, reason: str, ctx: dict, start: float) -> AgentResult:
        """Mark a task failed, emit TASK_FAILED, and return a failed result."""
        await db.update_task_status(task.task_id, "failed", error=reason)
        await db.emit_event(
            self.mission_id,
            EVENT_TASK_FAILED,
            {"task_id": task.task_id, "reason": reason, "context": ctx},
            SOURCE_AGENT,
            task_id=task.task_id,
        )
        self._log.error("task_failed", task_id=task.task_id, reason=reason)
        return self._finish("failed", {"error": reason}, [f"task failed: {reason}"], start)

    def _resolve_tool(self, tool: str | None) -> ToolPlan | None:
        """Map an explicit task.tool name to a ToolPlan (None when unset/unknown)."""
        if tool == "web_search" and self.web_search is not None:
            return ToolPlan(self.web_search.search, mutating=False, name="web_search", action_type=None)
        return None

    def _resolve(self, description: str) -> ToolPlan | None:
        """Map a task description to an MCP tool, most-specific match first.

        Args:
            description: Task description text.

        Returns:
            A :class:`ToolPlan`, or ``None`` if no rule matches.
        """
        text = description.lower()
        for keywords, client, method, mutating, action_type in self._routes():
            if any(keyword in text for keyword in keywords):
                return ToolPlan(getattr(client, method), mutating, method, action_type)
        return None

    def _routes(self) -> list[tuple[tuple[str, ...], Any, str, bool, str | None]]:
        """Routing table: (keywords, client, method, mutating, action_type)."""
        return [
            (("rollback", "trigger pipeline", "pipeline"), self.gitlab, "trigger_pipeline", True, "gitlab_rollback"),
            (("merge request", "hotfix"), self.gitlab, "create_merge_request", True, "gitlab_mr"),
            (("issue",), self.gitlab, "create_issue", True, "gitlab_issue"),
            (("problem detail", "timeline", "affected"), self.dynatrace, "get_problem_details", False, None),
            (("metric", "error rate", "latency", "throughput"), self.dynatrace, "get_metrics", False, None),
            (("problem", "anomaly", "incident"), self.dynatrace, "get_problems", False, None),
            (("deployment", "deploy"), self.gitlab, "list_deployments", False, None),
            (("commit", "diff"), self.gitlab, "get_commit", False, None),
        ]
