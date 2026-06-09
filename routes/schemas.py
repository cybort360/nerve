"""Pydantic request/response models for the NERVE API.

These are the typed contracts at the HTTP boundary; route handlers accept and
return these (or core state models) so FastAPI validates and serializes them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from state.models import Action, Event, Mission, MissionType, Snapshot, Task


class CreateMissionRequest(BaseModel):
    """Body for ``POST /missions``."""

    goal: str = Field(min_length=1)
    mission_type: MissionType = "GENERAL"


class CreateMissionResponse(BaseModel):
    """Response for ``POST /missions``."""

    mission_id: str
    status: str


class MissionStateResponse(BaseModel):
    """Aggregate mission view for ``GET /missions/{id}`` (dashboard payload)."""

    mission: Mission
    tasks: list[Task]
    recent_events: list[Event]
    latest_snapshot: Snapshot | None = None
    pending_actions: list[Action] = Field(default_factory=list)
    active_failures: list[dict] = Field(default_factory=list)
    risk: float | None = None
    risk_breakdown: dict | None = None


class EventsPageResponse(BaseModel):
    """Paginated event log for ``GET /missions/{id}/events``."""

    events: list[Event]
    total: int
    limit: int
    offset: int


class ApproveRequest(BaseModel):
    """Body for ``POST /actions/{id}/approve``."""

    approved_by: str = Field(min_length=1)


class RejectRequest(BaseModel):
    """Body for ``POST /actions/{id}/reject``."""

    approved_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
