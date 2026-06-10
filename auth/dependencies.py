"""FastAPI dependencies that resolve the session from the request/WebSocket cookie."""
from __future__ import annotations

from fastapi import HTTPException, Request, status
from starlette.websockets import WebSocket

from auth.tokens import COOKIE_NAME, decode_token
from state import database as db
from state.models import User

#: WebSocket close code for an unauthenticated handshake (private 4xxx range).
WS_UNAUTHORIZED = 4401


async def current_user(request: Request) -> User:
    """Return the authenticated user, or raise 401.

    Args:
        request: The incoming request (its cookies carry the session).

    Returns:
        The :class:`~state.models.User`.

    Raises:
        HTTPException: 401 if there is no valid session.
    """
    user_id = decode_token(request.cookies.get(COOKIE_NAME))
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    user = await db.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return user


async def reject_unauthenticated_ws(websocket: WebSocket) -> bool:
    """Close an unauthenticated WebSocket before it connects; return True if rejected.

    ``BaseHTTPMiddleware`` does not run for WebSocket handshakes, so the HTTP
    auth middleware cannot gate ``/ws`` routes — each WS endpoint must call this.

    Args:
        websocket: The incoming WebSocket (its cookies carry the session).

    Returns:
        True if the socket was closed (no valid session); False if authenticated.
    """
    if decode_token(websocket.cookies.get(COOKIE_NAME)) is None:
        await websocket.close(code=WS_UNAUTHORIZED)
        return True
    return False
