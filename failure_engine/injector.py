"""FailureEngine: controlled failure injection at the MCP boundary.

Holds a set of active :class:`FailureScenario` objects and, for a given MCP tool
call, produces an :class:`MCPCallModification` describing how to perturb it
(delay, error, or result transform). Effects are applied by ``BaseMCPClient``;
the RiskAgent separately reads :meth:`get_active_failures` for scoring.

Nothing takes effect unless ``settings.failure_engine_enabled`` is true.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

import structlog
from pydantic import BaseModel, ConfigDict, Field

from config import settings
from exceptions import MCPError, MCPToolCallError, StateError
from state import database as db

log = structlog.get_logger()

SOURCE_FAILURE_ENGINE = "failure_engine"
EVENT_FAILURE_INJECTED = "FAILURE_INJECTED"
EVENT_FAILURE_CLEARED = "FAILURE_CLEARED"

DELAY_MULTIPLIER_SECONDS = 10.0
CONTRADICTORY_MIN_FACTOR = 0.1
CONTRADICTORY_MAX_FACTOR = 3.0
_ERROR_RATE_KEYS = ("error_rate", "errorRate")
_NUMERIC_METRIC_KEYS = ("error_rate", "errorRate", "latency_ms", "latency", "throughput")


class FailureType(str, Enum):
    """The kinds of failure NERVE can inject (CLAUDE.md Failure Engine)."""

    DELAYED_SIGNAL = "DELAYED_SIGNAL"
    CONTRADICTORY_METRICS = "CONTRADICTORY_METRICS"
    SERVICE_OUTAGE = "SERVICE_OUTAGE"
    NOISY_DATA = "NOISY_DATA"
    DEPLOYMENT_BLACKOUT = "DEPLOYMENT_BLACKOUT"


class FailureScenario(BaseModel):
    """A single active failure scenario targeting an MCP tool (or agent)."""

    model_config = ConfigDict(use_enum_values=False)

    failure_type: FailureType
    target: str
    severity: float = Field(default=0.5, ge=0.0, le=1.0)
    duration_seconds: int = 0
    activated_at: datetime = Field(default_factory=datetime.utcnow)

    def as_dict(self) -> dict:
        """Serialize for events and RiskAgent scoring (``type`` key included)."""
        return {
            "type": self.failure_type.value,
            "target": self.target,
            "severity": self.severity,
            "duration_seconds": self.duration_seconds,
            "activated_at": self.activated_at.isoformat(),
        }

    def is_expired(self, now: datetime) -> bool:
        """Return True if this scenario's duration has elapsed (0 = indefinite)."""
        return self.duration_seconds > 0 and (now - self.activated_at).total_seconds() >= self.duration_seconds


@dataclass
class MCPCallModification:
    """How to modify a single MCP call: delay it, error it, or transform output."""

    delay_seconds: float = 0.0
    error: MCPError | None = None
    result_transform: Callable[[dict], dict] | None = None

    async def apply_before_call(self) -> None:
        """Apply pre-call effects: raise an injected error, or delay the call.

        Raises:
            MCPError: If a SERVICE_OUTAGE (or similar) error is injected.
        """
        if self.error is not None:
            raise self.error
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

    def apply_to_result(self, result: dict) -> dict:
        """Apply the post-call result transform, if any."""
        if self.result_transform is None:
            return result
        return self.result_transform(result)


class FailureEngine:
    """Manages active failure scenarios and renders MCP call modifications."""

    def __init__(self, mission_id: str | None = None) -> None:
        """Initialize the engine.

        Args:
            mission_id: Mission to attribute FAILURE_INJECTED/CLEARED events to.
        """
        self.mission_id = mission_id
        self._scenarios: list[FailureScenario] = []
        self._log = structlog.get_logger().bind(component="failure_engine")

    @property
    def enabled(self) -> bool:
        """Whether the engine is active (gated by the feature flag)."""
        return settings.failure_engine_enabled

    async def inject(self, scenario: FailureScenario) -> None:
        """Activate a failure scenario (no-op when the engine is disabled).

        Args:
            scenario: The failure to activate.
        """
        if not self.enabled:
            self._log.warning("failure_inject_skipped_disabled", failure_type=scenario.failure_type.value)
            return
        self._scenarios.append(scenario)
        await self._emit(EVENT_FAILURE_INJECTED, scenario.as_dict())
        self._log.info("failure_injected", failure_type=scenario.failure_type.value, target=scenario.target)

    async def clear(self, failure_type: FailureType) -> None:
        """Deactivate all active scenarios of a given type.

        Args:
            failure_type: The failure type to clear.
        """
        removed = [s for s in self._scenarios if s.failure_type == failure_type]
        self._scenarios = [s for s in self._scenarios if s.failure_type != failure_type]
        for scenario in removed:
            await self._emit(EVENT_FAILURE_CLEARED, scenario.as_dict())
        if removed:
            self._log.info("failure_cleared", failure_type=failure_type.value, count=len(removed))

    def get_active_failures(self) -> list[FailureScenario]:
        """Return active (non-expired) scenarios, pruning expired ones."""
        now = datetime.utcnow()
        self._scenarios = [s for s in self._scenarios if not s.is_expired(now)]
        return list(self._scenarios)

    def apply_to_mcp_call(self, tool: str, args: dict) -> MCPCallModification:
        """Compute how a given MCP tool call should be modified.

        Args:
            tool: The MCP tool name about to be called.
            args: The call arguments (unused today; reserved for targeting).

        Returns:
            An :class:`MCPCallModification` (empty when disabled or no match).
        """
        if not self.enabled:
            return MCPCallModification()
        matching = [s for s in self.get_active_failures() if self._targets(s, tool)]
        if not matching:
            return MCPCallModification()
        return self._build_modification(matching, tool)

    def _build_modification(self, scenarios: list[FailureScenario], tool: str) -> MCPCallModification:
        """Combine matching scenarios into a single modification."""
        delay = 0.0
        error: MCPError | None = None
        transforms: list[Callable[[dict], dict]] = []
        for scenario in scenarios:
            ftype = scenario.failure_type
            if ftype == FailureType.SERVICE_OUTAGE:
                error = MCPToolCallError(
                    "service outage (injected)", context={"tool": tool, "injected": True}, recoverable=False
                )
            elif ftype == FailureType.DELAYED_SIGNAL:
                delay = max(delay, scenario.severity * DELAY_MULTIPLIER_SECONDS)
            elif ftype == FailureType.CONTRADICTORY_METRICS:
                transforms.append(self._contradictory_metrics)
            elif ftype == FailureType.NOISY_DATA:
                transforms.append(lambda result, sev=scenario.severity: self._noisy_data(result, sev))
            elif ftype == FailureType.DEPLOYMENT_BLACKOUT:
                transforms.append(self._deployment_blackout)
        transform = self._compose(transforms) if transforms else None
        return MCPCallModification(delay_seconds=delay, error=error, result_transform=transform)

    @staticmethod
    def _targets(scenario: FailureScenario, tool: str) -> bool:
        """Return True if a scenario targets the given tool (exact or qualified)."""
        return scenario.target == tool or tool in scenario.target

    @staticmethod
    def _compose(transforms: list[Callable[[dict], dict]]) -> Callable[[dict], dict]:
        """Chain result transforms left-to-right."""

        def _apply(result: dict) -> dict:
            for transform in transforms:
                result = transform(result)
            return result

        return _apply

    @staticmethod
    def _contradictory_metrics(result: dict) -> dict:
        """Multiply error_rate by a random factor to create conflicting readings."""
        if not isinstance(result, dict):
            return result
        modified = dict(result)
        factor = random.uniform(CONTRADICTORY_MIN_FACTOR, CONTRADICTORY_MAX_FACTOR)
        for key in _ERROR_RATE_KEYS:
            value = modified.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                modified[key] = value * factor
        modified["_contradictory"] = True
        return modified

    @staticmethod
    def _noisy_data(result: dict, severity: float) -> dict:
        """Add gaussian noise (scaled by severity) to numeric metric values."""
        if not isinstance(result, dict):
            return result
        modified = dict(result)
        for key in _NUMERIC_METRIC_KEYS:
            value = modified.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                modified[key] = value + random.gauss(0.0, severity * (abs(value) + 1.0))
        modified["_noisy"] = severity
        return modified

    @staticmethod
    def _deployment_blackout(result: dict) -> dict:
        """Return empty lists for deployment-query results."""
        if isinstance(result, list):
            return []
        if not isinstance(result, dict):
            return result
        modified = {key: ([] if isinstance(value, list) else value) for key, value in result.items()}
        modified["_blackout"] = True
        return modified

    async def _emit(self, event_type: str, payload: dict) -> None:
        """Emit a failure-engine event if bound to a mission."""
        if self.mission_id is None:
            return
        try:
            await db.emit_event(self.mission_id, event_type, payload, SOURCE_FAILURE_ENGINE)
        except StateError as exc:
            self._log.warning("failure_event_emit_failed", event_type=event_type, error=str(exc))
