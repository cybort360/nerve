"""Unit tests for the MCP client layer (mcp_tools/).

These exercise the pure, connection-independent logic — redaction, audit
emission, retry/translation, and response parsing — using a fake MCP session
injected in place of a real one. Failure-injection behavior (FailureEngine ↔
BaseMCPClient) is covered in test_failure_engine.py; live-server behavior is
covered by the integration tests.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from exceptions import MCPAuthError, MCPRateLimitError
from mcp_tools.dynatrace import DynatraceClient, DynatraceProblem
from mcp_tools.gitlab import GitLabClient, GitLabIssue, GitLabPipeline

MISSION_ID = "m-1"


class FakeSession:
    """Stand-in for an MCP ClientSession with a canned response or error."""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if self._error is not None:
            raise self._error
        return SimpleNamespace(structuredContent=self._result, content=[])


def _dt_client(result=None, error=None, **kwargs) -> DynatraceClient:
    """Build a Dynatrace client with a fake session and no real backoff."""
    client = DynatraceClient(server_url="http://test/mcp", backoff_min=0, backoff_max=0, **kwargs)
    client._session = FakeSession(result=result, error=error)
    return client


def _gl_client(result=None, **kwargs) -> GitLabClient:
    """GitLab client whose REST transport is replaced with a canned _raw_call."""
    client = GitLabClient(server_url="http://test/api/v4", backoff_min=0, backoff_max=0, **kwargs)
    client._calls = []

    async def _fake_raw(tool, args):
        client._calls.append((tool, args))
        return {} if result is None else result

    client._raw_call = _fake_raw
    return client


# --------------------------------------------------------------------------- #
# Redaction + audit
# --------------------------------------------------------------------------- #
def test_redact_hides_secret_args():
    client = _dt_client()
    redacted = client._redact({"api_token": "abc", "service_id": "svc"})
    assert redacted["api_token"] == "***REDACTED***"
    assert redacted["service_id"] == "svc"


async def test_emits_called_and_result_events(monkeypatch):
    from mcp_tools import base_client

    emit = AsyncMock()
    monkeypatch.setattr(base_client.db, "emit_event", emit)
    client = _dt_client(result={"problems": []}, mission_id=MISSION_ID)

    await client.get_active_problems()
    emitted = [call.args[1] for call in emit.await_args_list]
    assert emitted == ["MCP_TOOL_CALLED", "MCP_TOOL_RESULT"]


async def test_no_events_without_mission():
    from mcp_tools import base_client

    client = _dt_client(result={"problems": []})  # mission_id is None
    # Should not raise even though no mission is bound; simply skips emission.
    problems = await client.get_active_problems()
    assert problems == []


# --------------------------------------------------------------------------- #
# Error translation + retry
# --------------------------------------------------------------------------- #
async def test_auth_error_not_retried():
    err = SimpleNamespace()
    boom = type("Boom", (Exception,), {})("403 forbidden")
    boom.status_code = 403  # type: ignore[attr-defined]
    client = _dt_client(error=boom)
    with pytest.raises(MCPAuthError):
        await client.get_active_problems()
    assert len(client._session.calls) == 1  # non-recoverable: no retry


async def test_rate_limit_is_retried_to_limit():
    boom = type("Boom", (Exception,), {})("429 too many")
    boom.status_code = 429  # type: ignore[attr-defined]
    client = _dt_client(error=boom)
    with pytest.raises(MCPRateLimitError):
        await client.get_active_problems()
    assert len(client._session.calls) == 3  # retried to MCP_MAX_ATTEMPTS


# --------------------------------------------------------------------------- #
# Response parsing -> typed models
# --------------------------------------------------------------------------- #
async def test_dynatrace_parses_problems():
    result = {
        "problems": [
            {
                "problemId": "P-1",
                "title": "checkout error spike",
                "severityLevel": "AVAILABILITY",
                "status": "OPEN",
                "affectedEntities": [{"name": "checkout"}],
                "startTime": 1700000000000,
            }
        ]
    }
    client = _dt_client(result=result)
    problems = await client.get_active_problems()
    assert len(problems) == 1
    assert isinstance(problems[0], DynatraceProblem)
    assert problems[0].problem_id == "P-1"
    assert problems[0].affected_entities == ["checkout"]
    assert isinstance(problems[0].start_time, datetime)


async def test_gitlab_create_issue_parses():
    result = {"id": 10, "iid": 3, "title": "Incident", "state": "opened", "labels": ["incident"]}
    client = _gl_client(result=result)
    issue = await client.create_issue("p1", "Incident", "body", ["incident"])
    assert isinstance(issue, GitLabIssue)
    assert issue.iid == 3
    assert issue.labels == ["incident"]


async def test_gitlab_trigger_pipeline_parses():
    result = {"id": 99, "status": "created", "ref": "main", "sha": "abc123"}
    client = _gl_client(result=result)
    pipeline = await client.trigger_pipeline("p1", "main", {"ROLLBACK": "true"})
    assert isinstance(pipeline, GitLabPipeline)
    assert pipeline.id == 99
    assert client._calls[0][1]["variables"] == {"ROLLBACK": "true"}


async def test_gitlab_close_issue_returns_none():
    client = _gl_client(result={})
    assert await client.close_issue("p1", 3) is None
    assert client._calls[0][0] == "close_issue"
