"""Typed exception hierarchy for NERVE.

Defined per ARCHITECTURE.md section 1. Never raise a bare :class:`Exception`.
Every exception carries a human-readable ``message``, a structured ``context``
dict for logging, and a ``recoverable`` flag indicating whether the system
should retry or halt.
"""

from __future__ import annotations


class NerveBaseError(Exception):
    """Root of the NERVE exception hierarchy.

    Args:
        message: Human-readable description of what went wrong.
        context: Structured data for logging (mission_id, task_id, tool, etc.).
        recoverable: Whether the system should retry/continue rather than halt.
            Defaults to the subclass's ``default_recoverable`` when omitted.
    """

    #: Sensible per-subclass default; overridable per instance.
    default_recoverable: bool = False

    def __init__(
        self,
        message: str,
        context: dict | None = None,
        recoverable: bool | None = None,
    ) -> None:
        self.message = message
        self.context: dict = context or {}
        self.recoverable: bool = (
            self.default_recoverable if recoverable is None else recoverable
        )
        super().__init__(message)

    def to_dict(self) -> dict:
        """Return a structured representation suitable for structlog payloads.

        Returns:
            Dict with the error type, message, context, and recoverable flag.
        """
        return {
            "error_type": type(self).__name__,
            "message": self.message,
            "context": self.context,
            "recoverable": self.recoverable,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(message={self.message!r}, "
            f"context={self.context!r}, recoverable={self.recoverable!r})"
        )


# --------------------------------------------------------------------------- #
# Orchestrator errors
# --------------------------------------------------------------------------- #
class OrchestratorError(NerveBaseError):
    """Base class for orchestration-loop failures."""


class MissionNotFoundError(OrchestratorError):
    """Requested mission does not exist in the ``missions`` collection."""

    default_recoverable = False


class PlanningFailedError(OrchestratorError):
    """The planner could not decompose the goal into a valid task graph."""

    default_recoverable = True


class CycleTimeoutError(OrchestratorError):
    """An orchestration cycle exceeded its allotted time budget."""

    default_recoverable = True


# --------------------------------------------------------------------------- #
# Agent errors
# --------------------------------------------------------------------------- #
class AgentError(NerveBaseError):
    """Base class for agent-level failures."""


class AgentExecutionError(AgentError):
    """An agent's ``run()`` raised or produced an unusable result."""

    default_recoverable = True


class AgentTimeoutError(AgentError):
    """An agent did not complete within its timeout."""

    default_recoverable = True


class InvalidAgentResultError(AgentError):
    """An agent returned a result that failed contract validation."""

    default_recoverable = False


# --------------------------------------------------------------------------- #
# MCP errors
# --------------------------------------------------------------------------- #
class MCPError(NerveBaseError):
    """Base class for Model Context Protocol client failures."""


class MCPConnectionError(MCPError):
    """Could not establish or maintain a connection to an MCP server."""

    default_recoverable = True


class MCPToolCallError(MCPError):
    """An MCP tool invocation returned an error or malformed response."""

    default_recoverable = True


class MCPRateLimitError(MCPError):
    """An MCP server rejected the call due to rate limiting."""

    default_recoverable = True


class MCPAuthError(MCPError):
    """Authentication or authorization against an MCP server failed."""

    default_recoverable = False


# --------------------------------------------------------------------------- #
# State / persistence errors
# --------------------------------------------------------------------------- #
class StateError(NerveBaseError):
    """Base class for state-layer (MongoDB) failures."""


class DocumentNotFoundError(StateError):
    """A required document was not found in its collection."""

    default_recoverable = False


class WriteFailedError(StateError):
    """A write to MongoDB failed after exhausting retries."""

    default_recoverable = True


class ValidationError(StateError):
    """Data failed Pydantic validation before entering the system."""

    default_recoverable = False


# --------------------------------------------------------------------------- #
# Failure engine
# --------------------------------------------------------------------------- #
class FailureEngineError(NerveBaseError):
    """A controlled failure-injection operation itself failed."""

    default_recoverable = True
