"""Abstract base for all NERVE agents.

Defines the agent contract (``run``/``observe``/``report``), the
:class:`AgentResult` return type, and the shared event-type/source constants
agents use when emitting to the audit trail.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from time import perf_counter
from typing import Literal

import structlog

from state.models import MissionState

# Event-source + event-type constants (ARCHITECTURE.md section 2). Shared by all
# agents so the audit trail uses a single canonical spelling for each.
SOURCE_AGENT = "agent"
EVENT_TASK_COMPLETED = "TASK_COMPLETED"
EVENT_TASK_FAILED = "TASK_FAILED"
EVENT_AGENT_OBSERVATION = "AGENT_OBSERVATION"
EVENT_ACTION_EXECUTED = "ACTION_EXECUTED"
EVENT_RISK_SCORE_UPDATED = "RISK_SCORE_UPDATED"

AgentStatus = Literal["success", "failed"]


@dataclass
class AgentResult:
    """Structured outcome of a single agent ``run``.

    Attributes:
        status: Whether the run succeeded or failed.
        output: Agent-specific structured output.
        observations: Human-readable notes the agent made about state.
        duration_ms: Wall-clock duration of the run in milliseconds.
    """

    status: AgentStatus
    output: dict = field(default_factory=dict)
    observations: list[str] = field(default_factory=list)
    duration_ms: float = 0.0


class BaseAgent(ABC):
    """Abstract agent. Subclasses implement ``run`` and ``observe``."""

    def __init__(self, name: str, mission_id: str) -> None:
        """Initialize the agent with its role name and mission context.

        Args:
            name: Agent role name (e.g. ``"planner"``).
            mission_id: Mission this agent instance is bound to.
        """
        self.name = name
        self.mission_id = mission_id
        self._log = structlog.get_logger().bind(agent=name, mission_id=mission_id)
        self._last_result: AgentResult | None = None

    @abstractmethod
    async def run(self, context: dict) -> AgentResult:
        """Execute the agent's main behavior and return a structured result."""
        raise NotImplementedError

    @abstractmethod
    def observe(self, state: MissionState) -> list[str]:
        """Return human-readable observations about the current mission state."""
        raise NotImplementedError

    def report(self) -> dict:
        """Return the last run's structured output for the audit trail.

        Returns:
            Dict with agent name and the last :class:`AgentResult` fields, or a
            null-status skeleton if the agent has not run yet.
        """
        result = self._last_result
        if result is None:
            return {
                "agent": self.name,
                "status": None,
                "output": {},
                "observations": [],
                "duration_ms": 0.0,
            }
        return {
            "agent": self.name,
            "status": result.status,
            "output": result.output,
            "observations": result.observations,
            "duration_ms": result.duration_ms,
        }

    def _finish(
        self,
        status: AgentStatus,
        output: dict,
        observations: list[str],
        start: float,
    ) -> AgentResult:
        """Build, store, and return an :class:`AgentResult` with timing.

        Args:
            status: Final run status.
            output: Structured output payload.
            observations: Observations gathered during the run.
            start: ``perf_counter`` value captured at run start.

        Returns:
            The constructed :class:`AgentResult`, also cached for ``report()``.
        """
        duration_ms = (perf_counter() - start) * 1000
        result = AgentResult(
            status=status,
            output=output,
            observations=observations,
            duration_ms=duration_ms,
        )
        self._last_result = result
        return result
