"""TelegramNotifier: mobile approvals and notifications over a Telegram bot.

Sends action-approval requests (with inline Approve/Reject buttons) and plain
notifications to a configured chat, and — in local dev — runs a polling loop that
turns button taps into state-layer approvals/rejections.

The bot is strictly opt-in (``settings.telegram_enabled``) and defensive: every
Telegram API call is wrapped so a bot failure can never crash NERVE. When the
bot is disabled (flag off or token/chat missing), every method is a no-op.
"""

from __future__ import annotations

from typing import Literal

import structlog
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from config import settings
from state import database as db

log = structlog.get_logger()

# --- Message + callback constants (no magic strings; CLAUDE.md style rules) --- #
APPROVAL_HEADER = "⚠️ NERVE — ACTION REQUIRED"
BUTTON_APPROVE = "✅ APPROVE"
BUTTON_REJECT = "❌ REJECT"
CALLBACK_APPROVE_PREFIX = "approve:"
CALLBACK_REJECT_PREFIX = "reject:"

APPROVED_BY_TELEGRAM = "telegram"
SOURCE_USER = "user"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
EVENT_ACTION_APPROVED = "ACTION_APPROVED"
EVENT_ACTION_REJECTED = "ACTION_REJECTED"

ANSWER_APPROVED = "Action approved"
ANSWER_REJECTED = "Action rejected"

NotificationLevel = Literal["info", "success", "warning", "critical"]
DEFAULT_LEVEL: NotificationLevel = "info"
LEVEL_EMOJI: dict[str, str] = {
    "info": "ℹ️",
    "success": "✅",
    "warning": "⚠️",
    "critical": "🚨",
}
#: Code point above which a leading character is treated as a status glyph the
#: caller already supplied (so we don't prepend a second emoji).
_GLYPH_MIN_CODEPOINT = 0x7F


class TelegramNotifier:
    """Sends approval requests/notifications and handles inline-button replies."""

    def __init__(self) -> None:
        """Capture config; build no network clients until first use."""
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._flag = settings.telegram_enabled
        self._bot: telegram.Bot | None = None
        self._bot_ready = False
        self._app: Application | None = None
        self._log = log.bind(component="telegram")

    @property
    def enabled(self) -> bool:
        """True only when the flag is set and both token and chat id are present."""
        return bool(self._flag and self._token and self._chat_id)

    # ----------------------------------------------------------------- #
    # Outbound messages
    # ----------------------------------------------------------------- #
    async def send_approval_request(
        self, action_id: str, action_type: str, description: str, mission_id: str
    ) -> None:
        """Send an approval request with inline Approve/Reject buttons.

        Args:
            action_id: Action awaiting approval (encoded in callback_data).
            action_type: Action type (e.g. ``gitlab_rollback``).
            description: Full human-readable description of the action.
            mission_id: Mission the action belongs to.
        """
        if not self.enabled:
            return
        text = self._format_approval(action_id, action_type, description, mission_id)
        await self._safe_send(text, reply_markup=self._approval_keyboard(action_id))

    async def send_notification(self, message: str, level: str = DEFAULT_LEVEL) -> None:
        """Send a plain notification, prefixed with the level's emoji.

        The emoji prefix is skipped when ``message`` already begins with a status
        glyph, so callers may pass a fully-formatted string verbatim.

        Args:
            message: Notification body.
            level: One of ``info``/``success``/``warning``/``critical``.
        """
        if not self.enabled:
            return
        await self._safe_send(self._apply_level(message, level))

    # ----------------------------------------------------------------- #
    # Polling lifecycle (local dev)
    # ----------------------------------------------------------------- #
    async def start_polling(self) -> None:
        """Start the bot in polling mode and register the callback handler.

        No-op (and never raises) when the bot is disabled or already running.
        """
        if not self.enabled or self._app is not None:
            self._log.info("telegram_polling_skipped", enabled=self.enabled)
            return
        try:
            self._app = Application.builder().token(self._token).build()
            self._app.add_handler(CallbackQueryHandler(self._on_callback))
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling()
            self._log.info("telegram_polling_started")
        except Exception as exc:  # noqa: BLE001 — bot startup must never crash NERVE
            self._log.warning("telegram_polling_failed", error=str(exc))
            self._app = None

    async def stop(self) -> None:
        """Gracefully stop polling and shut down any initialized clients."""
        try:
            await self._stop_app()
            if self._bot is not None and self._bot_ready:
                await self._bot.shutdown()
                self._bot_ready = False
            self._log.info("telegram_stopped")
        except Exception as exc:  # noqa: BLE001 — shutdown must never crash NERVE
            self._log.warning("telegram_stop_failed", error=str(exc))

    async def _stop_app(self) -> None:
        """Stop the polling Application if it is running."""
        if self._app is None:
            return
        if self._app.updater is not None and self._app.updater.running:
            await self._app.updater.stop()
        if self._app.running:
            await self._app.stop()
        await self._app.shutdown()
        self._app = None

    # ----------------------------------------------------------------- #
    # Inline-button callback handling
    # ----------------------------------------------------------------- #
    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Apply an Approve/Reject button tap to the action's state.

        Parses the action id, updates the action via the state layer, answers the
        callback, and rewrites the original message to record the decision.
        """
        query = update.callback_query
        if query is None or not query.data:
            return
        try:
            decision, action_id = self._parse_callback(query.data)
            if decision is None:
                return
            await self._apply_decision(action_id, decision)
            await query.answer(text=ANSWER_APPROVED if decision == STATUS_APPROVED else ANSWER_REJECTED)
            await query.edit_message_text(self._decision_text(query, decision))
            self._log.info("telegram_decision_applied", action_id=action_id, decision=decision)
        except Exception as exc:  # noqa: BLE001 — callback errors must never crash the bot
            self._log.warning("telegram_callback_failed", error=str(exc))

    async def _apply_decision(self, action_id: str, decision: str) -> None:
        """Update the action's status and emit the matching audit event."""
        await db.update_action_status(action_id, decision, approved_by=APPROVED_BY_TELEGRAM)
        action = await db.get_action(action_id)
        if action is None:
            return
        event_type = EVENT_ACTION_APPROVED if decision == STATUS_APPROVED else EVENT_ACTION_REJECTED
        await db.emit_event(
            action.mission_id,
            event_type,
            {"action_id": action_id, "approved_by": APPROVED_BY_TELEGRAM},
            SOURCE_USER,
        )

    # ----------------------------------------------------------------- #
    # Formatting helpers
    # ----------------------------------------------------------------- #
    @staticmethod
    def _format_approval(action_id: str, action_type: str, description: str, mission_id: str) -> str:
        """Build the approval-request message body (plain text)."""
        return (
            f"{APPROVAL_HEADER}\n\n"
            f"Mission: {mission_id}\n"
            f"Action: {action_type}\n"
            f"Action ID: {action_id}\n\n"
            f"{description}"
        )

    @staticmethod
    def _approval_keyboard(action_id: str) -> InlineKeyboardMarkup:
        """Build the inline keyboard with Approve/Reject buttons in one row."""
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(BUTTON_APPROVE, callback_data=f"{CALLBACK_APPROVE_PREFIX}{action_id}"),
                    InlineKeyboardButton(BUTTON_REJECT, callback_data=f"{CALLBACK_REJECT_PREFIX}{action_id}"),
                ]
            ]
        )

    @staticmethod
    def _apply_level(message: str, level: str) -> str:
        """Prefix the level emoji unless the message already leads with a glyph."""
        if message and ord(message[0]) > _GLYPH_MIN_CODEPOINT:
            return message
        emoji = LEVEL_EMOJI.get(level, LEVEL_EMOJI[DEFAULT_LEVEL])
        return f"{emoji} {message}"

    @staticmethod
    def _parse_callback(data: str) -> tuple[str | None, str]:
        """Decode ``approve:{id}`` / ``reject:{id}`` callback data."""
        if data.startswith(CALLBACK_APPROVE_PREFIX):
            return STATUS_APPROVED, data[len(CALLBACK_APPROVE_PREFIX):]
        if data.startswith(CALLBACK_REJECT_PREFIX):
            return STATUS_REJECTED, data[len(CALLBACK_REJECT_PREFIX):]
        return None, ""

    @staticmethod
    def _decision_text(query: telegram.CallbackQuery, decision: str) -> str:
        """Rewrite the original message to record the decision and who made it."""
        user = query.from_user
        who = (user.username or user.full_name) if user is not None else APPROVED_BY_TELEGRAM
        verb = "APPROVED ✅" if decision == STATUS_APPROVED else "REJECTED ❌"
        original = query.message.text if query.message is not None else ""
        return f"{original}\n\n— {verb} by {who}"

    # ----------------------------------------------------------------- #
    # Low-level send
    # ----------------------------------------------------------------- #
    async def _safe_send(self, text: str, *, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        """Send a message, swallowing and logging any Telegram/network failure."""
        try:
            bot = await self._ensure_bot()
            await bot.send_message(chat_id=self._chat_id, text=text, reply_markup=reply_markup)
        except Exception as exc:  # noqa: BLE001 — telegram failures must never crash NERVE
            self._log.warning("telegram_send_failed", error=str(exc))

    async def _ensure_bot(self) -> telegram.Bot:
        """Lazily build and initialize a standalone Bot for outbound sends."""
        if self._bot is None:
            self._bot = telegram.Bot(self._token)
        if not self._bot_ready:
            await self._bot.initialize()
            self._bot_ready = True
        return self._bot


#: Module-level singleton, mirroring the ``connection_manager``/``settings``
#: pattern. main.py, the incident workflow, and the actions route all share it.
telegram_notifier = TelegramNotifier()
