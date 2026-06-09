"""Dynatrace MCP client and its typed response models.

Wraps the Dynatrace MCP server tools (``get_problems``, ``get_problem_details``,
``get_metrics``) and returns Pydantic models rather than raw dicts. Parsing is
tolerant of field-name variants; missing required fields surface as
:class:`~exceptions.MCPToolCallError`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from config import settings
from exceptions import MCPToolCallError
from mcp_tools.base_client import BaseMCPClient

SERVER_DYNATRACE = "dynatrace"

TOOL_GET_PROBLEMS = "get_problems"
TOOL_GET_PROBLEM_DETAILS = "get_problem_details"
TOOL_GET_METRICS = "get_metrics"


class DynatraceProblem(BaseModel):
    """A single active Dynatrace problem (summary view)."""

    model_config = ConfigDict(extra="ignore")

    problem_id: str
    title: str
    severity: str
    status: str
    affected_entities: list[str] = Field(default_factory=list)
    start_time: datetime | None = None


class DynatraceProblemDetail(BaseModel):
    """Detailed view of one Dynatrace problem, including timeline."""

    model_config = ConfigDict(extra="ignore")

    problem_id: str
    title: str
    severity: str
    status: str
    impacted_services: list[str] = Field(default_factory=list)
    root_cause: str | None = None
    timeline: list[dict] = Field(default_factory=list)
    start_time: datetime | None = None
    end_time: datetime | None = None


class ServiceMetrics(BaseModel):
    """Error rate, latency, and throughput for a service over a window."""

    model_config = ConfigDict(extra="ignore")

    service_id: str
    error_rate: float | None = None
    latency_ms: float | None = None
    throughput: float | None = None
    from_time: datetime
    to_time: datetime
    raw_metrics: dict = Field(default_factory=dict)


def _parse_dt(value: Any) -> datetime | None:
    """Parse a Dynatrace timestamp (epoch-ms int or ISO string)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value / 1000.0)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_list(raw: dict, *keys: str) -> list[dict]:
    """Return the first list found under the given keys (or [])."""
    if isinstance(raw, list):
        return raw
    for key in keys:
        value = raw.get(key)
        if isinstance(value, list):
            return value
    return []


def _entity_names(items: Any) -> list[str]:
    """Normalize a list of entities (dicts or strings) to display names."""
    names: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            names.append(str(item.get("name") or item.get("entityId") or item.get("id")))
        else:
            names.append(str(item))
    return names


class DynatraceClient(BaseMCPClient):
    """Typed client for the Dynatrace MCP server."""

    def __init__(
        self,
        *,
        mission_id: str | None = None,
        failure_engine: Any | None = None,
        server_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize from settings unless ``server_url`` is overridden.

        Args:
            mission_id: Mission to attribute audit events to.
            failure_engine: FailureEngine whose modifications to apply.
            server_url: Override the derived MCP endpoint URL.
            **kwargs: Forwarded retry/backoff overrides to the base client.
        """
        url = server_url or f"{settings.dynatrace_environment_url.rstrip('/')}/mcp"
        headers = {"Authorization": f"Api-Token {settings.dynatrace_api_token}"}
        super().__init__(
            SERVER_DYNATRACE,
            url,
            headers,
            mission_id=mission_id,
            failure_engine=failure_engine,
            **kwargs,
        )

    async def get_active_problems(self) -> list[DynatraceProblem]:
        """Return all currently active problems.

        Returns:
            List of :class:`DynatraceProblem`.
        """
        raw = await self.call_tool(TOOL_GET_PROBLEMS, {})
        return [self._parse_problem(item) for item in _as_list(raw, "problems", "result", "data")]

    async def get_problem_details(self, problem_id: str) -> DynatraceProblemDetail:
        """Return the detailed timeline and impact for one problem.

        Args:
            problem_id: Dynatrace problem identifier.

        Returns:
            A :class:`DynatraceProblemDetail`.
        """
        raw = await self.call_tool(TOOL_GET_PROBLEM_DETAILS, {"problem_id": problem_id})
        body = raw.get("problem") if isinstance(raw.get("problem"), dict) else raw
        try:
            return DynatraceProblemDetail(
                problem_id=str(body.get("problemId") or body.get("problem_id") or problem_id),
                title=body.get("title") or body.get("displayName") or "",
                severity=body.get("severityLevel") or body.get("severity") or "UNKNOWN",
                status=body.get("status") or "OPEN",
                impacted_services=_entity_names(body.get("impactedEntities") or body.get("impacted_services")),
                root_cause=body.get("rootCause") or body.get("root_cause"),
                timeline=body.get("timeline") or body.get("events") or [],
                start_time=_parse_dt(body.get("startTime") or body.get("start_time")),
                end_time=_parse_dt(body.get("endTime") or body.get("end_time")),
            )
        except PydanticValidationError as exc:
            raise self._parse_error(TOOL_GET_PROBLEM_DETAILS, exc) from exc

    async def get_service_metrics(
        self, service_id: str, from_time: datetime, to_time: datetime
    ) -> ServiceMetrics:
        """Return error rate, latency, and throughput for a service window.

        Args:
            service_id: Dynatrace service entity id.
            from_time: Window start.
            to_time: Window end.

        Returns:
            A :class:`ServiceMetrics`.
        """
        args = {"service_id": service_id, "from": from_time.isoformat(), "to": to_time.isoformat()}
        raw = await self.call_tool(TOOL_GET_METRICS, args)
        try:
            return ServiceMetrics(
                service_id=service_id,
                error_rate=raw.get("error_rate") or raw.get("errorRate"),
                latency_ms=raw.get("latency_ms") or raw.get("latency"),
                throughput=raw.get("throughput"),
                from_time=from_time,
                to_time=to_time,
                raw_metrics=raw if isinstance(raw, dict) else {},
            )
        except PydanticValidationError as exc:
            raise self._parse_error(TOOL_GET_METRICS, exc) from exc

    def _parse_problem(self, item: dict) -> DynatraceProblem:
        """Parse one problem summary into a model."""
        try:
            return DynatraceProblem(
                problem_id=str(item.get("problemId") or item.get("problem_id") or item.get("id")),
                title=item.get("title") or item.get("displayName") or "",
                severity=item.get("severityLevel") or item.get("severity") or "UNKNOWN",
                status=item.get("status") or "OPEN",
                affected_entities=_entity_names(item.get("affectedEntities") or item.get("affected_entities")),
                start_time=_parse_dt(item.get("startTime") or item.get("start_time")),
            )
        except PydanticValidationError as exc:
            raise self._parse_error(TOOL_GET_PROBLEMS, exc) from exc

    def _parse_error(self, tool: str, exc: PydanticValidationError) -> MCPToolCallError:
        """Build a typed error for a response that failed model validation."""
        return MCPToolCallError(
            "Dynatrace response failed validation",
            context={"server": self.server_name, "tool": tool, "error": str(exc)},
            recoverable=False,
        )
