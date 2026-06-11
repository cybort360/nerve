"""Shared test fixtures for the NERVE suite.

Sets dummy values for the required settings BEFORE any project module imports
``config`` (which instantiates the ``settings`` singleton at import time), then
provides an in-memory MongoDB via ``mongomock-motor`` wired into the state layer.
"""

from __future__ import annotations

import base64
import os

# Load a real .env FIRST so a developer's real credentials populate os.environ.
# pydantic-settings ranks os.environ ABOVE the .env file, so the throwaway
# placeholders below (set via setdefault) must not run before .env is loaded —
# otherwise they'd shadow real values and break integration tests (NERVE_INTEGRATION=1).
try:
    from dotenv import load_dotenv

    load_dotenv()  # does not override values already in os.environ
except ImportError:
    pass

# Must run before `config` is imported anywhere. These fill only what .env / the
# real environment did NOT provide, so unit tests stay hermetic in CI while
# integration tests use the real credentials from .env.
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGODB_DATABASE", "nerve_test")
os.environ.setdefault("DYNATRACE_ENVIRONMENT_URL", "https://test.live.dynatrace.com")
os.environ.setdefault("DYNATRACE_API_TOKEN", "test-token")
os.environ.setdefault("GITLAB_TOKEN", "test-token")
os.environ.setdefault("GITLAB_PROJECT_ID", "123")

# Feature flags must be OFF for the hermetic test baseline, regardless of the
# developer's .env (which may enable the demo to run the dev server). These are
# hard overrides — not setdefault — so a local `FAILURE_ENGINE_ENABLED=true` /
# `DEMO_MODE=true` cannot leak in and break the "when disabled" tests. Tests that
# need a flag on opt in explicitly via monkeypatch (see the `enabled` fixtures).
os.environ["FAILURE_ENGINE_ENABLED"] = "false"
os.environ["DEMO_MODE"] = "false"
# Keep planning hermetic: the default planner must not reach out to the ADK /
# Agent Engine layer (no creds in CI). Tests of the ADK path inject decompose_fn.
os.environ["AGENT_BUILDER_ENABLED"] = "false"
os.environ["JWT_SECRET"] = "test-secret-key-deterministic-0123456789abcdef"
os.environ["SETTINGS_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"0" * 32).decode()

import pytest  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from state import database  # noqa: E402


@pytest.fixture
def mock_db(monkeypatch):
    """Replace the state layer's singleton with a fresh in-memory database.

    Yields a clean ``mongomock-motor`` database per test; ``monkeypatch`` restores
    the original module globals afterward.
    """
    client = AsyncMongoMockClient()
    db = client["nerve_test"]
    monkeypatch.setattr(database, "_client", client)
    monkeypatch.setattr(database, "_db", db)
    return db
