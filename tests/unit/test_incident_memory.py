"""Unit tests for IncidentMemory and its workflow wiring.

The Vertex AI Memory Bank client is mocked via the injectable ``client`` seam —
no real Vertex AI / network. Covers the disabled no-op behavior, enabled
store/retrieve with correct metadata, and that retrieved memories feed the
reasoning prompt.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import settings
from mcp_tools.dynatrace import DynatraceProblemDetail, ServiceMetrics
from mcp_tools.gitlab import GitLabDeployment
from memory.incident_memory import IncidentMemory, MemoryEntry
from modules.incident_autopilot.workflow import IncidentAutopilotWorkflow


@pytest.fixture
def memory_enabled(monkeypatch):
    monkeypatch.setattr(settings, "memory_bank_enabled", True)
    monkeypatch.setattr(settings, "memory_bank_id", "projects/p/locations/us/memoryBanks/demo")


def _problem() -> DynatraceProblemDetail:
    return DynatraceProblemDetail(
        problem_id="P-1", title="Checkout error spike", severity="AVAILABILITY", status="OPEN",
        impacted_services=["checkout"], start_time=datetime(2026, 6, 3),
    )


def _deployment() -> GitLabDeployment:
    return GitLabDeployment(id=42, status="success", ref="main", sha="abc123", environment="production")


# --------------------------------------------------------------------------- #
# Disabled: no-ops
# --------------------------------------------------------------------------- #
async def test_disabled_retrieve_returns_empty():
    # memory flags default off
    client = SimpleNamespace(create_memory=AsyncMock(), retrieve_memories=AsyncMock())
    mem = IncidentMemory(client=client)
    assert mem.enabled is False
    assert await mem.retrieve_similar("checkout errors") == []
    client.retrieve_memories.assert_not_awaited()


async def test_disabled_store_is_noop():
    client = SimpleNamespace(create_memory=AsyncMock(), retrieve_memories=AsyncMock())
    mem = IncidentMemory(client=client)
    await mem.store_incident("m-1", _problem(), _deployment(), "deploy 42 caused it", {"recommendation": "rollback"})
    client.create_memory.assert_not_awaited()  # no-op when disabled


# --------------------------------------------------------------------------- #
# Enabled: store with correct metadata
# --------------------------------------------------------------------------- #
async def test_enabled_store_calls_api_with_metadata(memory_enabled):
    client = SimpleNamespace(create_memory=AsyncMock(return_value="mem-123"), retrieve_memories=AsyncMock())
    mem = IncidentMemory(client=client)
    outcome = {"recommendation": "rollback", "resolution_time_seconds": 180, "changed_files": ["payment_processor.py"]}

    await mem.store_incident("m-1", _problem(), _deployment(), "deploy 42 correlates", outcome)

    client.create_memory.assert_awaited_once()
    content, metadata = client.create_memory.await_args.args
    assert "Checkout error spike" in content
    assert metadata["affected_service"] == "checkout"
    assert metadata["changed_files"] == ["payment_processor.py"]
    assert metadata["recommendation"] == "rollback"
    assert metadata["resolution_time_seconds"] == 180
    assert metadata["error_pattern"] == "Checkout error spike"


# --------------------------------------------------------------------------- #
# Enabled: retrieve maps results into typed entries
# --------------------------------------------------------------------------- #
async def test_enabled_retrieve_maps_and_limits(memory_enabled):
    raw = [
        {"memory_id": "m1", "summary": "checkout deploy rollback", "metadata": {"affected_service": "checkout"}, "relevance_score": 0.9, "created_at": "2026-05-01T00:00:00"},
        {"id": "m2", "fact": "payments outage", "score": 0.7},
    ]
    client = SimpleNamespace(create_memory=AsyncMock(), retrieve_memories=AsyncMock(return_value=raw))
    mem = IncidentMemory(client=client)

    entries = await mem.retrieve_similar("checkout errors", limit=3)

    client.retrieve_memories.assert_awaited_once_with("checkout errors", 3)
    assert len(entries) == 2
    assert all(isinstance(e, MemoryEntry) for e in entries)
    assert entries[0].memory_id == "m1" and entries[0].relevance_score == 0.9
    assert entries[1].memory_id == "m2" and entries[1].summary == "payments outage"


async def test_retrieve_swallows_client_errors(memory_enabled):
    client = SimpleNamespace(retrieve_memories=AsyncMock(side_effect=RuntimeError("bank down")), create_memory=AsyncMock())
    mem = IncidentMemory(client=client)
    assert await mem.retrieve_similar("x") == []  # degrades cleanly


# --------------------------------------------------------------------------- #
# Workflow wiring: retrieved memories feed the reasoning prompt
# --------------------------------------------------------------------------- #
def test_retrieved_memories_appear_in_reasoning_prompt():
    memories = [
        MemoryEntry(
            memory_id="m1", summary="Prior checkout incident fixed by rollback",
            metadata={"affected_service": "checkout", "changed_files": ["payment_processor.py"], "recommendation": "rollback"},
            relevance_score=0.9,
        )
    ]
    prompt = IncidentAutopilotWorkflow._reasoning_prompt(_problem(), None, [_deployment()], False, memories)
    assert "Past incidents involving similar services or files" in prompt
    assert "Prior checkout incident fixed by rollback" in prompt
    assert "payment_processor.py" in prompt


def test_no_memories_omits_section():
    prompt = IncidentAutopilotWorkflow._reasoning_prompt(_problem(), None, [_deployment()], False, [])
    assert "Past incidents" not in prompt


async def test_workflow_retrieves_memories_before_reasoning(mock_db, memory_enabled):
    """run() Step 2.5 calls retrieve_similar and stashes results for reasoning."""
    from state import database as db

    mission = await db.create_mission("incident", "INCIDENT_RESPONSE")
    metrics = ServiceMetrics(service_id="checkout", error_rate=0.02, from_time=datetime(2026, 6, 3), to_time=datetime(2026, 6, 3))
    dynatrace = SimpleNamespace(
        get_problem_details=AsyncMock(return_value=_problem()),
        get_service_metrics=AsyncMock(return_value=metrics),
    )
    gitlab = SimpleNamespace(list_recent_deployments=AsyncMock(return_value=[_deployment()]),
                             create_issue=AsyncMock(return_value=SimpleNamespace(iid=7, web_url="u")))
    fake_client = SimpleNamespace(
        retrieve_memories=AsyncMock(return_value=[{"memory_id": "m1", "summary": "prior checkout fix"}]),
        create_memory=AsyncMock(),
    )
    reason = AsyncMock(return_value=__import_correlation())
    resolver = SimpleNamespace(monitor_resolution=AsyncMock())
    wf = IncidentAutopilotWorkflow(
        dynatrace, gitlab, reason=reason, resolver=resolver, project_id="1",
        memory=IncidentMemory(client=fake_client),
    )

    await wf.run("P-1", mission.mission_id)

    fake_client.retrieve_memories.assert_awaited_once()
    assert wf._memories and wf._memories[0].memory_id == "m1"
    events = [e.event_type for e in await db.get_recent_events_for_mission(mission.mission_id)]
    assert "MEMORY_RETRIEVED" in events


def __import_correlation():
    from modules.incident_autopilot.workflow import CorrelationResult

    return CorrelationResult(correlated_deployment=_deployment(), confidence=0.9, reasoning="r", recommendation="rollback")
