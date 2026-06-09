#!/usr/bin/env python3
"""Provision a GitLab demo project for NERVE's Incident Autopilot.

Creates (idempotently) a ``nerve-demo-service`` project, seeds a commit that
touches ``payment_processor.py`` and a deployment record, then prints the
project id to copy into ``.env`` as ``GITLAB_PROJECT_ID``.

Run once after setting GITLAB_URL + GITLAB_TOKEN (token needs the ``api`` scope):

    python scripts/setup_gitlab_demo.py

This is a developer CLI tool (uses ``print`` for copy-paste output); it makes
real GitLab API calls and creates real objects in your namespace.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import httpx

from config import settings

PROJECT_NAME = "nerve-demo-service"
DEMO_FILE = "payment_processor.py"
DEMO_ENVIRONMENT = "production"


def _client() -> httpx.Client:
    """Return an httpx client bound to the GitLab REST API."""
    if not settings.gitlab_token:
        sys.exit("GITLAB_TOKEN is not set. Set it (api scope) in .env first.")
    return httpx.Client(
        base_url=f"{settings.gitlab_url.rstrip('/')}/api/v4",
        headers={"PRIVATE-TOKEN": settings.gitlab_token},
        timeout=30.0,
    )


def _find_project(client: httpx.Client) -> dict | None:
    """Return the demo project owned by the current user, or None."""
    resp = client.get("/projects", params={"search": PROJECT_NAME, "membership": True, "per_page": 100})
    resp.raise_for_status()
    for project in resp.json():
        if project.get("path") == PROJECT_NAME or project.get("name") == PROJECT_NAME:
            return project
    return None


def _create_project(client: httpx.Client) -> dict:
    """Create the demo project initialized with a default branch."""
    resp = client.post(
        "/projects",
        json={
            "name": PROJECT_NAME,
            "path": PROJECT_NAME,
            "description": "NERVE Incident Autopilot demo service (safe to delete).",
            "initialize_with_readme": True,
            "visibility": "private",
        },
    )
    resp.raise_for_status()
    return resp.json()


def _seed_commit(client: httpx.Client, project_id: int, branch: str) -> str:
    """Create/update payment_processor.py via a commit; return the commit SHA."""
    body = {
        "branch": branch,
        "commit_message": "Refactor payment_processor.py (demo change)",
        "actions": [
            {
                "action": "create",
                "file_path": DEMO_FILE,
                "content": "def charge(amount):\n    return process(amount)  # demo change\n",
            }
        ],
    }
    resp = client.post(f"/projects/{project_id}/repository/commits", json=body)
    if resp.status_code == 400:  # file already exists -> update instead
        body["actions"][0]["action"] = "update"
        resp = client.post(f"/projects/{project_id}/repository/commits", json=body)
    resp.raise_for_status()
    return resp.json()["id"]


def _seed_deployment(client: httpx.Client, project_id: int, branch: str, sha: str) -> None:
    """Create a deployment record pointing at the seeded commit."""
    resp = client.post(
        f"/projects/{project_id}/deployments",
        json={
            "environment": DEMO_ENVIRONMENT,
            "sha": sha,
            "ref": branch,
            "tag": False,
            "status": "success",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    # A 4xx here (e.g. environment already has deployments) is non-fatal for the demo.
    if resp.status_code >= 400:
        print(f"  (deployment seed returned {resp.status_code}; continuing)")


def main() -> None:
    """Provision the demo project and print the GITLAB_PROJECT_ID to use."""
    with _client() as client:
        project = _find_project(client)
        if project is None:
            print(f"Creating project '{PROJECT_NAME}'…")
            project = _create_project(client)
        else:
            print(f"Found existing project '{PROJECT_NAME}'.")
        project_id = project["id"]
        branch = project.get("default_branch") or "main"

        print("Seeding commit touching payment_processor.py…")
        sha = _seed_commit(client, project_id, branch)
        print(f"  commit {sha[:10]} on {branch}")

        print("Seeding deployment record…")
        _seed_deployment(client, project_id, branch, sha)

    print("\n" + "=" * 60)
    print("Demo project ready. Add this to your .env:")
    print(f"  GITLAB_PROJECT_ID={project_id}")
    print("=" * 60)


if __name__ == "__main__":
    main()
