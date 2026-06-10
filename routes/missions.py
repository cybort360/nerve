"""Mission routes: create, read aggregate state, and page through events."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

from config import settings
from routes.schemas import (
    CreateMissionRequest,
    CreateMissionResponse,
    EventsPageResponse,
    MissionListResponse,
    MissionStateResponse,
    MissionSummary,
)
from state import database as db

log = structlog.get_logger()
router = APIRouter(prefix="/missions", tags=["missions"])

SOURCE_ORCH = "orchestrator"
EVENT_MISSION_CREATED = "MISSION_CREATED"
RISK_EVENT_TYPE = "RISK_SCORE_UPDATED"
MAX_EVENT_PAGE = 200
# High-frequency internal events hidden from the dashboard feed (still in the
# full audit log via GET /missions/{id}/events).
FEED_NOISE_TYPES = ["MCP_TOOL_CALLED", "MCP_TOOL_RESULT", "RESOLUTION_CHECK"]


@router.get("", response_model=MissionListResponse)
async def list_missions() -> MissionListResponse:
    """Return recent missions as compact summaries for the fleet roster.

    Returns:
        Up to 12 most-recently-updated missions as :class:`MissionSummary` items.
    """
    missions = await db.list_recent_missions()
    summaries = [
        MissionSummary(
            mission_id=m.mission_id,
            goal=m.goal,
            mission_type=m.mission_type,
            status=m.status,
            updated_at=m.updated_at,
        )
        for m in missions
    ]
    log.info("missions_listed", count=len(summaries))
    return MissionListResponse(missions=summaries)


@router.post("", response_model=CreateMissionResponse, status_code=201)
async def create_mission(body: CreateMissionRequest, request: Request) -> CreateMissionResponse:
    """Create a mission and start its orchestration loop.

    Args:
        body: Goal and mission type.
        request: FastAPI request (for the orchestrator on app.state).

    Returns:
        The new mission id and its current status.
    """
    mission = await db.create_mission(body.goal, body.mission_type)
    await db.emit_event(
        mission.mission_id,
        EVENT_MISSION_CREATED,
        {"goal": body.goal, "mission_type": body.mission_type},
        SOURCE_ORCH,
    )
    await request.app.state.orchestrator.run_mission(mission.mission_id)
    log.info("mission_created_via_api", mission_id=mission.mission_id, mission_type=body.mission_type)
    return CreateMissionResponse(mission_id=mission.mission_id, status=mission.status)


@router.get("/{mission_id}", response_model=MissionStateResponse)
async def read_mission_state(mission_id: str, request: Request) -> MissionStateResponse:
    """Return the aggregate mission view consumed by the dashboard.

    Args:
        mission_id: Mission identifier.
        request: FastAPI request (for the failure engine on app.state).

    Returns:
        Mission, tasks, recent events, latest snapshot, pending actions, active
        failures, and the latest risk score.

    Raises:
        HTTPException: 404 if the mission does not exist.
    """
    state = await db.get_mission_state(mission_id)
    if state is None:
        raise HTTPException(status_code=404, detail="mission not found")
    snapshot = await db.get_latest_snapshot(mission_id)
    pending = await db.get_actions_for_mission(mission_id, status="pending")
    risk, breakdown = await _latest_risk(mission_id)
    feed = await db.get_recent_events_excluding(mission_id, FEED_NOISE_TYPES)
    return MissionStateResponse(
        mission=state.mission,
        tasks=state.tasks,
        recent_events=feed,
        latest_snapshot=snapshot,
        pending_actions=pending,
        active_failures=_active_failures(request),
        risk=risk,
        risk_breakdown=breakdown,
        beliefs=state.beliefs,
        metric_series=state.metric_series,
    )


@router.get("/{mission_id}/events", response_model=EventsPageResponse)
async def read_events(
    mission_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_EVENT_PAGE),
    offset: int = Query(default=0, ge=0),
) -> EventsPageResponse:
    """Return a chronological, paginated event log for a mission.

    Args:
        mission_id: Mission identifier.
        limit: Page size (1–200).
        offset: Events to skip from the start.

    Returns:
        The page of events plus total/limit/offset metadata.
    """
    events = await db.get_events_page(mission_id, limit, offset)
    total = await db.count_events_for_mission(mission_id)
    return EventsPageResponse(events=events, total=total, limit=limit, offset=offset)


async def _latest_risk(mission_id: str) -> tuple[float | None, dict | None]:
    """Return the latest risk score, queried directly so it never ages out."""
    event = await db.get_latest_event_of_type(mission_id, RISK_EVENT_TYPE)
    if event is None:
        return None, None
    return event.payload.get("overall"), event.payload.get("breakdown")


def _active_failures(request: Request) -> list[dict]:
    """Return active failure injections from the engine, if enabled."""
    engine = getattr(request.app.state, "failure_engine", None)
    if engine is None or not settings.failure_engine_enabled:
        return []
    return [scenario.as_dict() for scenario in engine.get_active_failures()]
