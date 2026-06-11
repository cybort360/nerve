"""SP3.2: per-user settings API (masked GET, encrypt-on-PUT, owner-scoped)."""
from __future__ import annotations

from routes import settings as settings_routes
from routes.schemas import SettingsUpdateRequest
from state import database as db


async def test_get_settings_masks_secrets(mock_db):
    user = await db.create_user("a@b.io", "h")
    await db.upsert_user_settings(user.user_id, {"gitlab_token": "glpat-abcd1234", "gitlab_url": "https://gl.example"})
    out = await settings_routes.get_settings(user)
    assert out.gitlab_url == "https://gl.example"        # non-secret returned as-is
    assert "glpat" not in out.gitlab_token               # secret MASKED
    assert out.gitlab_token.endswith("1234")             # shows last 4
    assert out.tavily_api_key == ""                      # unset secret → empty


async def test_put_creates_and_encrypts(mock_db):
    user = await db.create_user("a@b.io", "h")
    await settings_routes.put_settings(
        SettingsUpdateRequest(gitlab_token="glpat-newtoken", gitlab_project_id="42"), user
    )
    stored = await db.get_user_settings(user.user_id)
    assert stored.gitlab_token == "glpat-newtoken"
    assert stored.gitlab_project_id == "42"
    raw = await db.get_user_settings_collection().find_one({"user_id": user.user_id})
    assert raw["gitlab_token"] != "glpat-newtoken"       # encrypted at rest


async def test_put_empty_secret_keeps_existing(mock_db):
    user = await db.create_user("a@b.io", "h")
    await settings_routes.put_settings(SettingsUpdateRequest(gitlab_token="glpat-keep"), user)
    # second PUT with empty token but a new project id must NOT wipe the token
    await settings_routes.put_settings(SettingsUpdateRequest(gitlab_token="", gitlab_project_id="99"), user)
    stored = await db.get_user_settings(user.user_id)
    assert stored.gitlab_token == "glpat-keep"
    assert stored.gitlab_project_id == "99"


async def test_get_settings_is_owner_scoped(mock_db):
    alice = await db.create_user("alice@x.io", "h")
    bob = await db.create_user("bob@x.io", "h")
    await db.upsert_user_settings(alice.user_id, {"gitlab_token": "alice-secret"})
    out = await settings_routes.get_settings(bob)  # bob has no settings
    assert out.gitlab_token == ""                  # bob sees only his own (empty)
