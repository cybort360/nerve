"""Deploy the nerve-planner ADK agent to Vertex AI Agent Engine.

Run from the repo root with Application Default Credentials available
(``gcloud auth application-default login``). On success it prints the Agent
Engine resource name — set that as ``AGENT_BUILDER_AGENT_ID`` (locally in
``.env`` and on Cloud Run / Secret Manager) so the planner routes through the
deployed agent. Without it, planning still works in-process.

Usage:
    python scripts/deploy_planner_agent.py
    python scripts/deploy_planner_agent.py --project my-proj --location us-central1
"""

from __future__ import annotations

import argparse
import os
import sys

# Ensure repo root is importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REQUIREMENTS = [
    "google-adk>=2.0,<3.0",
    "google-cloud-aiplatform[agent_engines]>=1.155,<2.0",
    "google-genai>=1.0",
    "structlog>=24.0",
]

_EXTRA_PACKAGES = [
    "orchestrator/__init__.py",
    "orchestrator/planning_contract.py",
    "orchestrator/planner_agent_def.py",
]

_DISPLAY_NAME = "nerve-planner"
_DESCRIPTION = (
    "NERVE mission planner: decomposes an operational goal into an ordered task "
    "graph via the decompose_goal tool. Managed orchestration layer for NERVE."
)


def _ensure_staging_bucket(project: str, location: str, bucket: str) -> str:
    """Create the staging bucket if it does not already exist; return its URI."""
    from google.cloud import storage
    from google.cloud.exceptions import Conflict, NotFound

    name = bucket.removeprefix("gs://")
    client = storage.Client(project=project)
    try:
        client.get_bucket(name)
    except NotFound:
        try:
            client.create_bucket(name, location=location)
            print(f"created staging bucket gs://{name}")
        except Conflict:
            pass
    return f"gs://{name}"


def main() -> int:
    """Deploy the agent and print its resource name."""
    parser = argparse.ArgumentParser(description="Deploy the nerve-planner agent.")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument(
        "--location", default=os.environ.get("AGENT_BUILDER_LOCATION", "us-central1")
    )
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
    parser.add_argument("--staging-bucket", default=os.environ.get("AGENT_STAGING_BUCKET", ""))
    args = parser.parse_args()

    if not args.project:
        print("error: --project (or GOOGLE_CLOUD_PROJECT) is required", file=sys.stderr)
        return 2

    bucket = args.staging_bucket or f"gs://{args.project}-agent-engine-staging"

    import vertexai
    from vertexai import agent_engines

    from orchestrator.planner_agent_def import build_adk_app

    staging = _ensure_staging_bucket(args.project, args.location, bucket)
    vertexai.init(project=args.project, location=args.location, staging_bucket=staging)

    print(f"deploying {_DISPLAY_NAME} to {args.project}/{args.location} (model={args.model})…")
    remote = agent_engines.create(
        agent_engine=build_adk_app(),
        display_name=_DISPLAY_NAME,
        description=_DESCRIPTION,
        requirements=_REQUIREMENTS,
        extra_packages=_EXTRA_PACKAGES,
        # GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION are reserved — Agent Engine
        # injects them into the runtime itself, so only the model id is passed.
        env_vars={"GEMINI_MODEL": args.model},
    )

    print("\n✅ deployed. Resource name:\n")
    print(f"  {remote.resource_name}\n")
    print("Set this as AGENT_BUILDER_AGENT_ID in .env and on Cloud Run:")
    print(f"  AGENT_BUILDER_AGENT_ID={remote.resource_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
