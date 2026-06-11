"""NERVEOrchestrator: the main async execution loop.

Runs one asyncio task per mission, advancing it through the cycle defined in
CLAUDE.md (score risk → maybe replan → run ready tasks → audit → snapshot →
check resolution). The loop is the only thing that changes ``missions.status``
(invariant 4), never crashes silently, and handles cancellation cleanly.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Awaitable, Callable

import structlog

from agents.auditor_agent import AuditorAgent
from agents.execution_agent import ExecutionAgent
from agents.planner_agent import PlannerAgent
from agents.risk_agent import RiskAgent
from config import settings
from effective_config import resolve_effective_settings
from exceptions import PlanningFailedError
from mcp_tools.dynatrace import DynatraceClient
from mcp_tools.gitlab import GitLabClient
from mcp_tools.web_search import WebSearchClient
from orchestrator.mission_graph import MissionGraph
from orchestrator.planner import MissionPlanner
from state import database as db
from state.models import Mission, MissionState, Task

SOURCE_ORCH = "orchestrator"
EVENT_MISSION_STATUS_CHANGED = "MISSION_STATUS_CHANGED"
EVENT_TASK_STARTED = "TASK_STARTED"
EVENT_TASK_RETRYING = "TASK_RETRYING"
EVENT_REPLAN_TRIGGERED = "REPLAN_TRIGGERED"
EVENT_SNAPSHOT_TAKEN = "SNAPSHOT_TAKEN"
EVENT_INCIDENT_FAILED = "INCIDENT_FAILED"

TERMINAL_STATUSES = ("resolved", "failed")
MAX_REPLANS = 3


class NERVEOrchestrator:
    """Manages per-mission orchestration loops and their lifecycle."""

    def __init__(self, *, planner: MissionPlanner | None = None, failure_engine: Any | None = None) -> None:
        """Initialize the orchestrator.

        Args:
            planner: Goal decomposer for initial planning (defaults to MissionPlanner).
            failure_engine: FailureEngine providing active injections when enabled.
        """
        self._planner = planner or MissionPlanner()
        self._failure_engine = failure_engine
        self._active: dict[str, asyncio.Task] = {}
        self._cycles: dict[str, int] = {}
        self._replans: dict[str, int] = {}
        self._incident_workflows: dict[str, tuple] = {}
        self._log = structlog.get_logger().bind(component="orchestrator")
        # Agent construction seams — overridable for testing.
        self.risk_agent_factory: Callable[[str], RiskAgent] = lambda mid: RiskAgent(mid)
        self.auditor_agent_factory: Callable[[str], AuditorAgent] = lambda mid: AuditorAgent(mid)
        self.planner_agent_factory: Callable[[str], PlannerAgent] = lambda mid: PlannerAgent(mid)
        self.execution_agent_factory: Callable[[str], Awaitable[ExecutionAgent]] = self._default_execution_agent

    @property
    def active_mission_ids(self) -> list[str]:
        """Return the ids of missions with a running loop."""
        return list(self._active)

    # ----------------------------------------------------------------- #
    # Lifecycle control
    # ----------------------------------------------------------------- #
    async def run_mission(self, mission_id: str) -> asyncio.Task:
        """Start (or return the existing) orchestration loop for a mission.

        Args:
            mission_id: Mission to run.

        Returns:
            The asyncio Task running the mission loop.
        """
        existing = self._active.get(mission_id)
        if existing is not None and not existing.done():
            return existing
        self._cycles[mission_id] = 0
        self._replans[mission_id] = 0
        task = asyncio.create_task(self._run_loop(mission_id), name=f"mission-{mission_id}")
        self._active[mission_id] = task
        self._log.info("mission_started", mission_id=mission_id)
        return task

    async def stop_mission(self, mission_id: str) -> None:
        """Cancel a mission's loop and wait for it to unwind.

        Args:
            mission_id: Mission to stop.
        """
        task = self._active.get(mission_id)
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._active.pop(mission_id, None)
        self._log.info("mission_stopped", mission_id=mission_id)

    async def shutdown(self) -> None:
        """Stop every active mission loop (called on app shutdown)."""
        for mission_id in list(self._active):
            await self.stop_mission(mission_id)
        for mission_id, (workflow, dynatrace, gitlab, task) in list(self._incident_workflows.items()):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(Exception):
                await dynatrace.disconnect()
                await gitlab.disconnect()
            self._incident_workflows.pop(mission_id, None)

    # ----------------------------------------------------------------- #
    # Incident workflow
    # ----------------------------------------------------------------- #

    async def run_incident(self, mission_id: str, problem_id: str, owner_id: str | None) -> None:
        """Run the structured incident workflow on the owner's connected Dynatrace+GitLab.

        Marks the mission failed (no generic fallback) when the owner has not
        configured Dynatrace. Never raises — a webhook must not crash.

        Args:
            mission_id: Mission to run the workflow for.
            problem_id: Dynatrace problem id that triggered the mission.
            owner_id: User id of the mission owner (for effective config lookup).
        """
        log = self._log.bind(mission_id=mission_id, problem_id=problem_id)
        eff = await resolve_effective_settings(owner_id)
        if not (eff.dynatrace_environment_url and eff.dynatrace_api_token):
            await self._fail_incident(mission_id, "dynatrace_not_configured",
                                      "Connect a Dynatrace environment in settings to run incident autopilot.")
            return
        try:
            dynatrace = DynatraceClient(
                mission_id=mission_id, failure_engine=self._failure_engine,
                server_url=f"{eff.dynatrace_environment_url.rstrip('/')}/mcp", token=eff.dynatrace_api_token,
            )
            gitlab = GitLabClient(
                mission_id=mission_id, failure_engine=self._failure_engine,
                server_url=(f"{eff.gitlab_url.rstrip('/')}/api/v4" if eff.gitlab_url else None),
                token=eff.gitlab_token,
            )
            await dynatrace.connect()
            await gitlab.connect()
            from modules.incident_autopilot.workflow import IncidentAutopilotWorkflow  # lazy: avoids circular import
            workflow = IncidentAutopilotWorkflow(dynatrace, gitlab, project_id=eff.gitlab_project_id)
            task = asyncio.create_task(
                self._run_incident_workflow(workflow, mission_id, problem_id), name=f"incident-{mission_id}"
            )
            self._incident_workflows[mission_id] = (workflow, dynatrace, gitlab, task)
            log.info("incident_workflow_launched")
        except Exception as exc:  # noqa: BLE001 — a webhook must never crash
            log.error("incident_launch_failed", error=str(exc), exc_info=True)
            await self._fail_incident(mission_id, "launch_error", str(exc))

    async def _run_incident_workflow(
        self, workflow: Any, mission_id: str, problem_id: str
    ) -> None:
        """Drive the workflow; mark the mission failed if it raises.

        Args:
            workflow: IncidentAutopilotWorkflow instance to drive.
            mission_id: Owning mission identifier.
            problem_id: Dynatrace problem id passed to the workflow.
        """
        try:
            await workflow.run(problem_id, mission_id)
        except Exception as exc:  # noqa: BLE001 — never let the incident task crash silently
            self._log.error("incident_workflow_crashed", mission_id=mission_id, error=str(exc), exc_info=True)
            await self._fail_incident(mission_id, "workflow_error", str(exc))

    async def _fail_incident(self, mission_id: str, reason: str, message: str) -> None:
        """Mark an incident mission failed and emit a reason event.

        Args:
            mission_id: Mission to mark as failed.
            reason: Short machine-readable failure reason key.
            message: Human-readable explanation.
        """
        await db.update_mission_status(mission_id, "failed")
        await db.emit_event(mission_id, EVENT_INCIDENT_FAILED, {"reason": reason, "message": message}, SOURCE_ORCH)
        self._log.warning("incident_failed", mission_id=mission_id, reason=reason)

    # ----------------------------------------------------------------- #
    # The loop
    # ----------------------------------------------------------------- #
    async def _run_loop(self, mission_id: str) -> None:
        """Top-level mission loop: plan, then cycle until terminal or cancelled."""
        log = self._log.bind(mission_id=mission_id)
        try:
            await self._initial_plan(mission_id)
            while True:
                mission = await db.get_mission(mission_id)
                if mission is None or mission.status in TERMINAL_STATUSES:
                    break
                try:
                    await self._execute_cycle(mission)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — a cycle must never kill the loop
                    log.error("cycle_failed", error=str(exc), exc_info=True)
                await asyncio.sleep(settings.orchestration_interval_seconds)
        except asyncio.CancelledError:
            log.info("mission_loop_cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — never crash silently
            log.error("mission_loop_crashed", error=str(exc), exc_info=True)
        finally:
            self._active.pop(mission_id, None)
            log.info("mission_loop_exited")

    async def _execute_cycle(self, mission: Mission) -> None:
        """Run one full orchestration iteration for a mission."""
        mission_id = mission.mission_id
        self._cycles[mission_id] = self._cycles.get(mission_id, 0) + 1
        injections = self._active_failures(mission)
        state = await db.get_mission_state(mission_id)
        if state is None:
            return
        risk = await self._score_risk(mission_id, state, injections)
        if risk > settings.risk_threshold:
            await self._replan(mission)
            state = await db.get_mission_state(mission_id) or state
        ready = MissionGraph(state.tasks).get_ready_tasks()
        await self._run_ready_tasks(mission_id, ready)
        state = await db.get_mission_state(mission_id) or state
        await self._handle_retries(mission_id, state.tasks)
        state = await db.get_mission_state(mission_id) or state
        await self._audit(mission_id, state)
        await self._snapshot(mission_id, state, injections)
        await self._check_resolution(mission, state)

    # ----------------------------------------------------------------- #
    # Cycle steps
    # ----------------------------------------------------------------- #
    async def _score_risk(self, mission_id: str, state: MissionState, injections: list) -> float:
        """Run the RiskAgent and return the overall risk score."""
        agent = self.risk_agent_factory(mission_id)
        result = await agent.run({"state": state, "failure_injections": injections})
        return float(result.output.get("risk", {}).get("overall", 0.0))

    async def _run_ready_tasks(self, mission_id: str, ready: list[Task]) -> None:
        """Mark ready tasks in-progress and run them concurrently."""
        if not ready:
            return
        agent = await self.execution_agent_factory(mission_id)
        await asyncio.gather(*(self._run_one(mission_id, agent, task) for task in ready))

    async def _run_one(self, mission_id: str, agent: ExecutionAgent, task: Task) -> None:
        """Transition one task to in-progress, emit TASK_STARTED, and execute it."""
        await db.update_task_status(task.task_id, "in_progress")
        await db.emit_event(
            mission_id, EVENT_TASK_STARTED, {"task_id": task.task_id}, SOURCE_ORCH, task_id=task.task_id
        )
        await agent.run({"task": task, "tool_args": task.tool_args})

    async def _handle_retries(self, mission_id: str, tasks: list[Task]) -> None:
        """Re-queue failed tasks under the retry limit, emitting TASK_RETRYING.

        Implements the retry policy from ARCHITECTURE.md section 1: a failed task
        with ``retry_count`` below ``max_task_retries`` is reset to ``pending``
        (so the next cycle re-runs it) with its retry count incremented. Once the
        limit is reached it stays ``failed``.
        """
        for task in tasks:
            if task.status != "failed" or task.retry_count >= settings.max_task_retries:
                continue
            await db.update_task_status(task.task_id, "pending", retry_count=task.retry_count + 1)
            await db.emit_event(
                mission_id,
                EVENT_TASK_RETRYING,
                {"task_id": task.task_id, "retry_count": task.retry_count + 1},
                SOURCE_ORCH,
                task_id=task.task_id,
            )

    async def _audit(self, mission_id: str, state: MissionState) -> None:
        """Run the AuditorAgent over the current state."""
        agent = self.auditor_agent_factory(mission_id)
        await agent.run({"state": state})

    async def _snapshot(self, mission_id: str, state: MissionState, injections: list) -> None:
        """Persist a snapshot and emit SNAPSHOT_TAKEN."""
        cycle = self._cycles.get(mission_id, 0)
        active = [self._inj_dict(i) for i in injections]
        snapshot = await db.create_snapshot(state, cycle, active)
        await db.emit_event(
            mission_id, EVENT_SNAPSHOT_TAKEN, {"cycle": cycle, "snapshot_id": snapshot.snapshot_id}, SOURCE_ORCH
        )

    async def _check_resolution(self, mission: Mission, state: MissionState) -> None:
        """Resolve or fail the mission once all tasks reach a terminal status."""
        graph = MissionGraph(state.tasks)
        if not graph.is_complete():
            return
        all_completed = all(t.status == "completed" for t in state.tasks)
        any_completed = any(t.status == "completed" for t in state.tasks)
        # GENERAL research is resilient: synthesize a hand-off from whatever
        # completed (a single stray failed task shouldn't suppress the result),
        # and resolve the mission once it has produced a recommendation.
        if mission.mission_type == "GENERAL" and any_completed:
            from modules.research_concierge.synthesis import synthesize_and_handoff  # lazy: avoids circular import
            await synthesize_and_handoff(mission.mission_id)
            await self._set_status(mission, "resolved")
            return
        await self._set_status(mission, "resolved" if all_completed else "failed")

    # ----------------------------------------------------------------- #
    # Planning + replanning
    # ----------------------------------------------------------------- #
    async def _initial_plan(self, mission_id: str) -> None:
        """Plan the mission's first task set via MissionPlanner if none exist."""
        mission = await db.get_mission(mission_id)
        if mission is None or mission.task_ids:
            return
        await self._set_status(mission, "planning")
        try:
            tasks = await self._planner.plan(mission_id, mission.goal, mission.context)
        except PlanningFailedError as exc:
            self._log.error(
                "initial_planning_failed",
                mission_id=mission_id,
                error=str(exc),
                cause=getattr(exc, "context", None),
            )
            await self._set_status(mission, "failed")
            return
        await db.add_tasks(tasks)
        await self._set_status(mission, "executing")

    async def _replan(self, mission: Mission) -> None:
        """Generate a revised plan via the PlannerAgent and append it.

        Args:
            mission: Mission whose risk exceeded the threshold.
        """
        mission_id = mission.mission_id
        count = self._replans.get(mission_id, 0)
        if count >= MAX_REPLANS:
            self._log.warning("replan_limit_reached", mission_id=mission_id)
            await self._set_status(mission, "failed")
            return
        self._replans[mission_id] = count + 1
        await self._set_status(mission, "replanning")
        await db.emit_event(mission_id, EVENT_REPLAN_TRIGGERED, {"replan_number": count + 1}, SOURCE_ORCH)
        try:
            result = await self.planner_agent_factory(mission_id).run(
                {"goal": mission.goal, "context": mission.context}
            )
        except PlanningFailedError as exc:
            self._log.error("replanning_failed", mission_id=mission_id, error=str(exc))
            await self._set_status(mission, "failed")
            return
        await db.add_tasks([Task.model_validate(t) for t in result.output["tasks"]])
        await self._set_status(mission, "executing")

    # ----------------------------------------------------------------- #
    # Helpers
    # ----------------------------------------------------------------- #
    async def _set_status(self, mission: Mission, status: str) -> None:
        """Transition mission status and emit MISSION_STATUS_CHANGED."""
        if mission.status == status:
            return
        previous = mission.status
        updated = await db.update_mission_status(mission.mission_id, status)
        mission.status = updated.status
        await db.emit_event(
            mission.mission_id, EVENT_MISSION_STATUS_CHANGED, {"from": previous, "to": status}, SOURCE_ORCH
        )

    def _active_failures(self, mission: Mission) -> list:
        """Return active failure injections as dicts (honoring the flag).

        The RiskAgent is the only consumer; it reads ``type``/``severity`` keys.
        """
        if not settings.failure_engine_enabled or self._failure_engine is None:
            return []
        return [scenario.as_dict() for scenario in self._failure_engine.get_active_failures()]

    @staticmethod
    def _inj_dict(injection: Any) -> dict:
        """Normalize an injection (model or dict) to a plain dict for storage."""
        if hasattr(injection, "as_dict"):
            return injection.as_dict()
        if hasattr(injection, "model_dump"):
            return injection.model_dump()
        return dict(injection)

    async def _default_execution_agent(self, mission_id: str) -> ExecutionAgent:
        """Build an ExecutionAgent whose MCP clients use the mission owner's config.

        The owner's stored integration settings (Tavily/GitLab/Dynatrace) override
        the global env defaults; an unconfigured owner falls back to global.
        """
        mission = await db.get_mission(mission_id)
        eff = await resolve_effective_settings(mission.owner_id if mission else None)
        dynatrace = DynatraceClient(
            mission_id=mission_id, failure_engine=self._failure_engine,
            server_url=(f"{eff.dynatrace_environment_url.rstrip('/')}/mcp" if eff.dynatrace_environment_url else None),
            token=eff.dynatrace_api_token,
        )
        gitlab = GitLabClient(
            mission_id=mission_id, failure_engine=self._failure_engine,
            server_url=(f"{eff.gitlab_url.rstrip('/')}/api/v4" if eff.gitlab_url else None),
            token=eff.gitlab_token,
        )
        web_search = WebSearchClient(
            mission_id=mission_id, failure_engine=self._failure_engine,
            api_key=eff.tavily_api_key, api_url=eff.tavily_api_url,
        )
        return ExecutionAgent(mission_id, dynatrace, gitlab, web_search=web_search)
