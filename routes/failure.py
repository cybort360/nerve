"""Failure-engine routes: inject and clear failures (opt-in only).

Both routes return 403 unless ``settings.failure_engine_enabled`` is true
(CLAUDE.md invariant 7). They operate on the engine shared with the orchestrator
so injections affect both MCP calls and RiskAgent scoring.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from config import settings
from failure_engine.injector import FailureEngine, FailureScenario, FailureType

log = structlog.get_logger()
router = APIRouter(prefix="/failure-engine", tags=["failure-engine"])


class ClearFailureRequest(BaseModel):
    """Body for ``POST /failure-engine/clear``."""

    failure_type: FailureType


def _engine_or_403(request: Request) -> FailureEngine:
    """Return the shared FailureEngine, or raise 403 when disabled/missing."""
    if not settings.failure_engine_enabled:
        raise HTTPException(status_code=403, detail="failure engine disabled")
    engine = getattr(request.app.state, "failure_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="failure engine unavailable")
    return engine


@router.post("/inject")
async def inject_failure(
    scenario: FailureScenario, request: Request, mission_id: str | None = Query(default=None)
) -> dict:
    """Activate a failure scenario.

    Args:
        scenario: The failure to inject.
        request: FastAPI request (for the engine on app.state).
        mission_id: Optional mission to attribute FAILURE_INJECTED events to.

    Returns:
        Status and the currently active failures.
    """
    engine = _engine_or_403(request)
    if mission_id:
        engine.mission_id = mission_id
    await engine.inject(scenario)
    log.info("failure_injected_via_api", failure_type=scenario.failure_type.value, target=scenario.target)
    return {"status": "injected", "active": [s.as_dict() for s in engine.get_active_failures()]}


@router.post("/clear")
async def clear_failure(body: ClearFailureRequest, request: Request) -> dict:
    """Deactivate all active failures of a given type.

    Args:
        body: The failure type to clear.
        request: FastAPI request (for the engine on app.state).

    Returns:
        Status and the remaining active failures.
    """
    engine = _engine_or_403(request)
    await engine.clear(body.failure_type)
    log.info("failure_cleared_via_api", failure_type=body.failure_type.value)
    return {"status": "cleared", "active": [s.as_dict() for s in engine.get_active_failures()]}
