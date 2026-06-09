"""Controlled failure injection for NERVE demos and testing.

Opt-in only: nothing here has any effect unless ``FAILURE_ENGINE_ENABLED`` is
true (CLAUDE.md invariant 7). Re-exports the engine primitives from
:mod:`failure_engine.injector`. ``demo_scenario`` is imported on demand (it pulls
in the orchestrator) to avoid an import cycle with the MCP client layer.
"""

from failure_engine.injector import (
    FailureEngine,
    FailureScenario,
    FailureType,
    MCPCallModification,
)

__all__ = ["FailureEngine", "FailureScenario", "FailureType", "MCPCallModification"]
