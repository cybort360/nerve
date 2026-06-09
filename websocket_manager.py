"""WebSocket connection management for real-time dashboard updates.

Tracks active dashboard WebSocket connections per mission and broadcasts each
newly persisted event to them. Broadcasting is best-effort: a failed send never
propagates to the caller (event persistence must not depend on delivery), and
stale connections are pruned as they are discovered.

A module-level :data:`connection_manager` singleton is shared by the WebSocket
endpoint (via ``app.state``) and by ``state.database.emit_event`` (via a lazy
import), so both sides push/observe the same connection set.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import WebSocket

log = structlog.get_logger()


class ConnectionManager:
    """Tracks WebSocket connections per ``mission_id`` and broadcasts events."""

    def __init__(self) -> None:
        """Initialize an empty connection registry."""
        self._connections: dict[str, set[WebSocket]] = {}
        self._log = structlog.get_logger().bind(component="ws_manager")

    async def connect(self, mission_id: str, websocket: WebSocket) -> None:
        """Accept a WebSocket and register it under a mission.

        Args:
            mission_id: Mission the client wants updates for.
            websocket: The client connection to accept and track.
        """
        await websocket.accept()
        self._connections.setdefault(mission_id, set()).add(websocket)
        self._log.info(
            "ws_connected", mission_id=mission_id, connections=self.connection_count(mission_id)
        )

    def disconnect(self, mission_id: str, websocket: WebSocket) -> None:
        """Remove a connection, dropping the mission entry when it empties.

        Args:
            mission_id: Mission the connection was registered under.
            websocket: The connection to remove.
        """
        conns = self._connections.get(mission_id)
        if conns is None:
            return
        conns.discard(websocket)
        if not conns:
            self._connections.pop(mission_id, None)
        self._log.info(
            "ws_disconnected", mission_id=mission_id, connections=self.connection_count(mission_id)
        )

    async def broadcast(self, mission_id: str, message: dict[str, Any]) -> None:
        """Send a message to every connection for a mission, pruning dead ones.

        Best-effort: per-connection send failures are caught and the offending
        connection is removed; this method never raises.

        Args:
            mission_id: Mission whose subscribers should receive the message.
            message: JSON-serializable payload (typically a serialized event).
        """
        connections = list(self._connections.get(mission_id, ()))
        if not connections:
            return
        stale: list[WebSocket] = []
        for websocket in connections:
            try:
                await websocket.send_json(message)
            except Exception as exc:  # noqa: BLE001 — drop & prune any failed socket
                self._log.warning("ws_send_failed", mission_id=mission_id, error=str(exc))
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(mission_id, websocket)

    def connection_count(self, mission_id: str) -> int:
        """Return the number of active connections for a mission."""
        return len(self._connections.get(mission_id, ()))

    def total_connections(self) -> int:
        """Return the total number of active connections across all missions."""
        return sum(len(conns) for conns in self._connections.values())


#: Shared singleton used by the WebSocket endpoint and by ``emit_event``.
connection_manager = ConnectionManager()
