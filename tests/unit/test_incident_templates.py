"""Unit tests for IncidentTemplates markdown rendering."""

from __future__ import annotations

from datetime import datetime

from mcp_tools.dynatrace import DynatraceProblemDetail
from mcp_tools.gitlab import GitLabDeployment
from modules.incident_autopilot.templates import IncidentTemplates


def _problem() -> DynatraceProblemDetail:
    return DynatraceProblemDetail(
        problem_id="P-1",
        title="Checkout 500s",
        severity="AVAILABILITY",
        status="OPEN",
        impacted_services=["checkout"],
        root_cause="bad deploy",
        timeline=[{"timestamp": "2026-06-03T00:00:00", "description": "spike"}],
        start_time=datetime(2026, 6, 3, 0, 0, 0),
    )


def _deployment() -> GitLabDeployment:
    return GitLabDeployment(id=42, status="success", ref="main", sha="abc123", environment="production")


def test_labels_constant():
    assert IncidentTemplates.LABELS == ["incident", "p1", "nerve-created"]


def test_issue_body_includes_core_sections_with_deployment():
    body = IncidentTemplates.gitlab_issue_body(_problem(), _deployment(), "deploy 42 is the cause")
    assert "# 🚨 Incident: Checkout 500s" in body
    assert "checkout" in body
    assert "Deployment ID:** 42" in body
    assert "deploy 42 is the cause" in body
    assert "Recommended Actions" in body


def test_issue_body_handles_missing_deployment():
    body = IncidentTemplates.gitlab_issue_body(_problem(), None, "no correlation")
    assert "No correlated deployment identified" in body
    assert "Investigate the anomaly manually" in body


def test_rollback_mr_body_mentions_deployment_and_approval():
    body = IncidentTemplates.rollback_mr_body(_deployment())
    assert "revert deployment 42" in body
    assert "abc123" in body
    assert "requires human approval" in body
