"""Unit tests for beliefs (working memory), metric samples, and fleet listing.

Covers:
- write_belief: upsert semantics, version bumping, belief_id stability, event emission.
- get_beliefs: ordering by updated_at ascending.
- record_metric / get_metric_series: append, ordering, limit.
- GET /missions/{id}: beliefs and metric_series present in the response.
- GET /missions: fleet roster returns mission summaries.

All tests use the ``mock_db`` fixture (in-memory mongomock) and call handlers
directly using the same pattern as test_webhooks.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from routes import missions as mission_routes
from state import database as db
from state.models import Belief, MetricSample


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _fake_request() -> SimpleNamespace:
    """Build a minimal fake FastAPI Request with no failure engine."""
    state = SimpleNamespace(
        orchestrator=SimpleNamespace(run_mission=AsyncMock()),
        failure_engine=None,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


# --------------------------------------------------------------------------- #
# write_belief — upsert + version bump
# --------------------------------------------------------------------------- #
async def test_write_belief_creates_version_zero(mock_db):
    mission = await db.create_mission("svc 500s", "INCIDENT_RESPONSE")
    belief = await db.write_belief(
        mission.mission_id, "root_cause", "Root cause", "#4827 · conn-pool", confidence=0.8
    )

    assert isinstance(belief, Belief)
    assert belief.version == 0
    assert belief.key == "root_cause"
    assert belief.label == "Root cause"
    assert belief.value == "#4827 · conn-pool"
    assert belief.confidence == 0.8
    assert belief.op == "write"


async def test_write_belief_second_call_bumps_version_keeps_id(mock_db):
    mission = await db.create_mission("svc 500s", "INCIDENT_RESPONSE")
    first = await db.write_belief(
        mission.mission_id, "root_cause", "Root cause", "v1", confidence=0.6
    )

    second = await db.write_belief(
        mission.mission_id, "root_cause", "Root cause", "v2", confidence=0.9, op="update"
    )

    assert second.version == 1
    assert second.belief_id == first.belief_id
    assert second.value == "v2"
    assert second.op == "update"


async def test_write_belief_emits_belief_updated_event(mock_db):
    mission = await db.create_mission("goal", "GENERAL")
    await db.write_belief(
        mission.mission_id, "status", "Status", "degraded", confidence=0.7
    )

    events = await db.get_recent_events_for_mission(mission.mission_id)
    belief_events = [e for e in events if e.event_type == "BELIEF_UPDATED"]
    assert len(belief_events) == 1
    payload = belief_events[0].payload
    assert payload["key"] == "status"
    assert payload["value"] == "degraded"
    assert payload["confidence"] == 0.7
    assert payload["version"] == 0


async def test_write_belief_second_event_has_incremented_version(mock_db):
    mission = await db.create_mission("goal", "GENERAL")
    await db.write_belief(mission.mission_id, "k", "K", "v1")
    await db.write_belief(mission.mission_id, "k", "K", "v2", op="confirm")

    events = await db.get_recent_events_for_mission(mission.mission_id)
    belief_events = sorted(
        [e for e in events if e.event_type == "BELIEF_UPDATED"],
        key=lambda e: e.created_at,
    )
    assert belief_events[1].payload["version"] == 1
    assert belief_events[1].payload["op"] == "confirm"


async def test_write_belief_different_keys_are_independent(mock_db):
    mission = await db.create_mission("g", "GENERAL")
    await db.write_belief(mission.mission_id, "key_a", "A", "va")
    b2 = await db.write_belief(mission.mission_id, "key_b", "B", "vb")

    assert b2.version == 0


# --------------------------------------------------------------------------- #
# get_beliefs — ordering
# --------------------------------------------------------------------------- #
async def test_get_beliefs_returns_ordered_by_updated_at(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    await db.write_belief(mission.mission_id, "alpha", "Alpha", "a1")
    await db.write_belief(mission.mission_id, "beta", "Beta", "b1")
    # Update alpha last so it should appear after beta in updated_at order.
    await db.write_belief(mission.mission_id, "alpha", "Alpha", "a2")

    beliefs = await db.get_beliefs(mission.mission_id)
    assert len(beliefs) == 2
    # beta was last written before alpha's second write → beta < alpha in updated_at
    assert beliefs[0].key == "beta"
    assert beliefs[1].key == "alpha"


async def test_get_beliefs_empty_for_new_mission(mock_db):
    mission = await db.create_mission("g", "GENERAL")
    assert await db.get_beliefs(mission.mission_id) == []


async def test_get_beliefs_isolated_between_missions(mock_db):
    m1 = await db.create_mission("g1", "GENERAL")
    m2 = await db.create_mission("g2", "GENERAL")
    await db.write_belief(m1.mission_id, "k", "K", "v")

    assert await db.get_beliefs(m2.mission_id) == []


# --------------------------------------------------------------------------- #
# record_metric + get_metric_series
# --------------------------------------------------------------------------- #
async def test_record_metric_inserts_sample(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    await db.record_metric(mission.mission_id, "checkout · error rate", 3.2, unit="%")

    series = await db.get_metric_series(mission.mission_id)
    assert len(series) == 1
    assert isinstance(series[0], MetricSample)
    assert series[0].label == "checkout · error rate"
    assert series[0].value == 3.2
    assert series[0].unit == "%"


async def test_record_metric_emits_metric_sample_event(mock_db):
    mission = await db.create_mission("g", "GENERAL")
    await db.record_metric(mission.mission_id, "latency", 120.0, unit="ms", baseline=80.0)

    events = await db.get_recent_events_for_mission(mission.mission_id)
    metric_events = [e for e in events if e.event_type == "METRIC_SAMPLE"]
    assert len(metric_events) == 1
    payload = metric_events[0].payload
    assert payload["label"] == "latency"
    assert payload["value"] == 120.0
    assert payload["unit"] == "ms"
    assert payload["baseline"] == 80.0


async def test_get_metric_series_returns_oldest_to_newest(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    for i in range(5):
        await db.record_metric(mission.mission_id, "err", float(i), unit="%")

    series = await db.get_metric_series(mission.mission_id)
    assert len(series) == 5
    assert [s.value for s in series] == [0.0, 1.0, 2.0, 3.0, 4.0]


async def test_get_metric_series_respects_limit(mock_db):
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    for i in range(10):
        await db.record_metric(mission.mission_id, "err", float(i))

    series = await db.get_metric_series(mission.mission_id, limit=4)
    # Should be the 4 most-recent samples, in oldest→newest order.
    assert len(series) == 4
    assert [s.value for s in series] == [6.0, 7.0, 8.0, 9.0]


async def test_get_metric_series_empty_for_new_mission(mock_db):
    mission = await db.create_mission("g", "GENERAL")
    assert await db.get_metric_series(mission.mission_id) == []


async def test_metric_series_isolated_between_missions(mock_db):
    m1 = await db.create_mission("g1", "GENERAL")
    m2 = await db.create_mission("g2", "GENERAL")
    await db.record_metric(m1.mission_id, "err", 1.0)

    assert await db.get_metric_series(m2.mission_id) == []


# --------------------------------------------------------------------------- #
# GET /missions/{id} — response includes beliefs and metric_series
# --------------------------------------------------------------------------- #
async def test_read_mission_state_includes_beliefs_and_metrics(mock_db):
    mission = await db.create_mission("checkout errors", "INCIDENT_RESPONSE")
    await db.write_belief(mission.mission_id, "root_cause", "Root cause", "deploy #42")
    await db.record_metric(mission.mission_id, "error rate", 5.5, unit="%")

    resp = await mission_routes.read_mission_state(mission.mission_id, _fake_request())

    assert len(resp.beliefs) == 1
    assert resp.beliefs[0].key == "root_cause"
    assert resp.beliefs[0].value == "deploy #42"

    assert len(resp.metric_series) == 1
    assert resp.metric_series[0].label == "error rate"
    assert resp.metric_series[0].value == 5.5


async def test_read_mission_state_beliefs_and_metrics_empty_when_none(mock_db):
    mission = await db.create_mission("g", "GENERAL")

    resp = await mission_routes.read_mission_state(mission.mission_id, _fake_request())

    assert resp.beliefs == []
    assert resp.metric_series == []


# --------------------------------------------------------------------------- #
# GET /missions — fleet roster
# --------------------------------------------------------------------------- #
async def test_list_missions_returns_summaries(mock_db):
    m1 = await db.create_mission("incident A", "INCIDENT_RESPONSE")
    m2 = await db.create_mission("general task", "GENERAL")

    resp = await mission_routes.list_missions()

    ids = {s.mission_id for s in resp.missions}
    assert m1.mission_id in ids
    assert m2.mission_id in ids

    for summary in resp.missions:
        assert summary.goal in ("incident A", "general task")
        assert summary.status == "pending"
        assert summary.updated_at is not None


async def test_list_missions_ordered_by_most_recent_updated_at(mock_db):
    m1 = await db.create_mission("first", "GENERAL")
    m2 = await db.create_mission("second", "GENERAL")
    # Advance m1's updated_at by updating its status.
    await db.update_mission_status(m1.mission_id, "executing")

    resp = await mission_routes.list_missions()

    # m1 was updated more recently → should appear first.
    assert resp.missions[0].mission_id == m1.mission_id
    assert resp.missions[1].mission_id == m2.mission_id


async def test_list_missions_empty_when_none(mock_db):
    resp = await mission_routes.list_missions()
    assert resp.missions == []


async def test_list_missions_summary_fields(mock_db):
    mission = await db.create_mission("check latency", "INCIDENT_RESPONSE")

    resp = await mission_routes.list_missions()
    assert len(resp.missions) == 1
    s = resp.missions[0]
    assert s.mission_id == mission.mission_id
    assert s.goal == "check latency"
    assert s.mission_type == "INCIDENT_RESPONSE"
    assert s.status == "pending"
