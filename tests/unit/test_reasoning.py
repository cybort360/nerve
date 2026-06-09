"""Unit tests for grounded Gemini reasoning in the Incident Autopilot workflow.

The Vertex AI client is mocked via the workflow's ``model_factory`` seam — a
fake GenerativeModel whose ``generate_content`` returns a fake response carrying
text plus grounding metadata. No real Vertex AI / network is used.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from config import settings
from mcp_tools.dynatrace import DynatraceProblemDetail
from mcp_tools.gitlab import GitLabDeployment
from modules.incident_autopilot.templates import IncidentTemplates
from modules.incident_autopilot.workflow import IncidentAutopilotWorkflow

REASONING_JSON = json.dumps(
    {
        "correlated_deployment_id": 42,
        "confidence": 0.9,
        "reasoning": "Spike began ~31m after deployment 42.",
        "recommendation": "rollback",
        "external_context": "CVE-2024-9999 affects the payment library version in this deploy.",
    }
)


# --------------------------------------------------------------------------- #
# Fake Vertex AI client (mocked via model_factory)
# --------------------------------------------------------------------------- #
def _grounded_response(text: str, uris: list[str]):
    chunks = [SimpleNamespace(web=SimpleNamespace(uri=u, title=f"title for {u}")) for u in uris]
    candidate = SimpleNamespace(grounding_metadata=SimpleNamespace(grounding_chunks=chunks))
    return SimpleNamespace(text=text, candidates=[candidate])


class _FakeModel:
    def __init__(self, response):
        self._response = response
        self.calls: list[str] = []

    def generate_content(self, prompt):  # sync, like the real SDK (run via to_thread)
        self.calls.append(prompt)
        return self._response


def _problem() -> DynatraceProblemDetail:
    return DynatraceProblemDetail(
        problem_id="P-1", title="Checkout errors", severity="AVAILABILITY", status="OPEN",
        impacted_services=["checkout"], start_time=datetime(2026, 6, 3),
    )


def _deployment() -> GitLabDeployment:
    return GitLabDeployment(id=42, status="success", ref="main", sha="abc123", environment="production")


def _workflow(response):
    return IncidentAutopilotWorkflow(
        SimpleNamespace(), SimpleNamespace(), model_factory=lambda: _FakeModel(response), project_id="1"
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_grounding_enabled_populates_sources_and_context(monkeypatch):
    monkeypatch.setattr(settings, "gemini_grounding_enabled", True)
    uris = ["https://nvd.nist.gov/vuln/detail/CVE-2024-9999", "https://status.example.com/incident/42"]
    workflow = _workflow(_grounded_response(REASONING_JSON, uris))

    result = await workflow._default_reason(_problem(), None, [_deployment()])

    assert result.recommendation == "rollback"
    assert result.correlated_deployment is not None and result.correlated_deployment.id == 42
    assert result.grounding_sources == uris
    assert "CVE-2024-9999" in result.external_context


async def test_grounding_disabled_falls_back_cleanly(monkeypatch):
    monkeypatch.setattr(settings, "gemini_grounding_enabled", False)
    # Even if the (fake) response carries grounding metadata, disabled => ignored.
    uris = ["https://example.com/should-be-ignored"]
    workflow = _workflow(_grounded_response(REASONING_JSON, uris))

    result = await workflow._default_reason(_problem(), None, [_deployment()])

    assert result.recommendation == "rollback"   # core reasoning still works
    assert result.grounding_sources == []         # no grounding when disabled
    assert result.external_context == ""


async def test_grounding_sources_appear_in_issue_template():
    sources = ["https://nvd.nist.gov/vuln/detail/CVE-2024-9999", "https://status.example.com/incident/42"]
    body = IncidentTemplates.gitlab_issue_body(
        _problem(),
        _deployment(),
        "deploy 42 correlates",
        external_context="CVE-2024-9999 affects the payment library.",
        grounding_sources=sources,
    )
    assert "External Intelligence" in body
    assert "CVE-2024-9999 affects the payment library." in body
    for url in sources:
        assert url in body


def test_issue_template_omits_section_without_grounding():
    body = IncidentTemplates.gitlab_issue_body(_problem(), _deployment(), "deploy 42 correlates")
    assert "External Intelligence" not in body  # section hidden when nothing to show
