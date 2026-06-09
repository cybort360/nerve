"""MCP client layer for NERVE.

All Model Context Protocol access goes through these wrapped clients; agents
never call MCP tools directly (CLAUDE.md invariant 6). Each client emits audit
events, redacts secrets, retries transient failures, and raises typed
:class:`~exceptions.MCPError` subclasses.
"""
