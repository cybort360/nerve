"""Unit tests for the Dynatrace webhook receiver (routes/webhooks.py).

Handlers are called directly with a fake Request (its app.state holds a mocked
orchestrator) and the in-memory state layer, so the real auth + dispatch logic
is exercised without a live server or Dynatrace.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from config import settings
from routes import webhooks
from routes.webhooks import DynatraceWebhookPayload
from state import database as db

SECRET = "s3cret-shared-value"


@pytest.fixture
def configured(monkeypatch):
    """Configure the webhook secret for the duration of a test."""
    monkeypatch.setattr(settings, "dynatrace_webhook_secret", SECRET)


def _request(orchestrator=None):
    state = SimpleNamespace(orchestrator=orchestrator or SimpleNamespace(run_mission=AsyncMock()))
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _open_payload(problem_id="P-1") -> DynatraceWebhookPayload:
    return DynatraceWebhookPayload(problem_id=problem_id, state="OPEN", title="checkout errors")


# --------------------------------------------------------------------------- #
# PROBLEM_OPEN -> mission
# --------------------------------------------------------------------------- #
async def test_valid_open_creates_mission_and_starts_loop(mock_db, configured):
    orch = SimpleNamespace(run_mission=AsyncMock())
    resp = await webhooks.dynatrace_webhook(_open_payload("P-1"), _request(orch), x_dynatrace_signature=SECRET)

    assert resp["status"] == "mission_created"
    mid = resp["mission_id"]
    mission = await db.get_mission(mid)
    assert mission is not None
    assert mission.mission_type == "INCIDENT_RESPONSE"
    assert mission.context["problem_id"] == "P-1"
    orch.run_mission.assert_awaited_once_with(mid)

    events = [(e.event_type, e.source) for e in await db.get_recent_events_for_mission(mid)]
    assert ("DYNATRACE_PROBLEM_OPEN", "dynatrace_webhook") in events
    assert ("MISSION_CREATED", "orchestrator") in events


# --------------------------------------------------------------------------- #
# Signature validation
# --------------------------------------------------------------------------- #
async def test_invalid_signature_returns_401(mock_db, configured):
    with pytest.raises(HTTPException) as exc:
        await webhooks.dynatrace_webhook(_open_payload(), _request(), x_dynatrace_signature="wrong")
    assert exc.value.status_code == 401


async def test_missing_signature_returns_401(mock_db, configured):
    with pytest.raises(HTTPException) as exc:
        await webhooks.dynatrace_webhook(_open_payload(), _request(), x_dynatrace_signature=None)
    assert exc.value.status_code == 401


async def test_unconfigured_secret_returns_503(mock_db, monkeypatch):
    monkeypatch.setattr(settings, "dynatrace_webhook_secret", "")
    with pytest.raises(HTTPException) as exc:
        await webhooks.dynatrace_webhook(_open_payload(), _request(), x_dynatrace_signature="anything")
    assert exc.value.status_code == 503


# --------------------------------------------------------------------------- #
# PROBLEM_RESOLVED -> resolution event on the originating mission
# --------------------------------------------------------------------------- #
async def test_resolved_emits_event_on_matching_mission(mock_db, configured):
    # A mission opened earlier for this problem.
    mission = await db.create_mission("incident", "INCIDENT_RESPONSE", {"problem_id": "P-2"})
    payload = DynatraceWebhookPayload(problem_id="P-2", state="RESOLVED", title="checkout errors")

    resp = await webhooks.dynatrace_webhook(payload, _request(), x_dynatrace_signature=SECRET)

    assert resp["status"] == "resolution_logged"
    assert resp["mission_id"] == mission.mission_id
    events = [(e.event_type, e.source) for e in await db.get_recent_events_for_mission(mission.mission_id)]
    assert ("DYNATRACE_RESOLVED", "dynatrace_webhook") in events


async def test_resolved_with_no_matching_mission(mock_db, configured):
    payload = DynatraceWebhookPayload(problem_id="UNKNOWN", state="RESOLVED")
    resp = await webhooks.dynatrace_webhook(payload, _request(), x_dynatrace_signature=SECRET)
    assert resp["status"] == "no_mission"


# --------------------------------------------------------------------------- #
# Demo test endpoint
# --------------------------------------------------------------------------- #
async def test_test_endpoint_works_in_demo_mode(mock_db, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    orch = SimpleNamespace(run_mission=AsyncMock())
    resp = await webhooks.dynatrace_webhook_test(_request(orch))
    assert resp["status"] == "mission_created"
    orch.run_mission.assert_awaited_once()
    mission = await db.get_mission(resp["mission_id"])
    assert mission.context["problem_id"] == "DEMO-PROBLEM-1"


async def test_test_endpoint_blocked_outside_demo_mode(mock_db, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    with pytest.raises(HTTPException) as exc:
        await webhooks.dynatrace_webhook_test(_request())
    assert exc.value.status_code == 403
