"""Tests for incident workflow belief/metric emissions and contradict ordering.

Covers:
- write_belief "root_cause" → contradict: get_beliefs reflects latest op/confidence,
  belief_id is stable, and version is bumped.
- write_belief write → confirm chain: version sequence and op values are correct.
- Resolver emits ``record_metric`` on each poll (seeded via direct calls).
"""

from __future__ import annotations

import pytest

from state import database as db
from state.models import Belief


# --------------------------------------------------------------------------- #
# Contradict ordering — the signature "watch a belief get overwritten" flow
# --------------------------------------------------------------------------- #
async def test_write_then_contradict_reflects_latest_state(mock_db):
    """After write → contradict, get_beliefs returns the contradicted belief."""
    mission = await db.create_mission("checkout errors", "INCIDENT_RESPONSE")

    first = await db.write_belief(
        mission.mission_id, "root_cause", "Root cause",
        "deploy #4827 · main", confidence=0.92, op="write",
    )

    second = await db.write_belief(
        mission.mission_id, "root_cause", "Root cause",
        "#4827 vs #4830 ?", confidence=0.41, op="contradict",
    )

    # Version is bumped.
    assert second.version == first.version + 1
    # belief_id is stable across updates.
    assert second.belief_id == first.belief_id
    # get_beliefs returns the LATEST value.
    beliefs = await db.get_beliefs(mission.mission_id)
    assert len(beliefs) == 1
    latest = beliefs[0]
    assert latest.key == "root_cause"
    assert latest.op == "contradict"
    assert latest.confidence == 0.41
    assert latest.value == "#4827 vs #4830 ?"


async def test_write_contradict_then_confirm_version_sequence(mock_db):
    """write → contradict → confirm produces versions 0, 1, 2 with correct ops."""
    mission = await db.create_mission("checkout errors", "INCIDENT_RESPONSE")
    mid = mission.mission_id

    b0 = await db.write_belief(mid, "root_cause", "Root cause", "deploy #4827 · main", confidence=0.92, op="write")
    b1 = await db.write_belief(mid, "root_cause", "Root cause", "#4827 vs #4830 ?", confidence=0.41, op="contradict")
    b2 = await db.write_belief(mid, "root_cause", "Root cause", "deploy #4827", confidence=0.9, op="confirm")

    assert b0.version == 0
    assert b1.version == 1
    assert b2.version == 2

    # Stable belief_id throughout the entire chain.
    assert b0.belief_id == b1.belief_id == b2.belief_id

    beliefs = await db.get_beliefs(mid)
    assert len(beliefs) == 1
    latest = beliefs[0]
    assert latest.op == "confirm"
    assert latest.confidence == 0.9
    assert latest.value == "deploy #4827"


async def test_contradict_emits_belief_updated_event_with_correct_op(mock_db):
    """BELIEF_UPDATED event emitted on contradict carries op='contradict'."""
    mission = await db.create_mission("g", "INCIDENT_RESPONSE")
    mid = mission.mission_id

    await db.write_belief(mid, "root_cause", "Root cause", "v1", confidence=0.9, op="write")
    await db.write_belief(mid, "root_cause", "Root cause", "v2?", confidence=0.4, op="contradict")

    events = await db.get_recent_events_for_mission(mid)
    belief_events = sorted(
        [e for e in events if e.event_type == "BELIEF_UPDATED"],
        key=lambda e: e.created_at,
    )
    assert len(belief_events) == 2
    assert belief_events[1].payload["op"] == "contradict"
    assert belief_events[1].payload["confidence"] == 0.4
    assert belief_events[1].payload["version"] == 1


# --------------------------------------------------------------------------- #
# Metric series from the recovery loop
# --------------------------------------------------------------------------- #
async def test_record_metric_builds_recovery_series(mock_db):
    """Simulates what the resolver does: append one sample per poll."""
    mission = await db.create_mission("checkout errors", "INCIDENT_RESPONSE")
    mid = mission.mission_id

    # Initial spike (from workflow detection).
    await db.record_metric(mid, "checkout · error rate", 34.0, unit="%", baseline=2.0)
    # Recovery curve (as resolver polls).
    for rate_pct in [28.5, 15.0, 4.0, 2.1, 2.0]:
        await db.record_metric(mid, "checkout · error rate", rate_pct, unit="%", baseline=2.0)

    series = await db.get_metric_series(mid)
    assert len(series) == 6
    # Oldest first.
    assert series[0].value == 34.0
    assert series[-1].value == 2.0
    # All carry the baseline.
    assert all(s.baseline == 2.0 for s in series)
    assert all(s.unit == "%" for s in series)


# --------------------------------------------------------------------------- #
# Multi-belief independence during the workflow run
# --------------------------------------------------------------------------- #
async def test_multiple_belief_keys_written_independently(mock_db):
    """anomaly, root_cause, blast, plan, status written under different keys."""
    mission = await db.create_mission("checkout incident", "INCIDENT_RESPONSE")
    mid = mission.mission_id

    expected = [
        ("anomaly", "Anomaly", "checkout error rate elevated", 0.94, "write"),
        ("root_cause", "Root cause", "deploy #42 · main", 0.92, "write"),
        ("blast", "Blast radius", "checkout traffic affected", 0.9, "write"),
        ("plan", "Plan", "rollback main", 0.92, "write"),
        ("status", "Status", "recovered", 0.97, "confirm"),
    ]

    for key, label, value, confidence, op in expected:
        await db.write_belief(mid, key, label, value, confidence=confidence, op=op)

    beliefs = await db.get_beliefs(mid)
    assert len(beliefs) == len(expected)
    keys = {b.key for b in beliefs}
    assert keys == {"anomaly", "root_cause", "blast", "plan", "status"}
    # Each belief at version 0 (written once).
    assert all(b.version == 0 for b in beliefs)
