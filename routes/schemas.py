"""Pydantic request/response models for the NERVE API.

These are the typed contracts at the HTTP boundary; route handlers accept and
return these (or core state models) so FastAPI validates and serializes them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from state.models import (
    Action,
    Belief,
    Event,
    MetricSample,
    Mission,
    MissionStatus,
    MissionType,
    Snapshot,
    Task,
)


class SignupRequest(BaseModel):
    """Body for POST /auth/signup."""

    email: str = Field(min_length=3)
    password: str = Field(min_length=1)


class LoginRequest(BaseModel):
    """Body for POST /auth/login."""

    email: str = Field(min_length=3)
    password: str = Field(min_length=1)


class UserResponse(BaseModel):
    """Public user view."""

    user_id: str
    email: str


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
    beliefs: list[Belief] = Field(default_factory=list)
    metric_series: list[MetricSample] = Field(default_factory=list)


class MissionSummary(BaseModel):
    """Compact mission summary for the fleet roster (``GET /missions``)."""

    mission_id: str
    goal: str
    mission_type: MissionType
    status: MissionStatus
    updated_at: datetime


class MissionListResponse(BaseModel):
    """Response for ``GET /missions`` fleet listing."""

    missions: list[MissionSummary]


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


class SettingsUpdateRequest(BaseModel):
    """Body for PUT /settings.

    Omitted fields are left unchanged; an empty secret keeps the stored one.
    """

    tavily_api_key: str | None = None
    gitlab_url: str | None = None
    gitlab_token: str | None = None
    gitlab_project_id: str | None = None
    dynatrace_environment_url: str | None = None
    dynatrace_api_token: str | None = None
    dynatrace_webhook_secret: str | None = None


class SettingsResponse(BaseModel):
    """Current settings; secret fields are masked (never the raw token)."""

    tavily_api_key: str = ""
    gitlab_url: str = ""
    gitlab_token: str = ""
    gitlab_project_id: str = ""
    dynatrace_environment_url: str = ""
    dynatrace_api_token: str = ""
    dynatrace_webhook_secret: str = ""
