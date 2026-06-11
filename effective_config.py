"""Resolve a mission owner's effective integration config (user over global env)."""
from __future__ import annotations

from dataclasses import dataclass

from config import settings
from state import database as db

# Fields a user can override; each falls back to the global env value when unset.
_OVERRIDABLE = (
    "tavily_api_key", "gitlab_url", "gitlab_token", "gitlab_project_id",
    "dynatrace_environment_url", "dynatrace_api_token", "dynatrace_webhook_secret",
)


@dataclass(frozen=True)
class EffectiveSettings:
    """The integration config to use for one mission (owner's values over global)."""

    tavily_api_key: str
    tavily_api_url: str
    web_search_max_results: int
    gitlab_url: str
    gitlab_token: str
    gitlab_project_id: str
    dynatrace_environment_url: str
    dynatrace_api_token: str
    dynatrace_webhook_secret: str


def _from_global() -> dict:
    """Snapshot the current global env-based integration settings."""
    return {
        "tavily_api_key": settings.tavily_api_key,
        "tavily_api_url": settings.tavily_api_url,
        "web_search_max_results": settings.web_search_max_results,
        "gitlab_url": settings.gitlab_url,
        "gitlab_token": settings.gitlab_token,
        "gitlab_project_id": settings.gitlab_project_id,
        "dynatrace_environment_url": settings.dynatrace_environment_url,
        "dynatrace_api_token": settings.dynatrace_api_token,
        "dynatrace_webhook_secret": settings.dynatrace_webhook_secret,
    }


async def resolve_effective_settings(user_id: str | None) -> EffectiveSettings:
    """Merge a user's stored integration config over the global env defaults.

    Args:
        user_id: Mission owner (None => pure global defaults).

    Returns:
        EffectiveSettings; any field the user hasn't set uses the global value.
    """
    values = _from_global()
    if user_id:
        us = await db.get_user_settings(user_id)
        if us is not None:
            for field in _OVERRIDABLE:
                user_val = getattr(us, field, "")
                if user_val:  # non-empty user value overrides global
                    values[field] = user_val
    return EffectiveSettings(**values)
