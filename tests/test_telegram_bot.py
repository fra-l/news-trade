"""Tests for TelegramBotService.

All Telegram API interactions are mocked — no real network calls.
The Application and bot objects are replaced with MagicMock / AsyncMock so
that the service logic (approval futures, command handlers, access control) can
be exercised in isolation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.config import Settings
from news_trade.models.signals import SignalDirection, TradeSignal
from news_trade.services.tables import Base, OrderRow, TradeSignalRow
from news_trade.services.telegram_bot import TelegramBotService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs: Any) -> Settings:
    defaults: dict[str, object] = dict(
        telegram_bot_token="test-token",
        telegram_chat_id=12345,
        telegram_signal_approval=False,
        telegram_approval_timeout_sec=1,  # short timeout for tests
    )
    return Settings(**(defaults | kwargs))  # type: ignore[call-arg]


def _make_session() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _make_signal(**kwargs: Any) -> TradeSignal:
    defaults: dict[str, object] = dict(
        signal_id="sig-1",
        event_id="ev-1",
        ticker="AAPL",
        direction=SignalDirection.LONG,
        conviction=0.75,
        suggested_qty=10,
        confidence_score=0.80,
        passed_confidence_gate=True,
    )
    return TradeSignal(**(defaults | kwargs))


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
# TestRequestApproval
# ---------------------------------------------------------------------------


class TestRequestApproval:
    """Tests for TelegramBotService.request_approval()."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.bot = _make_bot(_make_settings(telegram_approval_timeout_sec=1))
        self.app = _mock_application()
        self.bot._app = self.app

    async def test_approve_path(self) -> None:
        """Future resolved with True → method returns True."""
        signal = _make_signal()

        async def _resolve_approve() -> None:
            await asyncio.sleep(0.05)
            future = self.bot._pending.get(signal.signal_id)
            if future and not future.done():
                future.set_result(True)

        task = asyncio.create_task(_resolve_approve())
        result = await self.bot.request_approval(signal)
        await task
        assert result is True

    async def test_block_path(self) -> None:
        """Future resolved with False → method returns False."""
        signal = _make_signal()

        async def _resolve_block() -> None:
            await asyncio.sleep(0.05)
            future = self.bot._pending.get(signal.signal_id)
            if future and not future.done():
                future.set_result(False)

        task = asyncio.create_task(_resolve_block())
        result = await self.bot.request_approval(signal)
        await task
        assert result is False

    async def test_timeout_auto_proceeds(self) -> None:
        """No response within timeout → auto-proceed (returns True)."""
        signal = _make_signal()
        # telegram_approval_timeout_sec=1 → times out quickly
        result = await self.bot.request_approval(signal)
        assert result is True

    async def test_disabled_when_no_app(self) -> None:
        """Bot not started (app=None) → always returns True."""
        bot = _make_bot()
        bot._app = None
        result = await bot.request_approval(_make_signal())
        assert result is True

    async def test_telegram_send_failure_fails_open(self) -> None:
        """If send_message raises, approval returns True (fail-open)."""
        self.app.bot.send_message.side_effect = RuntimeError("network error")
        result = await self.bot.request_approval(_make_signal())
        assert result is True

    async def test_pending_cleared_after_resolve(self) -> None:
        """_pending dict is cleaned up after approval completes."""
        signal = _make_signal()

        async def _resolve() -> None:
            await asyncio.sleep(0.05)
            future = self.bot._pending.get(signal.signal_id)
            if future and not future.done():
                future.set_result(True)

        task = asyncio.create_task(_resolve())
        await self.bot.request_approval(signal)
        await task
        assert signal.signal_id not in self.bot._pending


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

    async def test_wrong_chat_id_ignored_for_halt(self) -> None:
        update = _mock_update(chat_id=99999)  # not the configured 12345
        await self.bot._cmd_halt(update, _mock_context())
        assert self.bot.operator_halt is False  # flag not set
        update.effective_message.reply_text.assert_not_called()

    async def test_wrong_chat_id_ignored_for_status(self) -> None:
        update = _mock_update(chat_id=99999)
        await self.bot._cmd_status(update, _mock_context())
        update.effective_message.reply_text.assert_not_called()

    async def test_correct_chat_id_allowed(self) -> None:
        update = _mock_update(chat_id=12345)
        await self.bot._cmd_status(update, _mock_context())
        update.effective_message.reply_text.assert_called_once()


# ---------------------------------------------------------------------------
# TestCommandHandlers
# ---------------------------------------------------------------------------


class TestCommandHandlers:
    """Test each /command handler produces a reply and manages state correctly."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.settings = _make_settings()
        self.factory = _make_session()
        self.bot = TelegramBotService(self.settings, self.factory)
        self.app = _mock_application()
        self.bot._app = self.app

    async def test_halt_sets_flag_and_replies(self) -> None:
        update = _mock_update()
        await self.bot._cmd_halt(update, _mock_context())
        assert self.bot.operator_halt is True
        update.effective_message.reply_text.assert_called_once()

    async def test_resume_clears_flag_and_replies(self) -> None:
        self.bot.operator_halt = True
        update = _mock_update()
        await self.bot._cmd_resume(update, _mock_context())
        assert self.bot.operator_halt is False
        update.effective_message.reply_text.assert_called_once()

    async def test_help_replies(self) -> None:
        update = _mock_update()
        await self.bot._cmd_help(update, _mock_context())
        update.effective_message.reply_text.assert_called_once()

    async def test_status_replies(self) -> None:
        update = _mock_update()
        await self.bot._cmd_status(update, _mock_context())
        text: str = update.effective_message.reply_text.call_args[0][0]
        assert "status" in text.lower()

    async def test_portfolio_empty_db_replies(self) -> None:
        update = _mock_update()
        await self.bot._cmd_portfolio(update, _mock_context())
        update.effective_message.reply_text.assert_called_once()
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
# TestCallbackHandler
# ---------------------------------------------------------------------------


class TestCallbackHandler:
    """_on_callback resolves pending futures and edits the message."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.bot = _make_bot()
        self.app = _mock_application()
        self.bot._app = self.app

    def _make_callback_update(
        self, data: str, chat_id: int = 12345
    ) -> MagicMock:
        update = MagicMock()
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id
        query = AsyncMock()
        query.data = data
        query.answer = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock()
        query.message.text = "Signal approval required"
        update.callback_query = query
        return update

    async def test_approve_callback_resolves_future(self) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self.bot._pending["sig-1"] = future

        update = self._make_callback_update("approve:sig-1")
        await self.bot._on_callback(update, _mock_context())

        assert future.done()
        assert future.result() is True

    async def test_block_callback_resolves_future(self) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self.bot._pending["sig-1"] = future

        update = self._make_callback_update("block:sig-1")
        await self.bot._on_callback(update, _mock_context())

        assert future.done()
        assert future.result() is False

    async def test_unknown_callback_data_is_noop(self) -> None:
        update = self._make_callback_update("unknown:sig-1")
        await self.bot._on_callback(update, _mock_context())  # must not raise

    async def test_callback_wrong_chat_id_ignored(self) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self.bot._pending["sig-1"] = future

        update = self._make_callback_update("approve:sig-1", chat_id=99999)
        await self.bot._on_callback(update, _mock_context())

        assert not future.done()  # future was not resolved


# ---------------------------------------------------------------------------
# TestStop
# ---------------------------------------------------------------------------


class TestStop:
    """stop() resolves all pending futures and shuts down the Application."""

    async def test_stop_resolves_pending_futures(self) -> None:
        bot = _make_bot()
        app = _mock_application()
        bot._app = app

        loop = asyncio.get_running_loop()
        f1: asyncio.Future[bool] = loop.create_future()
        f2: asyncio.Future[bool] = loop.create_future()
        bot._pending = {"sig-1": f1, "sig-2": f2}

        await bot.stop()

        assert f1.done() and f1.result() is True
        assert f2.done() and f2.result() is True
        assert bot._pending == {}

    async def test_stop_calls_app_shutdown(self) -> None:
        bot = _make_bot()
        app = _mock_application()
        bot._app = app

        await bot.stop()

        app.updater.stop.assert_awaited_once()
        app.stop.assert_awaited_once()
        app.shutdown.assert_awaited_once()
