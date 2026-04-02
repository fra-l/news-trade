"""Telegram Bot service — read-only operator observer for the trading system.

Provides:
- Push notifications when system-level events occur (drawdown halt, trade executed).
- Read-only query commands: /status, /portfolio, /signals, /help.

The bot never blocks the pipeline or influences trading decisions.
The trading system runs fully automatically; the bot is for observation only.

The service is fully optional. When ``settings.telegram_bot_token`` is empty or
``settings.telegram_chat_id`` is 0 the bot is disabled and no Telegram dependency
is exercised at runtime.

Usage::

    bot = TelegramBotService(settings, session_factory)
    await bot.start(event_bus)   # call once at startup
    ...
    await bot.stop()             # call in the finally block
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from news_trade.config import Settings
from news_trade.services.tables import OrderRow, TradeSignalRow

if TYPE_CHECKING:
    from news_trade.services.event_bus import EventBus

# Shorthand so handler signatures fit on one line.
_Ctx = ContextTypes.DEFAULT_TYPE

logger = logging.getLogger(__name__)

# Redis channels the listener subscribes to
_HALTED_CHANNEL = "system_halted"
_TRADE_CHANNEL = "trade_executed"


class TelegramBotService:
    """Async Telegram bot — read-only observer for the trading pipeline.

    Subscribes to Redis push events and forwards them to the operator chat.
    Provides query commands so the operator can inspect pipeline state on demand.
    Never blocks or influences trading decisions.

    Lifecycle::

        bot = TelegramBotService(settings, session_factory)
        await bot.start(event_bus)
        # ... trading loop runs unattended ...
        await bot.stop()

    Thread safety: all methods must be called from the same asyncio event loop.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._app: Application | None = None  # type: ignore[type-arg]
        self._redis_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, event_bus: EventBus) -> None:
        """Build the Application, register handlers, and start polling."""
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            logger.info("TelegramBotService: disabled (no token or chat_id configured)")
            return

        self._app = (
            Application.builder()
            .token(self._settings.telegram_bot_token)
            .build()
        )

        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("portfolio", self._cmd_portfolio))
        self._app.add_handler(CommandHandler("signals", self._cmd_signals))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]

        self._redis_task = asyncio.create_task(
            self._redis_listener(event_bus), name="telegram_redis_listener"
        )

        logger.info(
            "TelegramBotService started (chat_id=%d)",
            self._settings.telegram_chat_id,
        )
        await self.notify("Trading system started. Send /help for available commands.")

    async def stop(self) -> None:
        """Shut down the bot gracefully."""
        if self._redis_task is not None:
            self._redis_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._redis_task

        if self._app is not None:
            try:
                await self._app.updater.stop()  # type: ignore[union-attr]
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                logger.exception("TelegramBotService: error during shutdown")
        logger.info("TelegramBotService stopped")

    # ------------------------------------------------------------------
    # Push notifications
    # ------------------------------------------------------------------

    async def notify(self, text: str) -> None:
        """Send a plain-text message to the configured operator chat."""
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(
                chat_id=self._settings.telegram_chat_id, text=text
            )
        except Exception:
            logger.exception("TelegramBotService: failed to send notification")

    # ------------------------------------------------------------------
    # Command handlers (read-only)
    # ------------------------------------------------------------------

    def _is_authorised(self, update: Update) -> bool:
        """Return True only if the message comes from the configured chat."""
        chat = update.effective_chat
        if chat is None:
            return False
        return chat.id == self._settings.telegram_chat_id

    async def _cmd_help(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return
        text = (
            "Available commands:\n"
            "/status    — system state and timestamp\n"
            "/portfolio — recent orders and open positions\n"
            "/signals N — last N trade signals (default 5)\n"
            "/help      — this message"
        )
        await update.effective_message.reply_text(text)  # type: ignore[union-attr]

    async def _cmd_status(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return
        text = (
            "System status: running (fully automatic)\n"
            f"Timestamp: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        await update.effective_message.reply_text(text)  # type: ignore[union-attr]

    async def _cmd_portfolio(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return
        with self._session_factory() as session:
            rows = (
                session.query(OrderRow)
                .order_by(OrderRow.created_at.desc())
                .limit(10)
                .all()
            )
        if not rows:
            await update.effective_message.reply_text("No recent orders found.")  # type: ignore[union-attr]
            return
        lines = ["Recent orders (last 10):"]
        for row in rows:
            fill = row.filled_avg_price or "—"
            ts = row.created_at.strftime("%m-%d %H:%M")
            lines.append(
                f"{row.ticker} {row.side} qty={row.qty} "
                f"status={row.status} fill={fill} {ts}"
            )
        await update.effective_message.reply_text("\n".join(lines))  # type: ignore[union-attr]

    async def _cmd_signals(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return
        limit = 5
        if context.args:
            with contextlib.suppress(ValueError):
                limit = max(1, min(20, int(context.args[0])))
        with self._session_factory() as session:
            rows = (
                session.query(TradeSignalRow)
                .order_by(TradeSignalRow.created_at.desc())
                .limit(limit)
                .all()
            )
        if not rows:
            await update.effective_message.reply_text("No recent signals found.")  # type: ignore[union-attr]
            return
        lines = [f"Last {limit} signals:"]
        for row in rows:
            gate = "PASS" if row.approved else "FAIL"
            lines.append(
                f"{row.ticker} {row.direction} conv={row.conviction:.2f} "
                f"gate={gate} {row.created_at.strftime('%m-%d %H:%M')}"
            )
        await update.effective_message.reply_text("\n".join(lines))  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Redis listener — push notifications for key pipeline events
    # ------------------------------------------------------------------

    async def _redis_listener(self, event_bus: EventBus) -> None:
        """Background task: forward pipeline events to Telegram.

        Subscribed channels:
        - ``system_halted``  — drawdown halt triggered by RiskManagerAgent.
        - ``trade_executed`` — order placed by ExecutionAgent.
        """
        try:
            pubsub = await event_bus.subscribe(_HALTED_CHANNEL, _TRADE_CHANNEL)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                channel = message.get("channel", "")
                try:
                    payload = json.loads(message["data"])
                except Exception:
                    payload = {}

                if channel == _HALTED_CHANNEL:
                    drawdown = payload.get("max_drawdown_pct", "?")
                    text = (
                        f"SYSTEM HALTED — drawdown limit breached "
                        f"(drawdown={drawdown}). All positions closed."
                    )
                elif channel == _TRADE_CHANNEL:
                    ticker = payload.get("ticker", "?")
                    side = payload.get("side", "?").upper()
                    qty = payload.get("qty", "?")
                    order_id = payload.get("order_id", "?")
                    status = payload.get("status", "?")
                    text = (
                        f"Trade executed: {ticker} {side} qty={qty}\n"
                        f"Order ID: {order_id}\n"
                        f"Status: {status}"
                    )
                else:
                    continue

                await self.notify(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("TelegramBotService: redis listener error")
