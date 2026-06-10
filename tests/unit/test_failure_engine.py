"""Unit tests for the FailureEngine and its BaseMCPClient integration.

Covers each failure type's behavior via apply_to_mcp_call, the inject/clear/active
lifecycle, the feature-flag gate, and that BaseMCPClient applies the engine's
MCPCallModification (delay, outage error, result transforms).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config import settings
from exceptions import MCPToolCallError
from failure_engine import injector
from failure_engine.injector import FailureEngine, FailureScenario, FailureType
from mcp_tools.dynatrace import DynatraceClient
from mcp_tools.gitlab import GitLabClient
from state import database as db


@pytest.fixture
def enabled(monkeypatch):
    """Enable the failure engine for the duration of a test."""
    monkeypatch.setattr(settings, "failure_engine_enabled", True)
    return True


def _scenario(failure_type: FailureType, target: str, severity: float = 0.5) -> FailureScenario:
    return FailureScenario(failure_type=failure_type, target=target, severity=severity, duration_seconds=0)


class FakeSession:
    """Minimal MCP session returning a canned structured result."""

    def __init__(self, result):
        self._result = result
        self.calls: list = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(structuredContent=self._result, content=[])


def _engine_with(scenario: FailureScenario) -> FailureEngine:
    engine = FailureEngine()
    engine._scenarios.append(scenario)
    return engine


# --------------------------------------------------------------------------- #
# Per-failure-type behavior (apply_to_mcp_call)
# --------------------------------------------------------------------------- #
def test_delayed_signal_sets_delay(enabled):
    engine = _engine_with(_scenario(FailureType.DELAYED_SIGNAL, "get_metrics", severity=0.7))
    mod = engine.apply_to_mcp_call("get_metrics", {})
    assert mod.delay_seconds == pytest.approx(7.0)  # severity * 10


async def test_service_outage_raises_tool_call_error(enabled):
    engine = _engine_with(_scenario(FailureType.SERVICE_OUTAGE, "get_problems"))
    mod = engine.apply_to_mcp_call("get_problems", {})
    assert isinstance(mod.error, MCPToolCallError)
    with pytest.raises(MCPToolCallError):
        await mod.apply_before_call()


def test_contradictory_metrics_scales_error_rate(enabled):
    engine = _engine_with(_scenario(FailureType.CONTRADICTORY_METRICS, "get_metrics"))
    mod = engine.apply_to_mcp_call("get_metrics", {})
    out = mod.apply_to_result({"error_rate": 0.2})
    assert out["_contradictory"] is True
    assert 0.1 * 0.2 <= out["error_rate"] <= 3.0 * 0.2  # multiplied by uniform(0.1, 3.0)


def test_noisy_data_perturbs_metrics(enabled):
    engine = _engine_with(_scenario(FailureType.NOISY_DATA, "get_metrics", severity=0.5))
    mod = engine.apply_to_mcp_call("get_metrics", {})
    out = mod.apply_to_result({"error_rate": 0.3, "throughput": 1000.0})
    assert out["_noisy"] == 0.5
    assert isinstance(out["error_rate"], float)


def test_deployment_blackout_empties_lists(enabled):
    engine = _engine_with(_scenario(FailureType.DEPLOYMENT_BLACKOUT, "list_deployments"))
    mod = engine.apply_to_mcp_call("list_deployments", {})
    out = mod.apply_to_result({"deployments": [{"id": 1}], "total": 1})
    assert out["deployments"] == []
    assert out["_blackout"] is True


def test_no_modification_when_tool_not_targeted(enabled):
    engine = _engine_with(_scenario(FailureType.SERVICE_OUTAGE, "get_metrics"))
    mod = engine.apply_to_mcp_call("get_problems", {})  # different tool
    assert mod.error is None and mod.delay_seconds == 0.0


def test_qualified_target_matches_tool(enabled):
    engine = _engine_with(_scenario(FailureType.SERVICE_OUTAGE, "dynatrace_get_metrics"))
    mod = engine.apply_to_mcp_call("get_metrics", {})
    assert isinstance(mod.error, MCPToolCallError)  # "get_metrics" in "dynatrace_get_metrics"


# --------------------------------------------------------------------------- #
# Feature-flag gate + lifecycle
# --------------------------------------------------------------------------- #
def test_disabled_engine_is_noop():
    # failure_engine_enabled defaults to False (not enabled fixture).
    engine = _engine_with(_scenario(FailureType.SERVICE_OUTAGE, "get_problems"))
    mod = engine.apply_to_mcp_call("get_problems", {})
    assert mod.error is None and mod.result_transform is None


async def test_inject_noop_when_disabled():
    engine = FailureEngine()
    await engine.inject(_scenario(FailureType.NOISY_DATA, "get_metrics"))
    assert engine.get_active_failures() == []


async def test_inject_and_clear_lifecycle(enabled, mock_db):
    engine = FailureEngine(mission_id=(await db.create_mission("g", "GENERAL")).mission_id)
    await engine.inject(_scenario(FailureType.NOISY_DATA, "get_metrics"))
    assert len(engine.get_active_failures()) == 1

    await engine.clear(FailureType.NOISY_DATA)
    assert engine.get_active_failures() == []

    events = [e.event_type for e in await db.get_recent_events_for_mission(engine.mission_id)]
    assert "FAILURE_INJECTED" in events
    assert "FAILURE_CLEARED" in events


def test_expired_scenarios_are_pruned(enabled):
    engine = FailureEngine()
    expired = FailureScenario(
        failure_type=FailureType.NOISY_DATA, target="get_metrics", duration_seconds=1,
        activated_at=datetime.utcnow() - timedelta(seconds=5),
    )
    engine._scenarios.append(expired)
    assert engine.get_active_failures() == []  # past its duration


# --------------------------------------------------------------------------- #
# BaseMCPClient applies modifications from the FailureEngine
# --------------------------------------------------------------------------- #
def _dt_client(engine, result):
    client = DynatraceClient(server_url="http://test/mcp", backoff_min=0, backoff_max=0, failure_engine=engine)
    client._session = FakeSession(result)
    return client


def _gl_client(engine, result):
    client = GitLabClient(server_url="http://test/api/v4", backoff_min=0, backoff_max=0, failure_engine=engine)

    async def _fake_raw(tool, args):
        return result

    client._raw_call = _fake_raw
    return client


async def test_client_applies_service_outage(enabled):
    engine = _engine_with(_scenario(FailureType.SERVICE_OUTAGE, "get_problems"))
    client = _dt_client(engine, {"problems": []})
    with pytest.raises(MCPToolCallError):
        await client.get_active_problems()
    assert client._session.calls == []  # raised before the real call, not retried


async def test_client_applies_deployment_blackout(enabled):
    engine = _engine_with(_scenario(FailureType.DEPLOYMENT_BLACKOUT, "list_deployments"))
    client = _gl_client(engine, {"deployments": [{"id": 1, "status": "success", "ref": "main"}]})
    assert await client.list_recent_deployments("p1", datetime.utcnow()) == []


async def test_client_applies_delay(enabled, monkeypatch):
    recorded: list[float] = []

    async def _fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(injector.asyncio, "sleep", _fake_sleep)
    engine = _engine_with(_scenario(FailureType.DELAYED_SIGNAL, "get_problems", severity=0.5))
    client = _dt_client(engine, {"problems": []})
    await client.get_active_problems()
    assert recorded == [5.0]  # severity 0.5 * 10


async def test_client_ignores_failures_when_disabled():
    # No enabled fixture: the engine is inert even with a scenario queued.
    engine = _engine_with(_scenario(FailureType.SERVICE_OUTAGE, "get_problems"))
    client = _dt_client(engine, {"problems": []})
    assert await client.get_active_problems() == []  # outage not applied


# --------------------------------------------------------------------------- #
# Demo scenario seeded reason function
# --------------------------------------------------------------------------- #
async def test_make_seeded_reason_uses_seeded_deployment_regardless_of_deployments_arg():
    """_make_seeded_reason must return the seeded deployment even with an empty deployments list.

    This is the regression guard for the bug where the demo's _seeded_reason used
    ``deployments[0] if deployments else None``.  When GitLab is configured with a real
    project that has no recent deployments, the workflow passes [] as the deployments
    argument.  The old code then returned correlated_deployment=None, which caused
    _enforce_no_deployment_rule to downgrade the recommendation from "rollback" to
    "investigate", so no pending gitlab_rollback action was ever created.
    """
    from datetime import timedelta
    from failure_engine.demo_scenario import _make_seeded_deployment, _make_seeded_reason

    incident_start = datetime.utcnow() - timedelta(minutes=30)
    seeded_deployment = _make_seeded_deployment(incident_start)
    reason_fn = _make_seeded_reason(seeded_deployment)

    # Case 1: empty list (real GitLab, no deployments in project)
    result_empty = await reason_fn(None, None, [])
    assert result_empty.correlated_deployment is seeded_deployment
    assert result_empty.recommendation == "rollback"
    assert result_empty.correlated_deployment.id == 42

    # Case 2: non-empty list (seeded GitLab path)
    from mcp_tools.gitlab import GitLabDeployment
    other_deployment = GitLabDeployment(id=99, status="success", ref="hotfix", sha="deadbeef")
    result_with_deployments = await reason_fn(None, None, [other_deployment])
    # Must still return the SEEDED deployment, not the first item from the list.
    assert result_with_deployments.correlated_deployment is seeded_deployment
    assert result_with_deployments.correlated_deployment.id == 42
    assert result_with_deployments.recommendation == "rollback"
