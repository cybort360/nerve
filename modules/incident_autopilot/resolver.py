"""Resolution monitoring loop for Incident Autopilot.

Polls Dynatrace for a service's error rate and, once it has returned to within a
tolerance of the pre-incident baseline for enough consecutive checks, closes the
GitLab issue and resolves the mission.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import structlog

from config import settings
from exceptions import MCPError
from mcp_tools.dynatrace import ServiceMetrics
from state import database as db

SOURCE_ORCH = "orchestrator"
EVENT_RESOLUTION_CHECK = "RESOLUTION_CHECK"
EVENT_ISSUE_CLOSED = "ISSUE_CLOSED"
EVENT_MISSION_STATUS_CHANGED = "MISSION_STATUS_CHANGED"

DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_TOLERANCE = 0.10
DEFAULT_REQUIRED_CONSECUTIVE = 3
METRIC_WINDOW_MINUTES = 5
_MIN_THRESHOLD = 1e-9


class IncidentResolver:
    """Monitors a service until its error rate normalizes, then resolves."""

    def __init__(
        self,
        dynatrace: Any,
        gitlab: Any,
        *,
        project_id: str | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        tolerance: float = DEFAULT_TOLERANCE,
        required_consecutive: int = DEFAULT_REQUIRED_CONSECUTIVE,
        max_checks: int | None = None,
    ) -> None:
        """Initialize the resolver.

        Args:
            dynatrace: Dynatrace MCP client.
            gitlab: GitLab MCP client.
            project_id: GitLab project id (defaults to settings).
            poll_interval_seconds: Seconds between polls (60 in production).
            tolerance: Fractional band around baseline considered "normal".
            required_consecutive: Consecutive normal checks needed to resolve.
            max_checks: Optional cap on total checks (defaults to unbounded).
        """
        self._dynatrace = dynatrace
        self._gitlab = gitlab
        self._project_id = project_id or settings.gitlab_project_id
        self._poll_interval = poll_interval_seconds
        self._tolerance = tolerance
        self._required = required_consecutive
        self._max_checks = max_checks
        self._log = structlog.get_logger().bind(component="incident_resolver")

    async def monitor_resolution(
        self, mission_id: str, service_id: str, issue_iid: int, baseline_error_rate: float
    ) -> None:
        """Poll until the error rate is normal for enough consecutive checks.

        Args:
            mission_id: Mission to resolve on success.
            service_id: Dynatrace service entity id to monitor.
            issue_iid: GitLab issue to close on resolution.
            baseline_error_rate: Pre-incident healthy error rate.
        """
        log = self._log.bind(mission_id=mission_id, service_id=service_id)
        consecutive = 0
        checks = 0
        # Convert fraction baselines (e.g. 0.02) to percentage for display.
        baseline_pct = round(baseline_error_rate * 100, 2)
        try:
            while self._max_checks is None or checks < self._max_checks:
                rate = await self._current_error_rate(service_id)
                consecutive = consecutive + 1 if self._is_normal(rate, baseline_error_rate) else 0
                await db.emit_event(
                    mission_id,
                    EVENT_RESOLUTION_CHECK,
                    {"error_rate": rate, "baseline": baseline_error_rate, "consecutive": consecutive},
                    SOURCE_ORCH,
                )
                # Record the recovery sparkline sample (best-effort, non-fatal).
                if rate is not None:
                    try:
                        await db.record_metric(
                            mission_id,
                            "checkout · error rate",
                            round(rate * 100, 2),
                            unit="%",
                            baseline=baseline_pct,
                        )
                    except Exception as exc:  # noqa: BLE001 — metric record is non-fatal
                        self._log.warning(
                            "resolver_metric_record_failed",
                            mission_id=mission_id,
                            error=str(exc),
                        )
                if consecutive >= self._required:
                    await self._resolve(mission_id, issue_iid)
                    return
                checks += 1
                await asyncio.sleep(self._poll_interval)
            log.info("resolution_monitoring_exhausted", checks=checks)
        except asyncio.CancelledError:
            log.info("resolution_monitoring_cancelled")
            raise

    def _is_normal(self, rate: float | None, baseline: float) -> bool:
        """Return True if ``rate`` is within tolerance of ``baseline``."""
        if rate is None:
            return False
        threshold = max(abs(baseline) * self._tolerance, _MIN_THRESHOLD)
        return abs(rate - baseline) <= threshold

    async def _current_error_rate(self, service_id: str) -> float | None:
        """Fetch the service's current error rate, swallowing MCP errors."""
        to_time = datetime.utcnow()
        from_time = to_time - timedelta(minutes=METRIC_WINDOW_MINUTES)
        try:
            metrics: ServiceMetrics = await self._dynatrace.get_service_metrics(
                service_id, from_time, to_time
            )
        except MCPError as exc:
            self._log.warning("resolution_metric_fetch_failed", error=str(exc))
            return None
        return metrics.error_rate

    async def _resolve(self, mission_id: str, issue_iid: int) -> None:
        """Close the issue, resolve the mission, and emit the transition."""
        await self._gitlab.close_issue(self._project_id, issue_iid)
        await db.emit_event(mission_id, EVENT_ISSUE_CLOSED, {"issue_iid": issue_iid}, SOURCE_ORCH)
        await db.update_mission_status(mission_id, "resolved")
        await db.emit_event(
            mission_id,
            EVENT_MISSION_STATUS_CHANGED,
            {"to": "resolved", "reason": "metrics_normalized"},
            SOURCE_ORCH,
        )
        self._log.info("incident_resolved", mission_id=mission_id, issue_iid=issue_iid)
