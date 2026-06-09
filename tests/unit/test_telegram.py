"""Unit tests for the Telegram notifier (notifications/telegram_bot.py).

The ``telegram.Bot`` client is mocked in every test — no token, no network. The
state layer is patched per test so callback handling is exercised in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import notifications.telegram_bot as tb


class _FakeBot:
    """Stand-in for ``telegram.Bot`` exposing the async methods we call."""

    def __init__(self) -> None:
        self.initialize = AsyncMock()
        self.send_message = AsyncMock()
        self.shutdown = AsyncMock()


@pytest.fixture
def fake_bot(monkeypatch):
    """Patch ``telegram.Bot`` so the notifier builds a controllable fake."""
    bot = _FakeBot()
    monkeypatch.setattr(tb.telegram, "Bot", lambda token: bot)
    return bot


def _enabled_notifier(monkeypatch) -> tb.TelegramNotifier:
    """Build a notifier with the bot enabled and credentials present."""
    monkeypatch.setattr(tb.settings, "telegram_enabled", True)
    monkeypatch.setattr(tb.settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(tb.settings, "telegram_chat_id", "999")
    return tb.TelegramNotifier()


def _make_callback_update(data: str):
    """Build a fake Update carrying a callback query with ``data``."""
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user = SimpleNamespace(username="ops", full_name="Ops Eng")
    query.message = SimpleNamespace(text="original message")
    return SimpleNamespace(callback_query=query), query


async def test_send_approval_request_formats_message_and_keyboard(monkeypatch, fake_bot):
    notifier = _enabled_notifier(monkeypatch)

    await notifier.send_approval_request(
        "act-1", "gitlab_rollback", "Roll back deployment 7.", "mis-1"
    )

    fake_bot.send_message.assert_awaited_once()
    kwargs = fake_bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == "999"
    text = kwargs["text"]
    assert tb.APPROVAL_HEADER in text
    assert "mis-1" in text
    assert "gitlab_rollback" in text
    assert "Roll back deployment 7." in text
    assert "act-1" in text

    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == [tb.BUTTON_APPROVE, tb.BUTTON_REJECT]
    assert buttons[0].callback_data == "approve:act-1"
    assert buttons[1].callback_data == "reject:act-1"


@pytest.mark.parametrize(
    "level,emoji",
    [("info", "ℹ️"), ("success", "✅"), ("warning", "⚠️"), ("critical", "🚨")],
)
async def test_send_notification_prefixes_level_emoji(monkeypatch, fake_bot, level, emoji):
    notifier = _enabled_notifier(monkeypatch)

    await notifier.send_notification("system nominal", level=level)

    assert fake_bot.send_message.await_args.kwargs["text"] == f"{emoji} system nominal"


async def test_approve_callback_calls_update_action_status_approved(monkeypatch, fake_bot):
    notifier = _enabled_notifier(monkeypatch)
    update_status = AsyncMock()
    monkeypatch.setattr(tb.db, "update_action_status", update_status)
    monkeypatch.setattr(tb.db, "get_action", AsyncMock(return_value=None))  # skip audit emit
    update, query = _make_callback_update("approve:act-9")

    await notifier._on_callback(update, None)

    update_status.assert_awaited_once_with("act-9", "approved", approved_by="telegram")
    query.answer.assert_awaited_once_with(text=tb.ANSWER_APPROVED)
    query.edit_message_text.assert_awaited_once()


async def test_reject_callback_calls_update_action_status_rejected(monkeypatch, fake_bot):
    notifier = _enabled_notifier(monkeypatch)
    update_status = AsyncMock()
    monkeypatch.setattr(tb.db, "update_action_status", update_status)
    monkeypatch.setattr(tb.db, "get_action", AsyncMock(return_value=None))
    update, query = _make_callback_update("reject:act-9")

    await notifier._on_callback(update, None)

    update_status.assert_awaited_once_with("act-9", "rejected", approved_by="telegram")
    query.answer.assert_awaited_once_with(text=tb.ANSWER_REJECTED)
    query.edit_message_text.assert_awaited_once()


async def test_callback_emits_audit_event_when_action_exists(monkeypatch, fake_bot):
    notifier = _enabled_notifier(monkeypatch)
    monkeypatch.setattr(tb.db, "update_action_status", AsyncMock())
    monkeypatch.setattr(
        tb.db, "get_action", AsyncMock(return_value=SimpleNamespace(mission_id="mis-7"))
    )
    emit = AsyncMock()
    monkeypatch.setattr(tb.db, "emit_event", emit)
    update, _ = _make_callback_update("approve:act-2")

    await notifier._on_callback(update, None)

    emit.assert_awaited_once()
    args = emit.await_args.args
    assert args[0] == "mis-7"
    assert args[1] == tb.EVENT_ACTION_APPROVED


async def test_methods_are_noops_when_disabled(monkeypatch):
    monkeypatch.setattr(tb.settings, "telegram_enabled", False)
    monkeypatch.setattr(tb.settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(tb.settings, "telegram_chat_id", "999")
    built: list[str] = []
    monkeypatch.setattr(tb.telegram, "Bot", lambda token: built.append(token) or MagicMock())
    notifier = tb.TelegramNotifier()

    assert notifier.enabled is False
    await notifier.send_approval_request("a", "gitlab_rollback", "desc", "m")
    await notifier.send_notification("hello", "info")
    await notifier.start_polling()

    assert built == []  # no Bot/Application ever constructed when disabled


async def test_disabled_when_token_or_chat_missing(monkeypatch):
    monkeypatch.setattr(tb.settings, "telegram_enabled", True)
    monkeypatch.setattr(tb.settings, "telegram_bot_token", "")
    monkeypatch.setattr(tb.settings, "telegram_chat_id", "999")
    assert tb.TelegramNotifier().enabled is False
