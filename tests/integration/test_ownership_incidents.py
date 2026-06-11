"""SP2: demo/webhook incidents get the right owner."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import config
from failure_engine.demo_scenario import DemoScenario
from routes import webhooks
from routes.webhooks import DynatraceWebhookPayload
from state import database as db


def _fake_app(orchestrator=None):
    """Return a minimal app object with a stubbed orchestrator on state."""
    orch = orchestrator or SimpleNamespace(run_incident=AsyncMock())
    state = SimpleNamespace(orchestrator=orch)
    return SimpleNamespace(state=state)


def _open_payload(problem_id: str = "P-OWN-1") -> DynatraceWebhookPayload:
    return DynatraceWebhookPayload(problem_id=problem_id, state="OPEN", title="checkout errors")


async def test_demo_mission_owned_by_clicker(mock_db, monkeypatch):
    monkeypatch.setattr(config.settings, "demo_mode", True)
    monkeypatch.setattr(config.settings, "failure_engine_enabled", True)
    owner = await db.create_user("clicker@x.io", "h")
    scenario = DemoScenario(owner_id=owner.user_id)
    mid = await scenario.prepare()
    m = await db.get_mission(mid)
    assert m.owner_id == owner.user_id


async def test_webhook_incident_owned_by_configured_owner(mock_db, monkeypatch):
    owner = await db.create_user("oncall@x.io", "h")
    monkeypatch.setattr(config.settings, "incidents_owner_email", "oncall@x.io")
    app = _fake_app()
    resp = await webhooks._handle_problem_open(app, _open_payload("P-OWN-2"))
    assert resp["status"] == "mission_created"
    m = await db.get_mission(resp["mission_id"])
    assert m is not None
    assert m.owner_id == owner.user_id


async def test_webhook_incident_unowned_when_unset(mock_db, monkeypatch):
    monkeypatch.setattr(config.settings, "incidents_owner_email", "")
    app = _fake_app()
    resp = await webhooks._handle_problem_open(app, _open_payload("P-OWN-3"))
    assert resp["status"] == "mission_created"
    m = await db.get_mission(resp["mission_id"])
    assert m is not None
    assert m.owner_id is None


async def test_webhook_dispatches_to_run_incident(mock_db, monkeypatch):
    owner = await db.create_user("oncall@x.io", "h")
    monkeypatch.setattr(config.settings, "incidents_owner_email", "oncall@x.io")
    orch = SimpleNamespace(run_incident=AsyncMock())
    app = _fake_app(orchestrator=orch)
    payload = _open_payload("P-DISPATCH-1")

    resp = await webhooks._handle_problem_open(app, payload)

    assert resp["status"] == "mission_created"
    mission_id = resp["mission_id"]
    orch.run_incident.assert_awaited_once_with(mission_id, payload.problem_id, owner.user_id)
