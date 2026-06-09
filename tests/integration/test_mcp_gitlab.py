"""Integration tests for the GitLab client against the real GitLab REST API.

Skipped unless ``NERVE_INTEGRATION=1`` and real GitLab credentials are set.
Read-only checks run by default; the mutating issue round-trip runs only when
``NERVE_INTEGRATION_WRITE=1`` is also set, to avoid creating noise in a project.

First provision the demo project so there is data to read:
    python scripts/setup_gitlab_demo.py

Run with:
    NERVE_INTEGRATION=1 \
    GITLAB_URL=... GITLAB_TOKEN=... GITLAB_PROJECT_ID=... \
    pytest tests/integration -m integration
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from config import settings
from mcp_tools.gitlab import GitLabClient, GitLabCommit, GitLabDeployment, GitLabIssue

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("NERVE_INTEGRATION") != "1",
        reason="set NERVE_INTEGRATION=1 with live GitLab credentials to run",
    ),
]


async def test_list_recent_deployments_returns_models():
    """A live deployments query returns typed GitLabDeployment models."""
    since = datetime.utcnow() - timedelta(days=365)
    async with GitLabClient() as client:
        deployments = await client.list_recent_deployments(settings.gitlab_project_id, since)
    assert isinstance(deployments, list)
    assert all(isinstance(d, GitLabDeployment) for d in deployments)


async def test_get_commit_details_returns_changed_files():
    """Fetch a real commit (from a deployment's SHA) with its changed files."""
    since = datetime.utcnow() - timedelta(days=365)
    async with GitLabClient() as client:
        deployments = await client.list_recent_deployments(settings.gitlab_project_id, since)
        sha = next((d.sha for d in deployments if d.sha), None)
        if not sha:
            pytest.skip("no deployment with a SHA to fetch a commit for")
        commit = await client.get_commit_details(settings.gitlab_project_id, sha)
    assert isinstance(commit, GitLabCommit)
    assert commit.sha
    assert isinstance(commit.files_changed, list)


async def test_issue_create_and_close_round_trip():
    """Create then close a real issue (write test, opt-in via a second flag)."""
    if os.environ.get("NERVE_INTEGRATION_WRITE") != "1":
        pytest.skip("set NERVE_INTEGRATION_WRITE=1 to run mutating GitLab checks")
    async with GitLabClient() as client:
        issue = await client.create_issue(
            settings.gitlab_project_id,
            title="[NERVE integration test] please ignore",
            description="Created by the NERVE integration suite; safe to close.",
            labels=["nerve-test"],
        )
        assert isinstance(issue, GitLabIssue)
        assert issue.iid > 0
        await client.close_issue(settings.gitlab_project_id, issue.iid)
