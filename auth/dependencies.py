"""FastAPI dependency that resolves the current user from the session cookie."""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from auth.tokens import COOKIE_NAME, decode_token
from state import database as db
from state.models import User


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
