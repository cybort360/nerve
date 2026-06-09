"""Local demo launcher — runs NERVE fully in-memory (no external MongoDB).

For local/offline demos only. Backs the state layer with mongomock-motor, skips
the real Mongo ping, seeds placeholder env + demo flags, and speeds up the demo
timeline. NOT for production (production uses real Atlas + Secret Manager).

    python run_local_demo.py
"""

from __future__ import annotations

import os

# Load real .env FIRST so the developer's values (e.g. a real GITLAB_PROJECT_ID /
# GITLAB_TOKEN) populate os.environ. pydantic-settings ranks os.environ above the
# .env file, so without this the setdefault placeholders below would shadow .env.
try:
    from dotenv import load_dotenv

    load_dotenv()  # does not override values already present in os.environ
except ImportError:
    pass

# Seed required env BEFORE importing config (it instantiates settings at import).
# setdefault only fills what .env (and the real environment) did NOT provide.
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "local-demo")
os.environ.setdefault("MONGODB_URI", "mongodb://in-memory")
os.environ.setdefault("MONGODB_DATABASE", "nerve_demo")
os.environ.setdefault("DYNATRACE_ENVIRONMENT_URL", "https://demo.live.dynatrace.com")
os.environ.setdefault("DYNATRACE_API_TOKEN", "demo")
os.environ.setdefault("GITLAB_TOKEN", "demo")
os.environ.setdefault("GITLAB_PROJECT_ID", "123")
os.environ["DEMO_MODE"] = "true"
os.environ["FAILURE_ENGINE_ENABLED"] = "true"

import uvicorn
from mongomock_motor import AsyncMongoMockClient

import main
from state import database as db

# Back the state layer with an in-memory database.
_client = AsyncMongoMockClient()
db._client = _client
db._db = _client["nerve_demo"]


async def _fake_connect() -> AsyncMongoMockClient:
    """Replace the real Mongo connect (no ping, no external server)."""
    _client.close = lambda: None  # mongomock client has no close()
    return _client


main.connect_to_mongo = _fake_connect

# Compress the scripted demo timeline so it fits a quick local look.
import failure_engine.demo_scenario as _ds

_OrigDemo = _ds.DemoScenario


def _fast_demo(*args, **kwargs):
    kwargs.setdefault("time_scale", 0.25)
    return _OrigDemo(*args, **kwargs)


_ds.DemoScenario = _fast_demo

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run NERVE locally with an in-memory DB.")
    parser.add_argument("--port", type=int, default=8000, help="Port to serve on (default: 8000).")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1).")
    cli_args = parser.parse_args()
    uvicorn.run(main.app, host=cli_args.host, port=cli_args.port, log_level="warning")
