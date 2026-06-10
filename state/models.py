"""Pydantic v2 models for every NERVE MongoDB collection.

These models are the contract: data that does not validate does not enter the
system (CLAUDE.md invariant 5). One model per collection, plus the
:class:`MissionState` aggregate that agents use to observe current state.

Note on timestamps:
    CLAUDE.md's general rule mentions ``created_at`` and ``updated_at`` on every
    document, but the explicit per-collection schemas only define ``updated_at``
    for ``missions`` and ``tasks``. The explicit schemas are the contract, so
    ``Event``, ``Action``, and ``Snapshot`` carry ``created_at`` only. Flagged
    here per the "never deviate without flagging" rule.

    ``Belief`` tracks both ``created_at`` and ``updated_at`` because write_belief
    upserts and bumps version; we need updated_at to order beliefs consistently.
    ``MetricSample`` is append-only so only ``created_at`` is needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Status / enum type aliases (Literal types per ARCHITECTURE.md section 3).
# --------------------------------------------------------------------------- #
MissionType: TypeAlias = Literal["INCIDENT_RESPONSE", "GENERAL"]
MissionStatus: TypeAlias = Literal[
    "pending", "planning", "executing", "replanning", "resolved", "failed"
]
AgentRole: TypeAlias = Literal["planner", "execution", "risk", "auditor"]
TaskStatus: TypeAlias = Literal[
    "pending", "in_progress", "completed", "failed", "retrying"
]
EventSource: TypeAlias = Literal[
    "orchestrator", "agent", "mcp", "failure_engine", "user", "dynatrace_webhook"
]
ActionType: TypeAlias = Literal[
    "gitlab_issue",
    "gitlab_rollback",
    "gitlab_mr",
    "dynatrace_query",
    "human_approval_request",
]
ActionStatus: TypeAlias = Literal[
    "pending", "approved", "executed", "rejected", "failed"
]


def _uuid() -> str:
    """Generate a uuid4 string for use as a document identifier."""
    return str(uuid.uuid4())


class _BaseDoc(BaseModel):
    """Shared config for all collection documents.

    ``populate_by_name`` matches ARCHITECTURE.md; ``extra="ignore"`` lets us
    validate raw Mongo documents (which carry an ``_id``) without error.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Mission(_BaseDoc):
    """A high-level goal NERVE is actively pursuing."""

    mission_id: str = Field(default_factory=_uuid)
    goal: str
    mission_type: MissionType
    status: MissionStatus = "pending"
    context: dict = Field(default_factory=dict)
    task_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Task(_BaseDoc):
    """A single unit of work owned by one agent role."""

    task_id: str = Field(default_factory=_uuid)
    mission_id: str
    agent_role: AgentRole
    description: str
    tool: str | None = None
    tool_args: dict = Field(default_factory=dict)
    status: TaskStatus = "pending"
    depends_on: list[str] = Field(default_factory=list)
    result: dict = Field(default_factory=dict)
    error: str | None = None
    retry_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Event(_BaseDoc):
    """An append-only audit record. Every meaningful action emits one."""

    event_id: str = Field(default_factory=_uuid)
    mission_id: str
    task_id: str | None = None
    event_type: str
    payload: dict = Field(default_factory=dict)
    source: EventSource
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Action(_BaseDoc):
    """An external side effect requiring human approval before execution."""

    action_id: str = Field(default_factory=_uuid)
    mission_id: str
    action_type: ActionType
    payload: dict = Field(default_factory=dict)
    status: ActionStatus = "pending"
    approved_by: str | None = None
    executed_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class User(_BaseDoc):
    """A registered account."""

    user_id: str = Field(default_factory=_uuid)
    email: str
    password_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Snapshot(_BaseDoc):
    """A point-in-time summary of mission state at the end of a loop cycle."""

    snapshot_id: str = Field(default_factory=_uuid)
    mission_id: str
    cycle: int
    state_summary: dict = Field(default_factory=dict)
    failure_injections_active: list[dict] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Belief(_BaseDoc):
    """A fact NERVE currently believes, with confidence — the working memory.

    Beliefs are upserted by (mission_id, key); ``version`` increments on each
    write so observers can detect staleness. ``op`` describes the nature of the
    latest update (write/update/confirm/contradict).
    """

    belief_id: str = Field(default_factory=_uuid)
    mission_id: str
    key: str
    label: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    op: Literal["write", "update", "confirm", "contradict"] = "write"
    version: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MetricSample(_BaseDoc):
    """One time-series sample of a mission's headline metric (sparkline).

    Samples are append-only; no ``updated_at`` because they are never mutated.
    """

    mission_id: str
    label: str
    value: float
    unit: str = ""
    baseline: float | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MissionState(BaseModel):
    """Aggregate view of a mission for agents to observe.

    Bundles the mission with its tasks, most-recent events, current beliefs
    (working memory), and recent metric samples so agents have a single object
    to reason over without issuing their own queries.
    """

    model_config = ConfigDict(extra="ignore")

    mission: Mission
    tasks: list[Task] = Field(default_factory=list)
    recent_events: list[Event] = Field(default_factory=list)
    beliefs: list[Belief] = Field(default_factory=list)
    metric_series: list[MetricSample] = Field(default_factory=list)
