"""Tests for TelegramBotService.

All Telegram API interactions are mocked — no real network calls.
The Application and bot objects are replaced with MagicMock / AsyncMock so
that the service logic (notifications, command handlers, access control) can
be exercised in isolation.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.config import Settings
from news_trade.services.tables import Base, OrderRow, TradeSignalRow
from news_trade.services.telegram_bot import TelegramBotService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs: Any) -> Settings:
    defaults: dict[str, object] = dict(
        telegram_bot_token="test-token",
        telegram_chat_id=12345,
    )
    return Settings(**(defaults | kwargs))  # type: ignore[call-arg]


def _make_session() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _make_bot(settings: Settings | None = None) -> TelegramBotService:
    s = settings or _make_settings()
    factory = _make_session()
    return TelegramBotService(s, factory)


def _mock_application() -> MagicMock:
    """Build a MagicMock that mimics telegram.ext.Application."""
    app = MagicMock()
    app.bot = AsyncMock()
    app.updater = AsyncMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.add_handler = MagicMock()
    return app


def _mock_update(chat_id: int = 12345) -> MagicMock:
    """Build a MagicMock Update with the given chat_id."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_message = AsyncMock()
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None
    return update


def _mock_context(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


# ---------------------------------------------------------------------------
# TestAccessControl
# ---------------------------------------------------------------------------


class TestAccessControl:
    """Messages from unknown chat IDs must be silently ignored."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.bot = _make_bot()
        self.app = _mock_application()
        self.bot._app = self.app

    async def test_wrong_chat_id_ignored_for_status(self) -> None:
        update = _mock_update(chat_id=99999)
        await self.bot._cmd_status(update, _mock_context())
        update.effective_message.reply_text.assert_not_called()

    async def test_wrong_chat_id_ignored_for_help(self) -> None:
        update = _mock_update(chat_id=99999)
        await self.bot._cmd_help(update, _mock_context())
        update.effective_message.reply_text.assert_not_called()

    async def test_correct_chat_id_allowed(self) -> None:
        update = _mock_update(chat_id=12345)
        await self.bot._cmd_status(update, _mock_context())
        update.effective_message.reply_text.assert_called_once()


# ---------------------------------------------------------------------------
# TestCommandHandlers
# ---------------------------------------------------------------------------


class TestCommandHandlers:
    """Test each /command handler produces a reply."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.settings = _make_settings()
        self.factory = _make_session()
        self.bot = TelegramBotService(self.settings, self.factory)
        self.app = _mock_application()
        self.bot._app = self.app

    async def test_help_replies(self) -> None:
        update = _mock_update()
        await self.bot._cmd_help(update, _mock_context())
        update.effective_message.reply_text.assert_called_once()
        text: str = update.effective_message.reply_text.call_args[0][0]
        # no halt or resume in the help text
        assert "/halt" not in text
        assert "/resume" not in text

    async def test_status_replies_running(self) -> None:
        update = _mock_update()
        await self.bot._cmd_status(update, _mock_context())
        text: str = update.effective_message.reply_text.call_args[0][0]
        assert "running" in text.lower()
        assert "as of" in text.lower()

    async def test_portfolio_empty_db_replies(self) -> None:
        update = _mock_update()
        await self.bot._cmd_portfolio(update, _mock_context())
        text: str = update.effective_message.reply_text.call_args[0][0]
        assert "no recent orders" in text.lower()

    async def test_portfolio_with_orders(self) -> None:
        with self.factory() as session:
            row = OrderRow(
                order_id="ord-1",
                signal_id="sig-1",
                ticker="AAPL",
                side="buy",
                qty=10,
                status="filled",
                created_at=datetime(2026, 1, 1, 9, 30),
            )
            session.add(row)
            session.commit()

        update = _mock_update()
        await self.bot._cmd_portfolio(update, _mock_context())
        text: str = update.effective_message.reply_text.call_args[0][0]
        assert "AAPL" in text

    async def test_signals_empty_db_replies(self) -> None:
        update = _mock_update()
        await self.bot._cmd_signals(update, _mock_context())
        text: str = update.effective_message.reply_text.call_args[0][0]
        assert "no recent signals" in text.lower()

    async def test_signals_with_rows(self) -> None:
        with self.factory() as session:
            row = TradeSignalRow(
                signal_id="sig-1",
                event_id="ev-1",
                ticker="MSFT",
                direction="long",
                conviction=0.8,
                suggested_qty=5,
                approved=1,
                created_at=datetime(2026, 1, 1, 9, 30),
            )
            session.add(row)
            session.commit()

        update = _mock_update()
        await self.bot._cmd_signals(update, _mock_context())
        text: str = update.effective_message.reply_text.call_args[0][0]
        assert "MSFT" in text

    async def test_signals_custom_limit(self) -> None:
        """Custom limit arg is accepted; reply is sent without error."""
        update = _mock_update()
        await self.bot._cmd_signals(update, _mock_context(args=["3"]))
        update.effective_message.reply_text.assert_called_once()

    async def test_signals_invalid_arg_uses_default(self) -> None:
        """Non-integer limit arg falls back to default; reply is still sent."""
        update = _mock_update()
        await self.bot._cmd_signals(update, _mock_context(args=["abc"]))
        update.effective_message.reply_text.assert_called_once()


# ---------------------------------------------------------------------------
# TestNotify
# ---------------------------------------------------------------------------


class TestNotify:
    """TelegramBotService.notify() calls bot.send_message with correct params."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.bot = _make_bot()
        self.app = _mock_application()
        self.bot._app = self.app

    async def test_sends_to_configured_chat_id(self) -> None:
        await self.bot.notify("hello")
        self.app.bot.send_message.assert_awaited_once_with(
            chat_id=12345, text="hello"
        )

    async def test_no_app_is_noop(self) -> None:
        self.bot._app = None
        await self.bot.notify("hello")  # must not raise

    async def test_send_failure_is_swallowed(self) -> None:
        self.app.bot.send_message.side_effect = RuntimeError("network")
        await self.bot.notify("hello")  # must not raise


# ---------------------------------------------------------------------------
# TestRedisNotifications
# ---------------------------------------------------------------------------


class TestRedisNotifications:
    """_redis_listener forwards system_halted and trade_executed to notify()."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.bot = _make_bot()
        self.app = _mock_application()
        self.bot._app = self.app

    def _make_pubsub(self, messages: list[dict]) -> MagicMock:
        """Build a mock pubsub that yields the given messages then stops."""

        async def _listen():  # type: ignore[return]
            for msg in messages:
                yield msg

        pubsub = MagicMock()
        pubsub.listen = _listen
        return pubsub

    async def test_system_halted_notification(self) -> None:
        payload = json.dumps({"max_drawdown_pct": 0.05})
        pubsub = self._make_pubsub([
            {"type": "message", "channel": "system_halted", "data": payload}
        ])
        event_bus = AsyncMock()
        event_bus.subscribe = AsyncMock(return_value=pubsub)

        await self.bot._redis_listener(event_bus)

        text: str = self.app.bot.send_message.call_args[1]["text"]
        assert "HALTED" in text
        assert "0.05" in text

    async def test_trade_executed_notification(self) -> None:
        payload = json.dumps({
            "ticker": "NVDA",
            "side": "buy",
            "qty": 5,
            "order_id": "ord-123",
            "status": "submitted",
        })
        pubsub = self._make_pubsub([
            {"type": "message", "channel": "trade_executed", "data": payload}
        ])
        event_bus = AsyncMock()
        event_bus.subscribe = AsyncMock(return_value=pubsub)

        await self.bot._redis_listener(event_bus)

        text: str = self.app.bot.send_message.call_args[1]["text"]
        assert "NVDA" in text
        assert "BUY" in text
        assert "ord-123" in text

    async def test_non_message_type_skipped(self) -> None:
        """subscribe/psubscribe confirmations must not trigger a notification."""
        pubsub = self._make_pubsub([
            {"type": "subscribe", "channel": "system_halted", "data": 1}
        ])
        event_bus = AsyncMock()
        event_bus.subscribe = AsyncMock(return_value=pubsub)

        await self.bot._redis_listener(event_bus)

        self.app.bot.send_message.assert_not_called()

    async def test_unknown_channel_skipped(self) -> None:
        pubsub = self._make_pubsub([
            {"type": "message", "channel": "unknown_channel", "data": "{}"}
        ])
        event_bus = AsyncMock()
        event_bus.subscribe = AsyncMock(return_value=pubsub)

        await self.bot._redis_listener(event_bus)

        self.app.bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# TestStop
# ---------------------------------------------------------------------------


class TestStop:
    """stop() shuts down the Application cleanly."""

    async def test_stop_calls_app_shutdown(self) -> None:
        bot = _make_bot()
        app = _mock_application()
        bot._app = app

        await bot.stop()

        app.updater.stop.assert_awaited_once()
        app.stop.assert_awaited_once()
        app.shutdown.assert_awaited_once()

    async def test_stop_with_no_app_is_noop(self) -> None:
        bot = _make_bot()
        bot._app = None
        await bot.stop()  # must not raise


# ---------------------------------------------------------------------------
# TestStopCommand
# ---------------------------------------------------------------------------


def _mock_callback_query(user_id: int = 12345) -> MagicMock:
    """Build a MagicMock CallbackQuery with the given user_id."""
    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _mock_update_with_query(user_id: int = 12345) -> MagicMock:
    """Build a MagicMock Update carrying a callback query (button tap)."""
    update = MagicMock()
    update.callback_query = _mock_callback_query(user_id)
    return update


class TestStopCommand:
    """/stop command handler — operator-initiated loop exit + position closure."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.callback_called = False

        def _callback() -> None:
            self.callback_called = True

        self.bot = TelegramBotService(
            _make_settings(), _make_session(), stop_callback=_callback
        )
        self.app = _mock_application()
        self.bot._app = self.app

    async def test_stop_sends_confirmation_buttons(self) -> None:
        """/stop sends a message with an inline keyboard; callback not yet called."""
        update = _mock_update(chat_id=12345)
        await self.bot._cmd_stop(update, _mock_context())

        assert not self.callback_called
        update.effective_message.reply_text.assert_called_once()
        call_kwargs = update.effective_message.reply_text.call_args
        # reply_markup keyword arg must be present (the inline keyboard)
        assert call_kwargs.kwargs.get("reply_markup") is not None
        text: str = call_kwargs.args[0]
        assert "sure" in text.lower()

    async def test_confirm_button_executes_stop(self) -> None:
        """Tapping 'Yes, stop & close all' calls the callback and edits the message."""
        update = _mock_update_with_query(user_id=12345)
        await self.bot._cb_stop_confirm(update, _mock_context())

        assert self.callback_called
        update.callback_query.answer.assert_called_once()
        update.callback_query.edit_message_text.assert_called_once()

    async def test_cancel_button_does_not_execute_stop(self) -> None:
        """Tapping 'Cancel' does NOT call the callback and edits the message."""
        update = _mock_update_with_query(user_id=12345)
        await self.bot._cb_stop_cancel(update, _mock_context())

        assert not self.callback_called
        update.callback_query.answer.assert_called_once()
        update.callback_query.edit_message_text.assert_called_once()
        text: str = update.callback_query.edit_message_text.call_args.args[0]
        assert "cancelled" in text.lower()

    async def test_confirm_button_unauthorised_ignored(self) -> None:
        """Callback query from wrong user_id is silently dismissed."""
        update = _mock_update_with_query(user_id=99999)
        await self.bot._cb_stop_confirm(update, _mock_context())

        assert not self.callback_called
        update.callback_query.answer.assert_called_once()
        update.callback_query.edit_message_text.assert_not_called()

    async def test_stop_unauthorised_ignored(self) -> None:
        """Unauthorised /stop: callback is NOT called and no reply is sent."""
        update = _mock_update(chat_id=99999)
        await self.bot._cmd_stop(update, _mock_context())

        assert not self.callback_called
        update.effective_message.reply_text.assert_not_called()

    async def test_stop_no_callback_replies_not_available(self) -> None:
        """When stop_callback is None, /stop replies 'Stop not available.'"""
        bot = TelegramBotService(_make_settings(), _make_session(), stop_callback=None)
        bot._app = self.app
        update = _mock_update(chat_id=12345)
        await bot._cmd_stop(update, _mock_context())

        assert not self.callback_called
        update.effective_message.reply_text.assert_called_once()
        text: str = update.effective_message.reply_text.call_args[0][0]
        assert "not available" in text.lower()

    async def test_help_mentions_stop(self) -> None:
        """/help output lists the /stop command."""
        update = _mock_update(chat_id=12345)
        await self.bot._cmd_help(update, _mock_context())
        text: str = update.effective_message.reply_text.call_args[0][0]
        assert "/stop" in text
