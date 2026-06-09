"""RiskAgent: scores mission-state uncertainty from 0.0 to 1.0.

Combines four dimensions — failed tasks, retry pressure, active failure
injections, and contradictory signals — into a weighted overall score, emits a
RISK_SCORE_UPDATED event, and returns a :class:`RiskScore`. The RiskAgent is
the only component that sees failure-injection state (CLAUDE.md Failure Engine).
"""

from __future__ import annotations

from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field

from agents.base_agent import EVENT_RISK_SCORE_UPDATED, SOURCE_AGENT, AgentResult, BaseAgent
from config import settings
from state import database as db
from state.models import MissionState, Task

# Per-dimension weights (sum to 1.0). Tuning constants kept local to the agent
# rather than env config, as they are model behavior, not deployment settings.
WEIGHT_FAILED = 0.4
WEIGHT_CONTRADICTIONS = 0.3
WEIGHT_INJECTIONS = 0.2
WEIGHT_RETRIES = 0.1

CONTRADICTORY_INJECTION_TYPE = "CONTRADICTORY_METRICS"
DEFAULT_INJECTION_SEVERITY = 0.5


class RiskScore(BaseModel):
    """Risk assessment with an overall score and per-dimension breakdown."""

    model_config = ConfigDict(extra="ignore")

    overall: float = Field(ge=0.0, le=1.0)
    breakdown: dict[str, float]
    threshold_exceeded: bool


class RiskAgent(BaseAgent):
    """Scores mission state for uncertainty and failure risk."""

    def __init__(self, mission_id: str) -> None:
        """Initialize the risk agent for a mission."""
        super().__init__(name="risk", mission_id=mission_id)

    async def run(self, context: dict) -> AgentResult:
        """Score the state in ``context`` and emit RISK_SCORE_UPDATED.

        Args:
            context: Must contain ``state`` (MissionState); may contain
                ``failure_injections`` (list of active injection dicts).

        Returns:
            AgentResult whose output holds the serialized risk score.
        """
        start = perf_counter()
        state: MissionState = context["state"]
        injections: list[dict] = context.get("failure_injections", [])
        score = self.assess(state, injections)
        await db.emit_event(
            self.mission_id,
            EVENT_RISK_SCORE_UPDATED,
            {
                "overall": score.overall,
                "breakdown": score.breakdown,
                "threshold_exceeded": score.threshold_exceeded,
            },
            SOURCE_AGENT,
        )
        self._log.info("risk_scored", overall=score.overall, exceeded=score.threshold_exceeded)
        return self._finish("success", {"risk": score.model_dump()}, [f"risk {score.overall:.2f}"], start)

    def observe(self, state: MissionState) -> list[str]:
        """Note the count of failed tasks contributing to risk."""
        failed = sum(1 for t in state.tasks if t.status == "failed")
        return [f"{failed} failed tasks in mission"]

    def assess(self, state: MissionState, failure_injections: list[dict] | None = None) -> RiskScore:
        """Compute the risk score for a mission state.

        Args:
            state: Aggregated mission state.
            failure_injections: Active failure injections (RiskAgent-only view).

        Returns:
            A :class:`RiskScore` with overall value and per-dimension breakdown.
        """
        injections = failure_injections or []
        tasks = state.tasks
        breakdown = {
            "failed_tasks": self._dim_failed(tasks),
            "retries": self._dim_retries(tasks),
            "failure_injections": self._dim_injections(injections),
            "contradictions": self._dim_contradictions(tasks, injections),
        }
        overall = self._combine(breakdown)
        return RiskScore(
            overall=overall,
            breakdown=breakdown,
            threshold_exceeded=overall > settings.risk_threshold,
        )

    @staticmethod
    def _dim_failed(tasks: list[Task]) -> float:
        """Fraction of tasks that have failed (0.0–1.0)."""
        if not tasks:
            return 0.0
        return min(sum(1 for t in tasks if t.status == "failed") / len(tasks), 1.0)

    @staticmethod
    def _dim_retries(tasks: list[Task]) -> float:
        """Total retries relative to the configured retry budget (0.0–1.0)."""
        if not tasks:
            return 0.0
        total = sum(t.retry_count for t in tasks)
        cap = max(len(tasks) * settings.max_task_retries, 1)
        return min(total / cap, 1.0)

    @staticmethod
    def _dim_injections(injections: list[dict]) -> float:
        """Worst active injection severity (0.0–1.0)."""
        severities = [float(i.get("severity", DEFAULT_INJECTION_SEVERITY)) for i in injections]
        return min(max(severities), 1.0) if severities else 0.0

    @staticmethod
    def _dim_contradictions(tasks: list[Task], injections: list[dict]) -> float:
        """Density of contradictory signals across tasks and injections."""
        contradictory = sum(1 for i in injections if i.get("type") == CONTRADICTORY_INJECTION_TYPE)
        bad = sum(1 for t in tasks if t.status == "completed" and t.error)
        return min((contradictory + bad) / max(len(tasks), 1), 1.0)

    @staticmethod
    def _combine(breakdown: dict[str, float]) -> float:
        """Weighted, clamped combination of the four risk dimensions."""
        overall = (
            WEIGHT_FAILED * breakdown["failed_tasks"]
            + WEIGHT_CONTRADICTIONS * breakdown["contradictions"]
            + WEIGHT_INJECTIONS * breakdown["failure_injections"]
            + WEIGHT_RETRIES * breakdown["retries"]
        )
        return max(0.0, min(overall, 1.0))
