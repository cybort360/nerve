"""Integration tests for the Dynatrace MCP client against a live server.

Skipped unless ``NERVE_INTEGRATION=1`` and real Dynatrace credentials are set.
The shared ``tests/conftest.py`` seeds dummy creds for unit tests, so a dedicated
opt-in flag — not the presence of the token — gates these.

Run with:
    NERVE_INTEGRATION=1 \
    DYNATRACE_ENVIRONMENT_URL=... DYNATRACE_API_TOKEN=... \
    pytest tests/integration -m integration
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from mcp_tools.dynatrace import (
    DynatraceClient,
    DynatraceProblem,
    DynatraceProblemDetail,
    ServiceMetrics,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("NERVE_INTEGRATION") != "1",
        reason="set NERVE_INTEGRATION=1 with live Dynatrace credentials to run",
    ),
]


async def test_get_active_problems_returns_models():
    """A live problems query returns typed DynatraceProblem models."""
    async with DynatraceClient() as client:
        problems = await client.get_active_problems()
    assert isinstance(problems, list)
    assert all(isinstance(p, DynatraceProblem) for p in problems)


async def test_problem_details_when_problems_exist():
    """If any problem is active, its details parse into the detail model."""
    async with DynatraceClient() as client:
        problems = await client.get_active_problems()
        if not problems:
            pytest.skip("no active problems in this environment to detail")
        detail = await client.get_problem_details(problems[0].problem_id)
    assert isinstance(detail, DynatraceProblemDetail)
    assert detail.problem_id == problems[0].problem_id


async def test_service_metrics_returns_model():
    """Metrics for a configured service window parse into ServiceMetrics."""
    service_id = os.environ.get("DYNATRACE_TEST_SERVICE_ID")
    if not service_id:
        pytest.skip("set DYNATRACE_TEST_SERVICE_ID to run the metrics check")
    to_time = datetime.utcnow()
    from_time = to_time - timedelta(hours=1)
    async with DynatraceClient() as client:
        metrics = await client.get_service_metrics(service_id, from_time, to_time)
    assert isinstance(metrics, ServiceMetrics)
    assert metrics.service_id == service_id
