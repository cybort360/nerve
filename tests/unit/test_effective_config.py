"""Unit tests for the effective-config resolver."""

from effective_config import resolve_effective_settings
from state import database as db


async def test_resolve_falls_back_to_global_when_unset(mock_db):
    eff = await resolve_effective_settings("u1")  # no user settings stored
    from config import settings as g
    assert eff.gitlab_url == g.gitlab_url
    assert eff.tavily_api_key == g.tavily_api_key


async def test_resolve_uses_user_value_over_global(mock_db):
    await db.upsert_user_settings("u1", {"gitlab_token": "user-token", "tavily_api_key": "user-tavily"})
    eff = await resolve_effective_settings("u1")
    assert eff.gitlab_token == "user-token"
    assert eff.tavily_api_key == "user-tavily"


async def test_resolve_none_user_is_pure_global(mock_db):
    eff = await resolve_effective_settings(None)
    from config import settings as g
    assert eff.gitlab_token == g.gitlab_token
