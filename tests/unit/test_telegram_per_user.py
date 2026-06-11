"""Per-user Telegram routing and inbound authorization tests (SP5).

Verifies that:
- Approval requests route to the mission owner's chat, not the global chat.
- When the owner has no saved chat, the global chat is the fallback.
- Inbound button taps are accepted from the mission owner and from the
  global/admin chat, but rejected from any other Telegram user.
- ``get_user_id_by_telegram_chat_id`` correctly resolves the lookup.
"""

from __future__ import annotations

import pytest

import notifications.telegram_bot as tb
from notifications.telegram_bot import TelegramNotifier
from state import database as db


def _enabled_notifier(global_chat: str = "GLOBAL") -> TelegramNotifier:
    """Build a notifier with the bot enabled and the given global chat id."""
    n = TelegramNotifier()
    n._flag = True
    n._token = "test-token"
    n._chat_id = global_chat
    return n


# --------------------------------------------------------------------------- #
# Outbound routing
# --------------------------------------------------------------------------- #

async def test_approval_routes_to_owner_chat(mock_db, monkeypatch):
    user = await db.create_user("o@x.io", "h")
    await db.upsert_user_settings(user.user_id, {"telegram_chat_id": "OWNER_CHAT"})
    m = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    n = _enabled_notifier()
    sent: dict = {}

    async def fake_send(text, *, reply_markup=None, chat_id=None):
        sent["chat_id"] = chat_id

    monkeypatch.setattr(n, "_safe_send", fake_send)
    await n.send_approval_request("a1", "gitlab_rollback", "roll back", m.mission_id)
    assert sent["chat_id"] == "OWNER_CHAT"


async def test_approval_falls_back_to_global_chat(mock_db, monkeypatch):
    user = await db.create_user("o@x.io", "h")  # no telegram_chat_id stored
    m = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    n = _enabled_notifier("GLOBAL")
    sent: dict = {}

    async def fake_send(text, *, reply_markup=None, chat_id=None):
        sent["chat_id"] = chat_id

    monkeypatch.setattr(n, "_safe_send", fake_send)
    await n.send_approval_request("a1", "gitlab_rollback", "roll back", m.mission_id)
    assert sent["chat_id"] == "GLOBAL"


async def test_notification_routes_to_owner_chat(mock_db, monkeypatch):
    user = await db.create_user("o@x.io", "h")
    await db.upsert_user_settings(user.user_id, {"telegram_chat_id": "OWNER_CHAT"})
    m = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    n = _enabled_notifier()
    sent: dict = {}

    async def fake_send(text, *, reply_markup=None, chat_id=None):
        sent["chat_id"] = chat_id

    monkeypatch.setattr(n, "_safe_send", fake_send)
    await n.send_notification("alert", mission_id=m.mission_id)
    assert sent["chat_id"] == "OWNER_CHAT"


async def test_notification_without_mission_id_uses_global(mock_db, monkeypatch):
    n = _enabled_notifier("GLOBAL")
    sent: dict = {}

    async def fake_send(text, *, reply_markup=None, chat_id=None):
        sent["chat_id"] = chat_id

    monkeypatch.setattr(n, "_safe_send", fake_send)
    await n.send_notification("hello")
    assert sent["chat_id"] == "GLOBAL"


# --------------------------------------------------------------------------- #
# Inbound authorization
# --------------------------------------------------------------------------- #

async def test_inbound_owner_can_decide(mock_db):
    user = await db.create_user("o@x.io", "h")
    await db.upsert_user_settings(user.user_id, {"telegram_chat_id": "OWNER_CHAT"})
    m = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    action = await db.create_action(m.mission_id, "gitlab_rollback", {})
    n = _enabled_notifier()
    applied = await n._apply_decision(action.action_id, "approved", "OWNER_CHAT")
    assert applied is True
    assert (await db.get_action(action.action_id)).status == "approved"


async def test_inbound_non_owner_is_denied_and_action_unchanged(mock_db):
    owner = await db.create_user("o@x.io", "h")
    await db.upsert_user_settings(owner.user_id, {"telegram_chat_id": "OWNER_CHAT"})
    intruder = await db.create_user("bad@x.io", "h")
    await db.upsert_user_settings(intruder.user_id, {"telegram_chat_id": "INTRUDER_CHAT"})
    m = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=owner.user_id)
    action = await db.create_action(m.mission_id, "gitlab_rollback", {})
    n = _enabled_notifier()
    applied = await n._apply_decision(action.action_id, "approved", "INTRUDER_CHAT")
    assert applied is False
    assert (await db.get_action(action.action_id)).status == "pending"  # untouched


async def test_inbound_global_chat_is_authorized(mock_db):
    user = await db.create_user("o@x.io", "h")
    m = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    action = await db.create_action(m.mission_id, "gitlab_rollback", {})
    n = _enabled_notifier("GLOBAL")
    applied = await n._apply_decision(action.action_id, "approved", "GLOBAL")
    assert applied is True


async def test_inbound_none_chat_id_is_denied(mock_db):
    user = await db.create_user("o@x.io", "h")
    m = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    action = await db.create_action(m.mission_id, "gitlab_rollback", {})
    n = _enabled_notifier("GLOBAL")
    applied = await n._apply_decision(action.action_id, "approved", None)
    assert applied is False


async def test_inbound_unknown_chat_id_is_denied(mock_db):
    user = await db.create_user("o@x.io", "h")
    await db.upsert_user_settings(user.user_id, {"telegram_chat_id": "OWNER_CHAT"})
    m = await db.create_mission("g", "INCIDENT_RESPONSE", owner_id=user.user_id)
    action = await db.create_action(m.mission_id, "gitlab_rollback", {})
    n = _enabled_notifier("GLOBAL")
    applied = await n._apply_decision(action.action_id, "approved", "UNKNOWN_CHAT")
    assert applied is False
    assert (await db.get_action(action.action_id)).status == "pending"


async def test_inbound_action_not_found_returns_false(mock_db):
    n = _enabled_notifier("GLOBAL")
    applied = await n._apply_decision("nonexistent-action-id", "approved", "GLOBAL")
    assert applied is False


# --------------------------------------------------------------------------- #
# DB lookup helper
# --------------------------------------------------------------------------- #

async def test_user_lookup_by_chat_id(mock_db):
    user = await db.create_user("o@x.io", "h")
    await db.upsert_user_settings(user.user_id, {"telegram_chat_id": "CHAT-42"})
    assert await db.get_user_id_by_telegram_chat_id("CHAT-42") == user.user_id
    assert await db.get_user_id_by_telegram_chat_id("nope") is None


async def test_user_lookup_by_chat_id_multiple_users(mock_db):
    u1 = await db.create_user("a@x.io", "h")
    u2 = await db.create_user("b@x.io", "h")
    await db.upsert_user_settings(u1.user_id, {"telegram_chat_id": "CHAT-A"})
    await db.upsert_user_settings(u2.user_id, {"telegram_chat_id": "CHAT-B"})
    assert await db.get_user_id_by_telegram_chat_id("CHAT-A") == u1.user_id
    assert await db.get_user_id_by_telegram_chat_id("CHAT-B") == u2.user_id
