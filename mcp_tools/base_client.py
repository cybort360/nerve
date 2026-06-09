"""Shared MCP client: session lifecycle, audit, retries, failure injection.

``BaseMCPClient`` is an async context manager wrapping an MCP session. Every
``call_tool`` invocation is logged and audited (``MCP_TOOL_CALLED`` /
``MCP_TOOL_RESULT``), has secret arguments redacted, is retried per the standard
policy, and is translated into typed :class:`~exceptions.MCPError` subclasses.
When a :class:`~failure_engine.injector.FailureEngine` is attached, it applies
the modifications that engine renders for each call.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from time import perf_counter
from typing import Any

import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from exceptions import (
    MCPAuthError,
    MCPConnectionError,
    MCPError,
    MCPRateLimitError,
    MCPToolCallError,
    StateError,
)
from failure_engine.injector import MCPCallModification
from state import database as db

log = structlog.get_logger()

SOURCE_MCP = "mcp"
EVENT_MCP_TOOL_CALLED = "MCP_TOOL_CALLED"
EVENT_MCP_TOOL_RESULT = "MCP_TOOL_RESULT"

# Standard MCP retry policy (ARCHITECTURE.md): 3 attempts, exp backoff 2s→10s.
MCP_MAX_ATTEMPTS = 3
MCP_BACKOFF_MIN = 2.0
MCP_BACKOFF_MAX = 10.0

MAX_RESULT_CHARS = 2000
REDACTED = "***REDACTED***"
_SECRET_HINTS = ("token", "secret", "password", "authorization", "api_key", "private")


def _is_recoverable_mcp(exc: BaseException) -> bool:
    """Return True if ``exc`` is a recoverable MCP error worth retrying."""
    return isinstance(exc, MCPError) and exc.recoverable


class BaseMCPClient:
    """Async-context-managed MCP client with audit, retry, and failure hooks."""

    def __init__(
        self,
        server_name: str,
        server_url: str,
        auth_headers: dict[str, str],
        *,
        mission_id: str | None = None,
        failure_engine: Any | None = None,
        max_attempts: int = MCP_MAX_ATTEMPTS,
        backoff_min: float = MCP_BACKOFF_MIN,
        backoff_max: float = MCP_BACKOFF_MAX,
    ) -> None:
        """Initialize the client.

        Args:
            server_name: Short server label used in logs/events.
            server_url: MCP endpoint URL.
            auth_headers: Headers carrying credentials for the transport.
            mission_id: Mission to attribute audit events to (optional).
            failure_engine: FailureEngine whose modifications to apply (optional).
            max_attempts: Retry attempts for recoverable errors.
            backoff_min: Minimum exponential backoff seconds.
            backoff_max: Maximum exponential backoff seconds.
        """
        self.server_name = server_name
        self._server_url = server_url
        self._auth_headers = auth_headers
        self.mission_id = mission_id
        self._failure_engine = failure_engine
        self._max_attempts = max_attempts
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._session: Any | None = None
        self._stack: AsyncExitStack | None = None
        self._log = structlog.get_logger().bind(mcp_server=server_name)

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #
    async def __aenter__(self) -> "BaseMCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Open and initialize the MCP session.

        Raises:
            MCPConnectionError / MCPAuthError: If the session cannot be opened.
        """
        from mcp import ClientSession  # lazy: SDK not needed for import-time
        from mcp.client.streamable_http import streamablehttp_client

        stack = AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(self._server_url, headers=self._auth_headers)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception as exc:  # noqa: BLE001 — translate any transport error
            await stack.aclose()
            raise self._classify(exc, "<connect>") from exc
        self._stack = stack
        self._session = session
        self._log.info("mcp_connected", url=self._server_url)

    async def disconnect(self) -> None:
        """Close the MCP session and release transport resources."""
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None
        self._log.info("mcp_disconnected")

    # ----------------------------------------------------------------- #
    # Tool invocation
    # ----------------------------------------------------------------- #
    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Invoke an MCP tool with audit, retry, and failure injection.

        Args:
            tool_name: Name of the MCP tool to call.
            arguments: Tool arguments (secrets are redacted before logging).

        Returns:
            The tool's structured result as a dict.

        Raises:
            MCPError: A typed error on any failure.
        """
        await self._emit(EVENT_MCP_TOOL_CALLED, {"tool": tool_name, "args": self._redact(arguments)})
        start = perf_counter()
        modification = self._failure_modification(tool_name, arguments)
        try:
            await modification.apply_before_call()
            raw = await self._retrying_call(tool_name, arguments)
            raw = modification.apply_to_result(raw)
        except MCPError as exc:
            duration_ms = (perf_counter() - start) * 1000
            self._log.error("mcp_tool_failed", tool=tool_name, duration_ms=duration_ms, error=str(exc))
            await self._emit(
                EVENT_MCP_TOOL_RESULT,
                {"tool": tool_name, "duration_ms": duration_ms, "success": False, "error": str(exc)},
            )
            raise
        duration_ms = (perf_counter() - start) * 1000
        self._log.info("mcp_tool_called", tool=tool_name, duration_ms=duration_ms, success=True)
        await self._emit(
            EVENT_MCP_TOOL_RESULT,
            {"tool": tool_name, "duration_ms": duration_ms, "success": True, "result": self._trim(raw)},
        )
        return raw

    def _failure_modification(self, tool_name: str, arguments: dict) -> MCPCallModification:
        """Ask the attached failure engine how to modify this call (if any)."""
        if self._failure_engine is None:
            return MCPCallModification()
        return self._failure_engine.apply_to_mcp_call(tool_name, arguments)

    async def _retrying_call(self, tool_name: str, arguments: dict) -> dict:
        """Call the tool, retrying only recoverable MCP errors."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=1, min=self._backoff_min, max=self._backoff_max),
            retry=retry_if_exception(_is_recoverable_mcp),
            reraise=True,
        ):
            with attempt:
                return await self._raw_call(tool_name, arguments)
        raise MCPToolCallError("retry loop exhausted", context={"server": self.server_name, "tool": tool_name})

    async def _raw_call(self, tool_name: str, arguments: dict) -> dict:
        """Perform a single MCP call and translate any raw error."""
        if self._session is None:
            raise MCPConnectionError(
                "session not connected", context={"server": self.server_name, "tool": tool_name}
            )
        try:
            result = await self._session.call_tool(tool_name, arguments)
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let raw transport errors escape
            raise self._classify(exc, tool_name) from exc
        return self._extract(result)

    # ----------------------------------------------------------------- #
    # Audit + helpers
    # ----------------------------------------------------------------- #
    async def _emit(self, event_type: str, payload: dict) -> None:
        """Emit an audit event if bound to a mission; never raise on failure."""
        if self.mission_id is None:
            return
        try:
            await db.emit_event(self.mission_id, event_type, payload, SOURCE_MCP)
        except StateError as exc:
            self._log.warning("mcp_event_emit_failed", event_type=event_type, error=str(exc))

    @staticmethod
    def _redact(arguments: dict) -> dict:
        """Replace secret-looking argument values with a redaction marker."""
        redacted: dict = {}
        for key, value in arguments.items():
            lowered = key.lower()
            redacted[key] = REDACTED if any(hint in lowered for hint in _SECRET_HINTS) else value
        return redacted

    @staticmethod
    def _trim(raw: dict) -> dict:
        """Trim a large result for the audit trail."""
        serialized = json.dumps(raw, default=str)
        if len(serialized) <= MAX_RESULT_CHARS:
            return raw
        return {"preview": serialized[:MAX_RESULT_CHARS], "truncated": True, "length": len(serialized)}

    @staticmethod
    def _extract(result: Any) -> dict:
        """Pull a dict payload from an MCP CallToolResult-like object."""
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"text": text}
        return {}

    def _classify(self, exc: Exception, tool: str) -> MCPError:
        """Translate a raw transport/SDK error into a typed MCPError."""
        if isinstance(exc, MCPError):
            return exc
        status = self._status_code(exc)
        ctx = {"server": self.server_name, "tool": tool, "status": status}
        if status in (401, 403):
            return MCPAuthError(f"authentication failed for {tool}", context=ctx)
        if status == 429:
            return MCPRateLimitError(f"rate limited calling {tool}", context=ctx)
        if status in (502, 503, 504) or status is None and _looks_like_connection(exc):
            return MCPConnectionError(f"connection error calling {tool}", context=ctx)
        return MCPToolCallError(f"tool call failed: {exc}", context=ctx)

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        """Best-effort extraction of an HTTP status code from an error."""
        for candidate in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None)):
            if isinstance(candidate, int):
                return candidate
        for token in str(exc).replace(":", " ").split():
            if token.isdigit() and len(token) == 3:
                return int(token)
        return None


def _looks_like_connection(exc: Exception) -> bool:
    """Heuristic: does this error name suggest a connection-level failure?"""
    name = type(exc).__name__.lower()
    return any(hint in name for hint in ("connect", "timeout", "transport", "disconnect"))
