"""Per-user integration settings API (auth'd; secrets masked on read)."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends

from auth.dependencies import current_user
from routes.schemas import SettingsResponse, SettingsUpdateRequest
from state import database as db
from state.models import User

log = structlog.get_logger()
router = APIRouter(prefix="/settings", tags=["settings"])

_SECRET_FIELDS = ("tavily_api_key", "gitlab_token", "dynatrace_api_token", "dynatrace_webhook_secret")
_ALL_FIELDS = (
    "tavily_api_key", "gitlab_url", "gitlab_token", "gitlab_project_id",
    "dynatrace_environment_url", "dynatrace_api_token", "dynatrace_webhook_secret",
)


def _mask(secret: str) -> str:
    """Mask a secret for display: '' if unset, else dots + last 4 chars.

    Args:
        secret: Plaintext secret value.

    Returns:
        Empty string if unset; otherwise '••••' + last 4 characters.
    """
    if not secret:
        return ""
    tail = secret[-4:] if len(secret) >= 4 else secret
    return "••••" + tail


@router.get("", response_model=SettingsResponse)
async def get_settings(user: User = Depends(current_user)) -> SettingsResponse:
    """Return the current user's settings with secrets masked.

    Args:
        user: Authenticated user from session cookie.

    Returns:
        :class:`SettingsResponse` with non-secret fields as-is and secret
        fields replaced by a masked representation.
    """
    us = await db.get_user_settings(user.user_id)
    data = {f: (getattr(us, f, "") if us else "") for f in _ALL_FIELDS}
    for f in _SECRET_FIELDS:
        data[f] = _mask(data[f])
    return SettingsResponse(**data)


@router.put("", response_model=SettingsResponse)
async def put_settings(body: SettingsUpdateRequest, user: User = Depends(current_user)) -> SettingsResponse:
    """Update the current user's settings.

    Omitted (None) fields are left unchanged. For a secret field, an empty
    string keeps the stored value; a non-empty value replaces it. Non-secret
    fields are set as given (empty clears them).

    Args:
        body: Partial update payload.
        user: Authenticated user from session cookie.

    Returns:
        :class:`SettingsResponse` with updated values (secrets masked).
    """
    existing = await db.get_user_settings(user.user_id)
    merged = {f: (getattr(existing, f, "") if existing else "") for f in _ALL_FIELDS}
    incoming = body.model_dump(exclude_none=True)
    for f, value in incoming.items():
        if f in _SECRET_FIELDS and value == "":
            continue  # empty secret => keep existing
        merged[f] = value
    await db.upsert_user_settings(user.user_id, merged)
    log.info("settings_updated", user_id=user.user_id)
    return await get_settings(user)
