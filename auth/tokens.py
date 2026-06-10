"""JWT session tokens (HS256) and the session cookie name."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import jwt
import structlog

from config import settings

log = structlog.get_logger()

COOKIE_NAME = "nerve_session"
_ALGORITHM = "HS256"
#: Fallback secret when JWT_SECRET is unset — stable for this process only.
_EPHEMERAL_SECRET = secrets.token_hex(32)


def _secret() -> str:
    """Return the signing secret (configured, or a per-process ephemeral one)."""
    if settings.jwt_secret:
        return settings.jwt_secret
    return _EPHEMERAL_SECRET


def create_access_token(user_id: str, *, expires_minutes: int | None = None) -> str:
    """Create a signed JWT carrying the user id as ``sub``.

    Args:
        user_id: The user this session belongs to.
        expires_minutes: Override the default lifetime (negative => already expired).

    Returns:
        The encoded JWT string.
    """
    minutes = settings.jwt_expire_minutes if expires_minutes is None else expires_minutes
    payload = {"sub": user_id, "exp": datetime.utcnow() + timedelta(minutes=minutes)}
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def decode_token(token: str | None) -> str | None:
    """Return the user id from a valid token, or None if invalid/expired/missing."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None
