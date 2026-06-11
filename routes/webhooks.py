"""Dynatrace webhook receiver — real-time incident detection.

Replaces polling for the Incident Autopilot trigger: Dynatrace posts a problem
notification here, NERVE validates a shared secret, and on ``OPEN`` it opens an
INCIDENT_RESPONSE mission and starts the orchestration loop. On ``RESOLVED`` it
records a resolution event against the originating mission. Every handled
webhook is logged to the events collection with ``source="dynatrace_webhook"``.
"""

from __future__ import annotations

import hmac
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from config import settings
from state import database as db

log = structlog.get_logger()
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SOURCE_DYNATRACE_WEBHOOK = "dynatrace_webhook"
SOURCE_ORCHESTRATOR = "orchestrator"
MISSION_TYPE_INCIDENT = "INCIDENT_RESPONSE"

STATE_OPEN = "OPEN"
STATE_RESOLVED = "RESOLVED"

EVENT_MISSION_CREATED = "MISSION_CREATED"
EVENT_DYNATRACE_PROBLEM_OPEN = "DYNATRACE_PROBLEM_OPEN"
EVENT_DYNATRACE_RESOLVED = "DYNATRACE_RESOLVED"

_GOAL_TEMPLATE = "Investigate and resolve Dynatrace problem: {title}"


class DynatraceWebhookPayload(BaseModel):
    """Dynatrace problem notification payload (custom-integration schema).

    The exact JSON shape is controlled by the payload template configured in
    Dynatrace (see docs/dynatrace_webhook_setup.md). ``problem_id`` and ``state``
    are required; ``state`` is Dynatrace's ``{State}`` — ``OPEN`` or ``RESOLVED``.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    problem_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    title: str = ""
    severity: str | None = None
    impact: str | None = None
    url: str | None = None
    impacted_entities: list[dict] = Field(default_factory=list)


def _verify_signature(provided: str | None) -> None:
    """Validate the webhook shared secret in constant time.

    Args:
        provided: Value of the ``X-Dynatrace-Signature`` header.

    Raises:
        HTTPException: 503 if no secret is configured server-side; 401 if the
            header is missing or does not match the configured secret.
    """
    secret = settings.dynatrace_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    if provided is None or not hmac.compare_digest(provided.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="invalid webhook signature")


@router.post("/dynatrace")
async def dynatrace_webhook(
    payload: DynatraceWebhookPayload,
    request: Request,
    x_dynatrace_signature: str | None = Header(default=None),
) -> dict:
    """Receive a (signed) Dynatrace problem notification.

    Args:
        payload: The validated Dynatrace problem notification.
        request: FastAPI request (for the orchestrator on app.state).
        x_dynatrace_signature: Shared-secret header for authentication.

    Returns:
        A small status payload describing what was done.
    """
    _verify_signature(x_dynatrace_signature)
    return await _process_webhook(request.app, payload)


@router.post("/dynatrace/test")
async def dynatrace_webhook_test(request: Request) -> dict:
    """Fire a fake PROBLEM_OPEN through the real handler (DEMO_MODE only).

    Args:
        request: FastAPI request (for the orchestrator on app.state).

    Returns:
        The handler's status payload.

    Raises:
        HTTPException: 403 when demo mode is disabled.
    """
    if not settings.demo_mode:
        raise HTTPException(status_code=403, detail="demo mode disabled")
    log.info("dynatrace_webhook_test_fired")
    return await _process_webhook(request.app, _sample_open_payload())


async def _process_webhook(app: Any, payload: DynatraceWebhookPayload) -> dict:
    """Dispatch a parsed webhook by state (shared by real + test endpoints)."""
    state = payload.state.upper()
    if state == STATE_OPEN:
        return await _handle_problem_open(app, payload)
    if state == STATE_RESOLVED:
        return await _handle_problem_resolved(payload)
    log.info("dynatrace_webhook_ignored", problem_id=payload.problem_id, state=state)
    return {"status": "ignored", "state": state}


async def _handle_problem_open(app: Any, payload: DynatraceWebhookPayload) -> dict:
    """Open an INCIDENT_RESPONSE mission and start its orchestration loop."""
    context = {
        "problem_id": payload.problem_id,
        "severity": payload.severity,
        "impact": payload.impact,
        "url": payload.url,
        "source": SOURCE_DYNATRACE_WEBHOOK,
    }
    goal = _GOAL_TEMPLATE.format(title=payload.title or payload.problem_id)
    owner_id = None
    if settings.incidents_owner_email:
        owner = await db.get_user_by_email(settings.incidents_owner_email)
        if owner is None:
            log.warning("incidents_owner_not_found", email=settings.incidents_owner_email)
        else:
            owner_id = owner.user_id
    mission = await db.create_mission(goal, MISSION_TYPE_INCIDENT, context, owner_id=owner_id)
    await db.emit_event(
        mission.mission_id, EVENT_MISSION_CREATED, {"goal": goal, "problem_id": payload.problem_id}, SOURCE_ORCHESTRATOR
    )
    await db.emit_event(
        mission.mission_id, EVENT_DYNATRACE_PROBLEM_OPEN, payload.model_dump(), SOURCE_DYNATRACE_WEBHOOK
    )
    await app.state.orchestrator.run_incident(mission.mission_id, payload.problem_id, owner_id)
    log.info("dynatrace_problem_opened", mission_id=mission.mission_id, problem_id=payload.problem_id)
    return {"status": "mission_created", "mission_id": mission.mission_id}


async def _handle_problem_resolved(payload: DynatraceWebhookPayload) -> dict:
    """Record a Dynatrace resolution against the originating mission."""
    mission = await db.find_mission_by_problem_id(payload.problem_id)
    if mission is None:
        log.warning("dynatrace_resolved_no_mission", problem_id=payload.problem_id)
        return {"status": "no_mission", "problem_id": payload.problem_id}
    await db.emit_event(
        mission.mission_id, EVENT_DYNATRACE_RESOLVED, payload.model_dump(), SOURCE_DYNATRACE_WEBHOOK
    )
    log.info("dynatrace_problem_resolved", mission_id=mission.mission_id, problem_id=payload.problem_id)
    return {"status": "resolution_logged", "mission_id": mission.mission_id}


def _sample_open_payload() -> DynatraceWebhookPayload:
    """A seeded PROBLEM_OPEN payload for the demo test endpoint."""
    return DynatraceWebhookPayload(
        problem_id="DEMO-PROBLEM-1",
        state=STATE_OPEN,
        title="Elevated error rate on checkout",
        severity="AVAILABILITY",
        impact="SERVICE",
        url="https://demo.live.dynatrace.com/#problems/problemdetails;pid=DEMO-PROBLEM-1",
        impacted_entities=[{"type": "SERVICE", "name": "checkout"}],
    )
