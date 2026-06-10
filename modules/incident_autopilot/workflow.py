"""IncidentAutopilotWorkflow: end-to-end incident response orchestration.

Implements the Incident Autopilot flow from CLAUDE.md: detect → assemble context
→ reason (Gemini) → file issue → propose human-approved rollback → monitor for
resolution. Every step emits an audit event. Rollback is never executed here —
it is only ever created as a pending Action awaiting human approval.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from config import settings
from mcp_tools.dynatrace import DynatraceProblemDetail, ServiceMetrics
from mcp_tools.gitlab import GitLabDeployment, GitLabIssue
from memory.incident_memory import IncidentMemory, MemoryEntry
from modules.incident_autopilot.resolver import IncidentResolver
from modules.incident_autopilot.templates import IncidentTemplates
from notifications.telegram_bot import telegram_notifier
from state import database as db

SOURCE_ORCH = "orchestrator"
EVENT_INCIDENT_DETECTED = "INCIDENT_DETECTED"
EVENT_CONTEXT_ASSEMBLED = "CONTEXT_ASSEMBLED"
EVENT_REASONING_COMPLETE = "REASONING_COMPLETE"
EVENT_ACTION_CREATED = "ACTION_CREATED"
EVENT_ACTION_EXECUTED = "ACTION_EXECUTED"
EVENT_RESOLUTION_MONITORING_STARTED = "RESOLUTION_MONITORING_STARTED"
EVENT_MEMORY_RETRIEVED = "MEMORY_RETRIEVED"

ACTION_GITLAB_ISSUE = "gitlab_issue"
ACTION_GITLAB_ROLLBACK = "gitlab_rollback"
STATUS_EXECUTED = "executed"

Recommendation = Literal["rollback", "investigate", "monitor"]
RECOMMENDATIONS = ("rollback", "investigate", "monitor")

DEPLOYMENT_LOOKBACK_HOURS = 24
DEFAULT_INCIDENT_WINDOW_MINUTES = 60
BASELINE_WINDOW_HOURS = 1

@dataclass
class GenerationResult:
    """Raw model output: the text plus any Google Search grounding citations."""

    text: str
    grounding_sources: list[str] = field(default_factory=list)


ReasonFn = Callable[
    [DynatraceProblemDetail, ServiceMetrics | None, list[GitLabDeployment]],
    Awaitable["CorrelationResult"],
]
GenerateFn = Callable[[str], Awaitable[GenerationResult]]


class CorrelationResult(BaseModel):
    """Outcome of correlating the anomaly with deployment history.

    (The task refers to this as ``ReasoningResult``; ``ReasoningResult`` is
    exported below as an alias of this model.)
    """

    model_config = ConfigDict(extra="ignore")

    correlated_deployment: GitLabDeployment | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning: str = ""
    recommendation: Recommendation = "investigate"
    grounding_sources: list[str] = Field(default_factory=list)
    external_context: str = ""


#: The task's name for the reasoning output model.
ReasoningResult = CorrelationResult


class IncidentAutopilotWorkflow:
    """Runs one incident from detection through resolution monitoring."""

    def __init__(
        self,
        dynatrace: Any,
        gitlab: Any,
        *,
        reason: ReasonFn | None = None,
        generate: GenerateFn | None = None,
        resolver: IncidentResolver | None = None,
        project_id: str | None = None,
        model_factory: Callable[[], Any] | None = None,
        memory: IncidentMemory | None = None,
    ) -> None:
        """Initialize the workflow.

        Args:
            dynatrace: Dynatrace MCP client (connected or mocked).
            gitlab: GitLab MCP client (connected or mocked).
            reason: Correlation function; defaults to a Gemini-backed reasoner.
            generate: Low-level text generator used by the default reasoner.
            resolver: Resolution monitor (defaults to a new IncidentResolver).
            project_id: GitLab project id (defaults to settings).
            model_factory: Builds the Gemini model; injected in tests to mock
                the Vertex AI client. ``None`` builds the real grounded model.
        """
        self._dynatrace = dynatrace
        self._gitlab = gitlab
        self._project_id = project_id or settings.gitlab_project_id
        self._reason: ReasonFn = reason or self._default_reason
        self._generate: GenerateFn = generate or self._default_generate
        self._model_factory = model_factory
        self._resolver = resolver or IncidentResolver(dynatrace, gitlab, project_id=self._project_id)
        self._memory = memory or IncidentMemory()
        self._memories: list[MemoryEntry] = []
        self._run_started_at: datetime | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._log = structlog.get_logger().bind(component="incident_autopilot")

    async def run(self, problem_id: str, mission_id: str) -> None:
        """Execute the full incident response flow for one problem.

        Args:
            problem_id: Dynatrace problem id that triggered the mission.
            mission_id: Mission this incident belongs to.
        """
        log = self._log.bind(mission_id=mission_id, problem_id=problem_id)
        self._run_started_at = datetime.utcnow()
        problem = await self._dynatrace.get_problem_details(problem_id)  # Step 1
        await self._emit(mission_id, EVENT_INCIDENT_DETECTED, {"problem_id": problem_id, "title": problem.title})
        service_id = problem.impacted_services[0] if problem.impacted_services else None

        metrics, deployments = await self._assemble_context(problem, service_id)  # Step 2
        await self._emit(mission_id, EVENT_CONTEXT_ASSEMBLED, {"deployments": len(deployments), "service_id": service_id})

        # Record the initial metric spike so the sparkline starts high.
        if metrics is not None and metrics.error_rate is not None:
            await self._safe_record_metric(
                mission_id, "checkout · error rate",
                round(metrics.error_rate * 100, 2), unit="%",
            )

        self._memories = await self._memory.retrieve_similar(problem.title or problem.problem_id)  # Step 2.5
        if self._memories:
            await self._emit(mission_id, EVENT_MEMORY_RETRIEVED, {"count": len(self._memories)})
        correlation = self._enforce_no_deployment_rule(await self._reason(problem, metrics, deployments))  # Step 3
        await self._emit(mission_id, EVENT_REASONING_COMPLETE, self._reasoning_payload(correlation))

        await self._emit_workflow_beliefs(mission_id, correlation, service_id, metrics)  # Milestone beliefs

        issue = await self._create_issue(mission_id, problem, correlation)  # Steps 4-5
        if correlation.recommendation == "rollback" and correlation.correlated_deployment is not None:
            await self._create_rollback_action(mission_id, correlation, service_id)  # Step 6
        await self._start_resolution(mission_id, problem, service_id, issue, correlation)  # Step 7
        log.info("incident_workflow_complete", recommendation=correlation.recommendation)

    async def _emit_workflow_beliefs(
        self,
        mission_id: str,
        correlation: CorrelationResult,
        service_id: str | None,
        metrics: ServiceMetrics | None,
    ) -> None:
        """Write the milestone working-memory beliefs for a completed reasoning step.

        Covers anomaly detection, root cause, blast radius, and action plan.
        Each write is best-effort (non-fatal) via :meth:`_safe_write_belief`.

        Args:
            mission_id: Owning mission identifier.
            correlation: Result of the Gemini correlation step.
            service_id: Primary impacted service (may be None).
            metrics: Service metrics fetched during context assembly (may be None).
        """
        anomaly_label = service_id or "unknown"
        await self._safe_write_belief(
            mission_id, "anomaly", "Anomaly",
            f"{anomaly_label[:22]} error rate elevated",
            confidence=0.94, op="write",
        )

        deployment = correlation.correlated_deployment
        if deployment is not None:
            root_value: str = f"deploy #{deployment.id} · {deployment.ref}"[:28]
        else:
            root_value = correlation.recommendation[:28]
        await self._safe_write_belief(
            mission_id, "root_cause", "Root cause",
            root_value,
            confidence=correlation.confidence, op="write",
        )

        if service_id is not None:
            await self._safe_write_belief(
                mission_id, "blast", "Blast radius",
                f"{service_id[:18]} traffic affected",
                confidence=0.9, op="write",
            )

        if deployment is not None:
            plan_value: str = f"rollback {deployment.ref}"[:28]
        else:
            plan_value = correlation.recommendation[:28]
        await self._safe_write_belief(
            mission_id, "plan", "Plan",
            plan_value,
            confidence=correlation.confidence, op="write",
        )

    async def _assemble_context(
        self, problem: DynatraceProblemDetail, service_id: str | None
    ) -> tuple[ServiceMetrics | None, list[GitLabDeployment]]:
        """Concurrently fetch incident metrics and recent deployments (Step 2)."""
        anomaly_start = problem.start_time or datetime.utcnow() - timedelta(minutes=DEFAULT_INCIDENT_WINDOW_MINUTES)
        since = anomaly_start - timedelta(hours=DEPLOYMENT_LOOKBACK_HOURS)
        deployments_call = self._gitlab.list_recent_deployments(self._project_id, since)
        if service_id is None:
            return None, await deployments_call
        metrics, deployments = await asyncio.gather(
            self._dynatrace.get_service_metrics(service_id, anomaly_start, datetime.utcnow()),
            deployments_call,
        )
        return metrics, deployments

    async def _create_issue(
        self, mission_id: str, problem: DynatraceProblemDetail, correlation: CorrelationResult
    ) -> GitLabIssue:
        """Create the issue action, file the issue, and mark the action executed.

        NOTE: filing an incident issue is informational and is auto-executed here.
        Only the rollback (Step 6) is gated on human approval.
        """
        title = f"[NERVE] {problem.title}"
        body = IncidentTemplates.gitlab_issue_body(
            problem,
            correlation.correlated_deployment,
            correlation.reasoning,
            external_context=correlation.external_context,
            grounding_sources=correlation.grounding_sources,
        )
        action = await db.create_action(
            mission_id, ACTION_GITLAB_ISSUE, {"title": title, "labels": IncidentTemplates.LABELS}
        )
        await self._emit(mission_id, EVENT_ACTION_CREATED, {"action_id": action.action_id, "type": ACTION_GITLAB_ISSUE})
        issue = await self._gitlab.create_issue(self._project_id, title, body, IncidentTemplates.LABELS)
        await db.update_action_status(action.action_id, STATUS_EXECUTED, executed_at=datetime.utcnow())
        await self._emit(
            mission_id,
            EVENT_ACTION_EXECUTED,
            {"action_id": action.action_id, "issue_iid": issue.iid, "web_url": issue.web_url},
        )
        return issue

    async def _create_rollback_action(
        self, mission_id: str, correlation: CorrelationResult, service_id: str | None
    ) -> None:
        """Create a PENDING rollback action awaiting human approval (Step 6).

        The rollback is never executed here — it stays pending until a human
        approves it via the API, per NERVE invariant 2.

        Args:
            mission_id: Owning mission identifier.
            correlation: Reasoning output with the correlated deployment.
            service_id: Real affected service id derived from the Dynatrace problem.
        """
        deployment = correlation.correlated_deployment
        assert deployment is not None  # guarded by caller
        impact_label = f"{service_id} · rolling restart" if service_id else "rolling restart"
        payload = {
            "deployment_id": deployment.id,
            "ref": deployment.ref,
            "sha": deployment.sha,
            "environment": deployment.environment,
            "confidence": correlation.confidence,
            "mr_body": IncidentTemplates.rollback_mr_body(deployment),
            "impact": {
                "kind": "pods",
                "label": impact_label,
                "sub": "no downtime",
            },
        }
        action = await db.create_action(mission_id, ACTION_GITLAB_ROLLBACK, payload)
        await self._emit(
            mission_id,
            EVENT_ACTION_CREATED,
            {"action_id": action.action_id, "type": ACTION_GITLAB_ROLLBACK, "requires_approval": True},
        )
        description = (
            f"Roll back deployment {deployment.id} on {deployment.environment} "
            f"(ref={deployment.ref}, sha={(deployment.sha or '')[:8]}). "
            f"Confidence {correlation.confidence:.0%}."
        )
        await telegram_notifier.send_approval_request(
            action.action_id, ACTION_GITLAB_ROLLBACK, description, mission_id
        )
        self._log.info("rollback_action_pending_approval", mission_id=mission_id, action_id=action.action_id)

    async def _start_resolution(
        self,
        mission_id: str,
        problem: DynatraceProblemDetail,
        service_id: str | None,
        issue: GitLabIssue,
        correlation: CorrelationResult,
    ) -> None:
        """Launch the resolution monitor; store the incident once resolved (Step 7)."""
        if service_id is None:
            self._log.info("resolution_monitoring_skipped", mission_id=mission_id, reason="no_service")
            return
        baseline = await self._baseline_error_rate(service_id, problem)
        coro = self._monitor_and_remember(mission_id, problem, service_id, issue, baseline, correlation)
        task = asyncio.create_task(coro, name=f"resolver-{mission_id}")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        await self._emit(
            mission_id,
            EVENT_RESOLUTION_MONITORING_STARTED,
            {"service_id": service_id, "issue_iid": issue.iid, "baseline_error_rate": baseline},
        )

    async def _monitor_and_remember(
        self,
        mission_id: str,
        problem: DynatraceProblemDetail,
        service_id: str,
        issue: GitLabIssue,
        baseline: float,
        correlation: CorrelationResult,
    ) -> None:
        """Run the resolver, then store the resolved incident in memory (Step 7)."""
        await self._resolver.monitor_resolution(mission_id, service_id, issue.iid, baseline)
        mission = await db.get_mission(mission_id)
        if mission is None or mission.status != "resolved":
            return  # monitoring exhausted without a confirmed resolution
        elapsed = (datetime.utcnow() - self._run_started_at).total_seconds() if self._run_started_at else None

        # Belief: service recovered — update working memory with resolution.
        await self._safe_write_belief(
            mission_id, "status", "Status", "recovered",
            confidence=0.97, op="confirm",
        )

        await telegram_notifier.send_notification(
            f"✅ Incident resolved. {service_id} error rate normalized after {self._format_duration(elapsed)}.",
            level="success",
        )
        await self._remember_incident(mission_id, problem, correlation)

    async def _remember_incident(
        self, mission_id: str, problem: DynatraceProblemDetail, correlation: CorrelationResult
    ) -> None:
        """Persist the resolved incident to Memory Bank (best-effort)."""
        elapsed = (datetime.utcnow() - self._run_started_at).total_seconds() if self._run_started_at else None
        outcome = {
            "status": "resolved",
            "recommendation": correlation.recommendation,
            "resolution_time_seconds": elapsed,
            "changed_files": await self._changed_files(correlation.correlated_deployment),
        }
        await self._memory.store_incident(
            mission_id, problem, correlation.correlated_deployment, correlation.reasoning, outcome
        )

    async def _changed_files(self, deployment: GitLabDeployment | None) -> list[str]:
        """Fetch the deployment's changed files for memory metadata (best-effort)."""
        if deployment is None or deployment.sha is None or not self._memory.enabled:
            return []
        try:
            commit = await self._gitlab.get_commit_details(self._project_id, deployment.sha)
            return commit.files_changed
        except Exception as exc:  # noqa: BLE001 — metadata enrichment is best-effort
            self._log.warning("changed_files_fetch_failed", error=str(exc))
            return []

    async def _baseline_error_rate(self, service_id: str, problem: DynatraceProblemDetail) -> float:
        """Fetch the pre-incident error rate to use as the resolution baseline."""
        end = problem.start_time or datetime.utcnow()
        start = end - timedelta(hours=BASELINE_WINDOW_HOURS)
        try:
            metrics = await self._dynatrace.get_service_metrics(service_id, start, end)
        except Exception as exc:  # noqa: BLE001 — baseline is best-effort
            self._log.warning("baseline_fetch_failed", error=str(exc))
            return 0.0
        return metrics.error_rate or 0.0

    @staticmethod
    def _enforce_no_deployment_rule(correlation: CorrelationResult) -> CorrelationResult:
        """Downgrade a rollback to investigate when there is nothing to roll back."""
        if correlation.correlated_deployment is None and correlation.recommendation == "rollback":
            return correlation.model_copy(update={"recommendation": "investigate"})
        return correlation

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        """Render an elapsed-seconds value as a compact ``Xm Ys`` string."""
        if not seconds or seconds < 0:
            return "an unknown interval"
        minutes, secs = divmod(int(seconds), 60)
        return f"{minutes}m {secs}s" if minutes else f"{secs}s"

    @staticmethod
    def _reasoning_payload(correlation: CorrelationResult) -> dict:
        """Build the REASONING_COMPLETE event payload."""
        deployment = correlation.correlated_deployment
        return {
            "recommendation": correlation.recommendation,
            "confidence": correlation.confidence,
            "correlated_deployment_id": deployment.id if deployment else None,
        }

    async def _safe_write_belief(
        self,
        mission_id: str,
        key: str,
        label: str,
        value: str,
        confidence: float = 0.5,
        op: str = "write",
    ) -> None:
        """Write a working-memory belief; log a warning and continue on failure.

        Args:
            mission_id: Owning mission identifier.
            key: Stable belief key (e.g. ``"root_cause"``).
            label: Display label shown in the dashboard panel.
            value: Short display string (≤28 chars recommended).
            confidence: Confidence score in [0.0, 1.0].
            op: Belief operation: write | update | confirm | contradict.
        """
        try:
            await db.write_belief(mission_id, key, label, value, confidence=confidence, op=op)
        except Exception as exc:  # noqa: BLE001 — belief write is non-fatal
            self._log.warning(
                "belief_write_failed", mission_id=mission_id, key=key, error=str(exc)
            )

    async def _safe_record_metric(
        self,
        mission_id: str,
        label: str,
        value: float,
        unit: str = "",
        baseline: float | None = None,
    ) -> None:
        """Record a metric sample; log a warning and continue on failure.

        Args:
            mission_id: Owning mission identifier.
            label: Metric series label (e.g. ``"checkout · error rate"``).
            value: Sampled value (already scaled to display units, e.g. percent).
            unit: Display unit string (e.g. ``"%"``).
            baseline: Optional healthy baseline for sparkline reference line.
        """
        try:
            await db.record_metric(mission_id, label, value, unit=unit, baseline=baseline)
        except Exception as exc:  # noqa: BLE001 — metric record is non-fatal
            self._log.warning(
                "metric_record_failed", mission_id=mission_id, label=label, error=str(exc)
            )

    async def _emit(self, mission_id: str, event_type: str, payload: dict) -> None:
        """Emit a workflow audit event."""
        await db.emit_event(mission_id, event_type, payload, SOURCE_ORCH)

    # ----------------------------------------------------------------- #
    # Default Gemini-backed reasoner (injectable / overridable)
    # ----------------------------------------------------------------- #
    async def _default_reason(
        self,
        problem: DynatraceProblemDetail,
        metrics: ServiceMetrics | None,
        deployments: list[GitLabDeployment],
    ) -> CorrelationResult:
        """Correlate the anomaly with deployments via Gemini, defaulting safely.

        When ``GEMINI_GROUNDING_ENABLED`` is set, the model is given a Google
        Search retrieval tool and asked to look up CVEs/known issues for the
        changed files, outage reports for similar services, and dependency bugs;
        the cited URLs populate ``grounding_sources`` and the model's summary
        populates ``external_context``. When disabled it falls back cleanly to
        ungrounded reasoning (both fields empty).
        """
        grounded = settings.gemini_grounding_enabled
        prompt = self._reasoning_prompt(problem, metrics, deployments, grounded, self._memories)
        try:
            generation = await self._generate_with_retry(prompt)
            data = self._parse_reasoning(generation.text)
        except Exception as exc:  # noqa: BLE001 — reasoning failure -> investigate
            self._log.error("reasoning_failed", error=str(exc))
            return CorrelationResult(reasoning="reasoning unavailable; defaulting to investigate")
        deployment = self._match_deployment(data.get("correlated_deployment_id"), deployments)
        recommendation = data.get("recommendation")
        return CorrelationResult(
            correlated_deployment=deployment,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            recommendation=recommendation if recommendation in RECOMMENDATIONS else "investigate",
            grounding_sources=generation.grounding_sources if grounded else [],
            external_context=str(data.get("external_context", "")) if grounded else "",
        )

    @staticmethod
    def _reasoning_prompt(
        problem: DynatraceProblemDetail,
        metrics: ServiceMetrics | None,
        deployments: list[GitLabDeployment],
        grounded: bool = False,
        memories: list[MemoryEntry] | None = None,
    ) -> str:
        """Build the correlation prompt for Gemini (grounding + past-incident asks)."""
        deploy_lines = [
            {"id": d.id, "ref": d.ref, "sha": d.sha, "created_at": str(d.created_at)} for d in deployments
        ]
        grounding_block = ""
        schema_extra = ""
        if grounded:
            grounding_block = (
                "\nUse Google Search to find EXTERNAL CONTEXT before deciding:\n"
                "- known issues or CVEs related to the changed files/libraries,\n"
                "- recent outage reports for similar services,\n"
                "- known bugs in the dependency versions touched by the deployment.\n"
                "Summarize the findings in the 'external_context' field.\n"
            )
            schema_extra = ", \"external_context\": str"
        return (
            "Correlate this production anomaly with recent deployments.\n"
            "Respond with ONLY JSON: {\"correlated_deployment_id\": int|null, "
            "\"confidence\": float, \"reasoning\": str, "
            f"\"recommendation\": \"rollback\"|\"investigate\"|\"monitor\"{schema_extra}}}.\n"
            f"{grounding_block}"
            f"{IncidentAutopilotWorkflow._memory_block(memories or [])}\n"
            f"PROBLEM: {problem.title} (start={problem.start_time}, services={problem.impacted_services})\n"
            f"TIMELINE: {problem.timeline}\n"
            f"METRICS: {metrics.model_dump() if metrics else None}\n"
            f"DEPLOYMENTS: {json.dumps(deploy_lines, default=str)}\n"
        )

    @staticmethod
    def _memory_block(memories: list[MemoryEntry]) -> str:
        """Render the past-incident context block (empty when there are none)."""
        if not memories:
            return ""
        lines = ["\nPast incidents involving similar services or files:"]
        for entry in memories:
            meta = entry.metadata or {}
            lines.append(
                f"- {entry.summary} "
                f"(service={meta.get('affected_service')}, files={meta.get('changed_files')}, "
                f"recommendation={meta.get('recommendation')})"
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _match_deployment(
        deployment_id: Any, deployments: list[GitLabDeployment]
    ) -> GitLabDeployment | None:
        """Resolve a deployment id from the model back to a deployment object."""
        if deployment_id is None:
            return None
        for deployment in deployments:
            if deployment.id == deployment_id:
                return deployment
        return None

    @staticmethod
    def _parse_reasoning(raw: str) -> dict:
        """Parse the model's JSON reasoning output (stripping fences)."""
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise TypeError("reasoning output is not a JSON object")
        return data

    async def _generate_with_retry(self, prompt: str) -> GenerationResult:
        """Generate with the Gemini retry policy (3 attempts, exp 2→15s)."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15), reraise=True
        ):
            with attempt:
                return await self._generate(prompt)
        raise RuntimeError("generation produced no output")

    async def _default_generate(self, prompt: str) -> GenerationResult:
        """Generate via Vertex AI Gemini, capturing grounding citations."""
        model = self._build_model()
        response = await asyncio.to_thread(model.generate_content, prompt)
        text = getattr(response, "text", "") or ""
        sources = self._extract_grounding(response) if settings.gemini_grounding_enabled else []
        return GenerationResult(text=text, grounding_sources=sources)

    def _build_model(self) -> Any:
        """Build the Gemini model, attaching a Google Search tool when grounded.

        Injected ``model_factory`` (tests) short-circuits the real Vertex client.
        """
        if self._model_factory is not None:
            return self._model_factory()
        from vertexai.generative_models import GenerativeModel, Tool, grounding  # lazy: heavy import

        if settings.gemini_grounding_enabled:
            grounding_tool = Tool.from_google_search_retrieval(grounding.GoogleSearchRetrieval())
            return GenerativeModel(settings.gemini_model, tools=[grounding_tool])
        return GenerativeModel(settings.gemini_model)

    @staticmethod
    def _extract_grounding(response: Any) -> list[str]:
        """Pull cited source URLs from a Gemini response's grounding metadata.

        Defensive across Vertex SDK shapes (``grounding_chunks[*].web.uri`` and
        the older ``grounding_attributions[*].web.uri``); unknown shapes yield [].
        """
        sources: list[str] = []
        for candidate in getattr(response, "candidates", None) or []:
            meta = getattr(candidate, "grounding_metadata", None)
            if meta is None:
                continue
            entries = getattr(meta, "grounding_chunks", None) or getattr(meta, "grounding_attributions", None) or []
            for entry in entries:
                web = getattr(entry, "web", None)
                uri = getattr(web, "uri", None) if web is not None else None
                if uri and uri not in sources:
                    sources.append(uri)
        return sources
