"""Scripted demo timeline for NERVE (ARCHITECTURE.md section 6).

Runs the full 3-minute Incident Autopilot arc against a **seeded** scenario
rather than live MCP connections, so it works without real Dynatrace/GitLab
endpoints or Gemini billing. The seeded MCP sessions still flow through the real
``BaseMCPClient`` (so audit events and failure injections apply) and the real
``IncidentAutopilotWorkflow`` / ``IncidentResolver``.

Timeline: detection + issue + pending rollback (t=0) → contradictory metrics
injected (t=45) → risk spike + replan signal (t=60) → metrics cleared (t=90) →
approval requested (t=120). When a human approves the rollback (dashboard or
``POST /actions/{id}/approve``), the seeded service recovers and the resolver
closes the incident.

Active only when ``DEMO_MODE`` and ``FAILURE_ENGINE_ENABLED`` are both set.
``time_scale`` compresses the timeline (1.0 = real seconds).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import structlog

from agents.execution_agent import ExecutionAgent
from agents.risk_agent import RiskAgent
from config import settings
from exceptions import MCPError
from failure_engine.injector import FailureEngine, FailureScenario, FailureType
from mcp_tools.dynatrace import DynatraceClient
from mcp_tools.gitlab import (
    GitLabClient,
    GitLabDeployment,
    TOOL_CLOSE_ISSUE,
    TOOL_CREATE_ISSUE,
    TOOL_CREATE_MR,
    TOOL_GET_COMMIT,
    TOOL_GET_COMMIT_DIFF,
    TOOL_LIST_DEPLOYMENTS,
    TOOL_TRIGGER_PIPELINE,
)
from modules.incident_autopilot.resolver import IncidentResolver
from modules.incident_autopilot.workflow import CorrelationResult, IncidentAutopilotWorkflow
from state import database as db

# GITLAB_TOKEN / GITLAB_PROJECT_ID values that mean "not really configured".
_PLACEHOLDER_TOKENS = {"", "demo", "test-token", "changeme", "REPLACE_WITH_GITLAB_TOKEN"}
_PLACEHOLDER_PROJECTS = {"", "0", "123"}


def _gitlab_configured() -> bool:
    """Return True when real GitLab credentials + a real project are set.

    Used to decide whether the demo creates REAL GitLab artifacts or falls back
    to a seeded GitLab client (so the demo still runs fully offline).
    """
    return (
        settings.gitlab_token not in _PLACEHOLDER_TOKENS
        and settings.gitlab_project_id not in _PLACEHOLDER_PROJECTS
    )

log = structlog.get_logger()

SOURCE_ORCH = "orchestrator"
SOURCE_FAILURE_ENGINE = "failure_engine"
METRICS_TOOL_TARGET = "get_metrics"
DEMO_PROBLEM_ID = "DEMO-PROBLEM-1"

EVENT_DEMO_STARTED = "DEMO_STARTED"
EVENT_REPLAN_TRIGGERED = "REPLAN_TRIGGERED"
EVENT_MISSION_STATUS_CHANGED = "MISSION_STATUS_CHANGED"
EVENT_DEMO_APPROVAL_REQUESTED = "DEMO_APPROVAL_REQUESTED"

# Demo timeline offsets in seconds from t=0 (ARCHITECTURE.md section 6).
T_INJECT_CONTRADICTORY = 45
T_UNCERTAINTY = 60
T_CLEAR_CONTRADICTORY = 90
T_APPROVAL_REQUESTED = 120

# Seeded service behavior.
SEED_BASELINE_ERROR_RATE = 0.02
SEED_INCIDENT_ERROR_RATE = 0.34  # ~340% spike over baseline
SEED_GOAL = "Investigate and resolve elevated error rate on checkout service"
RESOLVER_POLL_SECONDS = 15.0
RESOLVER_MAX_CHECKS = 30


@dataclass
class _DemoState:
    """Mutable demo state shared across seeded MCP calls."""

    rolled_back: bool = False


class SeededMCPSession:
    """A fake MCP session returning seeded demo data.

    Routes by tool name. ``get_metrics`` returns the healthy baseline for
    pre-incident windows, the elevated rate during the incident, and recovers
    once a rollback pipeline has been triggered. Triggering the rollback flips
    the shared demo state so the service "recovers".
    """

    def __init__(self, demo_state: _DemoState, incident_start: datetime) -> None:
        self._state = demo_state
        self._incident_start = incident_start

    async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
        """Return seeded structured content for a named tool."""
        handler = getattr(self, f"_tool_{name}", None)
        data = handler(arguments) if handler is not None else {}
        return SimpleNamespace(structuredContent=data, content=[])

    def _tool_get_problem_details(self, _args: dict) -> dict:
        return {
            "problemId": DEMO_PROBLEM_ID,
            "title": "Elevated error rate on checkout",
            "severityLevel": "AVAILABILITY",
            "status": "OPEN",
            "impactedEntities": [{"name": "checkout"}],
            "rootCause": "Deployment of payment_processor.py",
            "timeline": [{"timestamp": self._incident_start.isoformat(), "description": "error rate +340%"}],
            "startTime": int(self._incident_start.timestamp() * 1000),
        }

    def _tool_get_metrics(self, args: dict) -> dict:
        to_raw = args.get("to")
        window_end = datetime.fromisoformat(to_raw) if to_raw else datetime.utcnow()
        if window_end <= self._incident_start:  # pre-incident baseline window
            rate = SEED_BASELINE_ERROR_RATE
        else:
            rate = SEED_BASELINE_ERROR_RATE if self._state.rolled_back else SEED_INCIDENT_ERROR_RATE
        return {"error_rate": rate, "latency_ms": 120.0, "throughput": 950.0}


class _DemoGitLabClient(GitLabClient):
    """Real GitLab client used in DEMO_MODE when GitLab is configured.

    Behaves exactly like the real client (real issues, deployments, pipelines),
    but flips the shared demo state when a rollback pipeline is triggered so the
    seeded Dynatrace metrics "recover" after the human-approved rollback.
    """

    def __init__(self, demo_state: "_DemoState", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._demo_state = demo_state

    async def trigger_pipeline(self, project_id: str, ref: str, variables: dict):  # type: ignore[override]
        """Trigger the real rollback pipeline, but never let the demo stall.

        If the configured project cannot actually trigger a pipeline (e.g. no CI
        config → GitLab 400), the demo still recovers: we log the failure, flip
        the seeded service to healthy, and return a synthetic pipeline so the
        approved rollback completes and the incident resolves.
        """
        from mcp_tools.gitlab import GitLabPipeline

        try:
            result = await super().trigger_pipeline(project_id, ref, variables)
        except MCPError as exc:
            log.warning("demo_real_pipeline_failed_recovering_anyway", ref=ref, error=str(exc))
            result = GitLabPipeline(id=0, status="created", ref=ref, sha=None)
        self._demo_state.rolled_back = True
        return result


class _SeededGitLabClient(GitLabClient):
    """Offline GitLab client returning canned REST payloads (no network).

    Used when GitLab is not configured so the demo still runs fully offline.
    Calls flow through the wrapped ``call_tool`` path (audit events still emit);
    only the raw transport is replaced. Triggering a rollback recovers metrics.
    """

    def __init__(self, demo_state: "_DemoState", incident_start: datetime, **kwargs: Any) -> None:
        super().__init__(server_url="seeded://gitlab", **kwargs)
        self._demo_state = demo_state
        self._incident_start = incident_start

    async def connect(self) -> None:  # no real connection needed
        self._session = self

    async def disconnect(self) -> None:
        self._session = None

    async def _raw_call(self, tool_name: str, arguments: dict) -> Any:  # type: ignore[override]
        deployed_at = (self._incident_start - timedelta(minutes=31)).isoformat()
        if tool_name == TOOL_LIST_DEPLOYMENTS:
            return [{"id": 42, "status": "success", "ref": "main", "sha": "a1b2c3d4",
                     "environment": {"name": "production"}, "created_at": deployed_at}]
        if tool_name == TOOL_GET_COMMIT:
            return {"id": "a1b2c3d4", "title": "Refactor payment_processor.py",
                    "message": "Refactor payment_processor.py", "author_name": "demo",
                    "created_at": deployed_at, "web_url": "https://gitlab.example/checkout/-/commit/a1b2c3d4"}
        if tool_name == TOOL_GET_COMMIT_DIFF:
            return [{"new_path": "payment_processor.py", "old_path": "payment_processor.py"}]
        if tool_name == TOOL_CREATE_ISSUE:
            return {"id": 900, "iid": 7, "title": arguments.get("title"), "state": "opened",
                    "web_url": "https://gitlab.example/checkout/-/issues/7", "labels": arguments.get("labels") or []}
        if tool_name == TOOL_CREATE_MR:
            return {"id": 300, "iid": 3, "title": arguments.get("title"), "state": "opened",
                    "source_branch": arguments.get("source_branch"), "target_branch": "main",
                    "web_url": "https://gitlab.example/checkout/-/merge_requests/3"}
        if tool_name == TOOL_TRIGGER_PIPELINE:
            self._demo_state.rolled_back = True  # the service now recovers
            return {"id": 555, "status": "created", "ref": arguments.get("ref", "main"), "sha": "a1b2c3d4"}
        if tool_name == TOOL_CLOSE_ISSUE:
            return {"id": 900, "iid": 7, "state": "closed"}
        return {}


def _make_seeded_deployment(incident_start: datetime) -> GitLabDeployment:
    """Build the canonical seeded deployment for the demo scenario.

    The deployment is always 31 minutes before the incident start, which places
    it firmly inside the 24-hour lookback window while providing a clear temporal
    correlation with the error-rate spike.

    Args:
        incident_start: The seeded incident start time (naive UTC).

    Returns:
        A :class:`~mcp_tools.gitlab.GitLabDeployment` for deployment #42.
    """
    deployed_at = incident_start - timedelta(minutes=31)
    return GitLabDeployment(
        id=42,
        status="success",
        ref="main",
        sha="a1b2c3d4",
        environment="production",
        created_at=deployed_at,
    )


def _make_seeded_reason(seeded_deployment: GitLabDeployment):
    """Return a correlation function that always returns the seeded deployment.

    The returned function ignores the ``deployments`` argument supplied by the
    workflow.  This is intentional: when the demo runs against a *real* GitLab
    project the project almost certainly has no deployments in the last 24 hours
    that match the seeded checkout scenario.  If the workflow passes an empty
    list (or real unrelated deployments) to the reason function, the plain
    ``deployments[0] if deployments else None`` pattern would yield
    ``correlated_deployment=None``, which causes
    :meth:`~modules.incident_autopilot.workflow.IncidentAutopilotWorkflow._enforce_no_deployment_rule`
    to downgrade the recommendation from ``"rollback"`` to ``"investigate"``
    — preventing the pending rollback action from ever being created.

    By closing over the pre-built :class:`~mcp_tools.gitlab.GitLabDeployment` we
    guarantee that the seeded correlation result is always correct regardless of
    what the (real or seeded) GitLab client returned.

    Args:
        seeded_deployment: The canonical seeded deployment for this demo run.

    Returns:
        An async correlation function compatible with :data:`~modules.incident_autopilot.workflow.ReasonFn`.
    """
    async def _seeded_reason(problem: Any, metrics: Any, deployments: list) -> CorrelationResult:
        return CorrelationResult(
            correlated_deployment=seeded_deployment,
            confidence=0.92,
            reasoning=(
                "Error rate spiked ~31 minutes after deployment 42 (payment_processor.py) "
                "to production. Strong temporal correlation; recommend rollback."
            ),
            recommendation="rollback",
        )

    return _seeded_reason


async def build_seeded_clients(
    engine: FailureEngine, mission_id: str
) -> tuple[DynatraceClient, GitLabClient, _DemoState, GitLabDeployment]:
    """Build the demo's Dynatrace (always seeded) and GitLab clients.

    Dynatrace is always seeded (no live Dynatrace in the demo). GitLab is REAL
    when configured (creates real issues/MRs/pipelines in the demo project) and
    seeded otherwise, so the demo still runs fully offline.

    The canonical seeded deployment is also returned so callers can pass it to
    :func:`_make_seeded_reason`, ensuring the correlation result always references
    the seeded deployment regardless of what the (real or seeded) GitLab client
    actually returns from :meth:`~mcp_tools.gitlab.GitLabClient.list_recent_deployments`.

    Args:
        engine: Failure engine whose injections apply to the wrapped calls.
        mission_id: Mission to attribute MCP audit events to.

    Returns:
        A 4-tuple of (dynatrace_client, gitlab_client, demo_state, seeded_deployment).
    """
    demo_state = _DemoState()
    incident_start = datetime.utcnow() - timedelta(minutes=30)
    seeded_deployment = _make_seeded_deployment(incident_start)
    dynatrace = DynatraceClient(mission_id=mission_id, failure_engine=engine, server_url="seeded://dynatrace")
    dynatrace._session = SeededMCPSession(demo_state, incident_start)

    if _gitlab_configured():
        gitlab: GitLabClient = _DemoGitLabClient(demo_state, mission_id=mission_id, failure_engine=engine)
        await gitlab.connect()
        log.info("demo_using_real_gitlab", project_id=settings.gitlab_project_id)
    else:
        gitlab = _SeededGitLabClient(demo_state, incident_start, mission_id=mission_id, failure_engine=engine)
        log.info("demo_using_seeded_gitlab")
    return dynatrace, gitlab, demo_state, seeded_deployment


class DemoScenario:
    """Runs the pre-scripted demo incident end-to-end with seeded data."""

    def __init__(self, *, time_scale: float = 1.0, owner_id: str | None = None) -> None:
        """Initialize the demo driver.

        Args:
            time_scale: Multiplier on the timeline (e.g. 0.001 for fast tests).
            owner_id: User id to set as the mission owner (the clicking user).
        """
        self._time_scale = time_scale
        self._owner_id = owner_id
        self._clock = 0.0
        self._mission_id: str | None = None
        self._workflow: IncidentAutopilotWorkflow | None = None
        self._engine: FailureEngine | None = None
        self._log = structlog.get_logger().bind(component="demo_scenario")

    @staticmethod
    def _seed_context() -> dict:
        """Return the seeded incident context (checkout, 340% spike, bad deploy)."""
        return {
            "problem_id": DEMO_PROBLEM_ID,
            "service": "checkout",
            "error_spike_pct": 340,
            "correlated_deployment": {"file": "payment_processor.py", "minutes_prior": 31},
        }

    async def run(self, orchestrator: Any) -> None:
        """Drive the demo end-to-end against the given orchestrator.

        Args:
            orchestrator: A NERVEOrchestrator (holds the shared failure engine and
                the execution-agent factory used by the approval route).
        """
        if not (settings.demo_mode and settings.failure_engine_enabled):
            self._log.warning(
                "demo_scenario_skipped", demo_mode=settings.demo_mode, failures=settings.failure_engine_enabled
            )
            return
        mission_id = self._mission_id or await self._seed_mission()
        self._engine = orchestrator._failure_engine or FailureEngine()
        self._engine.mission_id = mission_id
        orchestrator._failure_engine = self._engine
        dynatrace, gitlab, _, seeded_deployment = await build_seeded_clients(self._engine, mission_id)
        # The demo overrides the shared orchestrator's factory so the approved
        # rollback runs against the seeded GitLab. Include a web_search client too,
        # otherwise GENERAL research missions launched on this same instance after a
        # demo would get an agent without web_search and fail with tool_unavailable.
        from mcp_tools.web_search import WebSearchClient

        engine = self._engine
        orchestrator.execution_agent_factory = lambda mid: ExecutionAgent(
            mid, dynatrace, gitlab, web_search=WebSearchClient(mission_id=mid, failure_engine=engine)
        )
        await self._launch_workflow(orchestrator, dynatrace, gitlab, mission_id, seeded_deployment)
        await self._drive_timeline(mission_id)

    async def prepare(self) -> str:
        """Create the seeded mission up front and return its id.

        Lets the caller (e.g. the demo route) get the mission id synchronously
        and hand it to the dashboard, while :meth:`run` drives the rest of the
        timeline in the background.

        Returns:
            The seeded mission's id.
        """
        self._mission_id = await self._seed_mission()
        return self._mission_id

    async def _seed_mission(self) -> str:
        """Create the seeded mission and move it into the executing state."""
        mission = await db.create_mission(SEED_GOAL, "INCIDENT_RESPONSE", self._seed_context(), owner_id=self._owner_id)
        await db.emit_event(mission.mission_id, EVENT_DEMO_STARTED, self._seed_context(), SOURCE_FAILURE_ENGINE)
        await db.emit_event(mission.mission_id, "MISSION_CREATED", {"goal": SEED_GOAL}, SOURCE_ORCH)
        await self._set_status(mission.mission_id, "pending", "planning")
        await self._set_status(mission.mission_id, "planning", "executing")
        return mission.mission_id

    async def _launch_workflow(
        self,
        orchestrator: Any,
        dynatrace: DynatraceClient,
        gitlab: GitLabClient,
        mission_id: str,
        seeded_deployment: GitLabDeployment,
    ) -> None:
        """Run the incident workflow (detection → issue → pending rollback).

        Args:
            orchestrator: NERVE orchestrator used to store the workflow reference.
            dynatrace: Dynatrace MCP client (always seeded in demo).
            gitlab: GitLab client (seeded or real depending on configuration).
            mission_id: Owning mission identifier.
            seeded_deployment: The canonical seeded deployment; passed to the
                reason function so it always returns a rollback recommendation
                even when the real GitLab project has no recent deployments.
        """
        resolver = IncidentResolver(
            dynatrace,
            gitlab,
            project_id=settings.gitlab_project_id,
            poll_interval_seconds=RESOLVER_POLL_SECONDS * self._time_scale,
            required_consecutive=3,
            max_checks=RESOLVER_MAX_CHECKS,
        )
        self._workflow = IncidentAutopilotWorkflow(
            dynatrace,
            gitlab,
            reason=_make_seeded_reason(seeded_deployment),
            resolver=resolver,
            project_id=settings.gitlab_project_id,
        )
        # Keep the workflow (and its background resolver task) alive past run().
        orchestrator._demo_workflow = self._workflow
        await self._workflow.run(DEMO_PROBLEM_ID, mission_id)

    async def _drive_timeline(self, mission_id: str) -> None:
        """Inject/clear failures and emit the risk-spike + approval milestones."""
        await self._wait_until(T_INJECT_CONTRADICTORY)
        await self._engine.inject(
            FailureScenario(
                failure_type=FailureType.CONTRADICTORY_METRICS,
                target=METRICS_TOOL_TARGET,
                severity=0.9,
                duration_seconds=T_CLEAR_CONTRADICTORY - T_INJECT_CONTRADICTORY,
            )
        )
        # Belief contradiction: conflicting signals make root-cause ambiguous.
        try:
            await db.write_belief(
                mission_id, "root_cause", "Root cause",
                "#4827 vs #4830 ?",
                confidence=0.41, op="contradict",
            )
        except Exception as exc:  # noqa: BLE001 — belief write is best-effort during demo
            self._log.warning("demo_belief_write_failed", key="root_cause", op="contradict", error=str(exc))
        await self._wait_until(T_UNCERTAINTY)
        await self._spike_risk(mission_id)
        await self._wait_until(T_CLEAR_CONTRADICTORY)
        await self._engine.clear(FailureType.CONTRADICTORY_METRICS)
        # Belief re-confirmed once contradictory metrics are cleared.
        try:
            await db.write_belief(
                mission_id, "root_cause", "Root cause",
                "deploy #4827",
                confidence=0.9, op="confirm",
            )
        except Exception as exc:  # noqa: BLE001 — belief write is best-effort during demo
            self._log.warning("demo_belief_write_failed", key="root_cause", op="confirm", error=str(exc))
        await self._wait_until(T_APPROVAL_REQUESTED)
        await db.emit_event(mission_id, EVENT_DEMO_APPROVAL_REQUESTED, {"action": "gitlab_rollback"}, SOURCE_FAILURE_ENGINE)

    async def _spike_risk(self, mission_id: str) -> None:
        """Score risk against the active failure and emit a replan signal."""
        state = await db.get_mission_state(mission_id)
        if state is None:
            return
        injections = [s.as_dict() for s in self._engine.get_active_failures()]
        result = await RiskAgent(mission_id).run({"state": state, "failure_injections": injections})
        overall = result.output.get("risk", {}).get("overall", 0.0)
        await db.emit_event(
            mission_id, EVENT_REPLAN_TRIGGERED, {"reason": "contradictory_metrics", "risk": overall}, SOURCE_ORCH
        )

    async def _set_status(self, mission_id: str, previous: str, new: str) -> None:
        """Transition mission status and emit MISSION_STATUS_CHANGED."""
        await db.update_mission_status(mission_id, new)
        await db.emit_event(mission_id, EVENT_MISSION_STATUS_CHANGED, {"from": previous, "to": new}, SOURCE_ORCH)

    async def _wait_until(self, offset_seconds: float) -> None:
        """Sleep until the given timeline offset (scaled by ``time_scale``)."""
        delay = max(0.0, offset_seconds - self._clock) * self._time_scale
        self._clock = offset_seconds
        if delay > 0:
            await asyncio.sleep(delay)
