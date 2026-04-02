"""Telegram Bot service — operator interface for the trading system.

Provides:
- Push notifications when system-level events occur (drawdown halt, errors).
- Blocking signal approval gate: each signal is sent to the operator via an
  inline-keyboard message; the pipeline waits up to ``telegram_approval_timeout_sec``
  seconds for a response before auto-proceeding.
- Operator commands: /status, /portfolio, /signals, /halt, /resume, /help.

The service is fully optional. When ``settings.telegram_bot_token`` is empty or
``settings.telegram_chat_id`` is 0 the bot is disabled and no Telegram dependency
is exercised at runtime.

Usage::

    bot = TelegramBotService(settings, session_factory)
    await bot.start(event_bus)          # call once at startup
    ...
    approved = await bot.request_approval(signal)   # called by RiskManagerAgent
    ...
    await bot.stop()                    # call in the finally block
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from news_trade.config import Settings
from news_trade.models.signals import TradeSignal
from news_trade.services.tables import OrderRow, TradeSignalRow

if TYPE_CHECKING:
    from news_trade.services.event_bus import EventBus

# Shorthand so handler signatures fit on one line.
_Ctx = ContextTypes.DEFAULT_TYPE

logger = logging.getLogger(__name__)

# Redis channel that RiskManagerAgent publishes to on drawdown halt
_HALTED_CHANNEL = "system_halted"


class TelegramBotService:
    """Async Telegram bot that bridges the trading pipeline with the operator.

    Lifecycle::

        bot = TelegramBotService(settings, session_factory)
        await bot.start(event_bus)
        # ... trading loop ...
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
        # signal_id → Future that resolves to True (proceed) or False (blocked)
        self._pending: dict[str, asyncio.Future[bool]] = {}
        # Set True by /halt command; checked by main.py before each cycle
        self.operator_halt: bool = False
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
        self._app.add_handler(CommandHandler("halt", self._cmd_halt))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CallbackQueryHandler(self._on_callback))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]

        self._redis_task = asyncio.create_task(
            self._redis_listener(event_bus), name="telegram_redis_listener"
        )

        logger.info(
            "TelegramBotService started (chat_id=%d, signal_approval=%s)",
            self._settings.telegram_chat_id,
            self._settings.telegram_signal_approval,
        )
        await self.notify("Trading system started. Send /help for available commands.")

    async def stop(self) -> None:
        """Shut down the bot gracefully."""
        if self._redis_task is not None:
            self._redis_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._redis_task

        # Resolve any pending approval futures so blocked coroutines can exit
        for future in self._pending.values():
            if not future.done():
                future.set_result(True)  # auto-approve on shutdown
        self._pending.clear()

        if self._app is not None:
            try:
                await self._app.updater.stop()  # type: ignore[union-attr]
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                logger.exception("TelegramBotService: error during shutdown")
        logger.info("TelegramBotService stopped")

    # ------------------------------------------------------------------
    # Public API used by RiskManagerAgent
    # ------------------------------------------------------------------

    async def request_approval(self, signal: TradeSignal) -> bool:
        """Send the signal to Telegram and wait for operator approval.

        Returns:
            True  — operator pressed Approve, or timeout elapsed (auto-proceed).
            False — operator pressed Block.

        If the bot is not running (disabled or not started) always returns True.
        """
        if self._app is None:
            return True

        direction = signal.direction.value if signal.direction else "?"
        text = (
            f"Signal approval required\n"
            f"Ticker:     {signal.ticker}\n"
            f"Direction:  {direction}\n"
            f"Qty:        {signal.suggested_qty}\n"
            f"Confidence: {signal.confidence_score:.2f}\n"
            f"ID:         {signal.signal_id}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "Approve", callback_data=f"approve:{signal.signal_id}"
                ),
                InlineKeyboardButton(
                    "Block", callback_data=f"block:{signal.signal_id}"
                ),
            ]
        ])

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[signal.signal_id] = future

        try:
            await self._app.bot.send_message(
                chat_id=self._settings.telegram_chat_id,
                text=text,
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("TelegramBotService: failed to send approval request")
            self._pending.pop(signal.signal_id, None)
            return True  # fail-open: proceed if Telegram is unreachable

        timeout = self._settings.telegram_approval_timeout_sec
        try:
            result: bool = await asyncio.wait_for(
                asyncio.shield(future), timeout=float(timeout)
            )
            return result
        except TimeoutError:
            logger.info(
                "Telegram approval timeout for signal %s — auto-proceeding",
                signal.signal_id,
            )
            return True
        finally:
            self._pending.pop(signal.signal_id, None)

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
    # Command handlers
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
            "/status    — system state and last cycle info\n"
            "/portfolio — recent orders and open positions\n"
            "/signals N — last N trade signals (default 5)\n"
            "/halt      — pause trading loop\n"
            "/resume    — resume trading loop\n"
            "/help      — this message"
        )
        await update.effective_message.reply_text(text)  # type: ignore[union-attr]

    async def _cmd_status(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return
        halt_status = "HALTED by operator" if self.operator_halt else "running"
        approval_state = "on" if self._settings.telegram_signal_approval else "off"
        text = (
            f"System status: {halt_status}\n"
            f"Signal approval gate: {approval_state}\n"
            f"Approval timeout: {self._settings.telegram_approval_timeout_sec}s\n"
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

    async def _cmd_halt(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return
        self.operator_halt = True
        logger.warning("TelegramBotService: operator halt requested via Telegram")
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Trading loop HALTED. Send /resume to restart."
        )

    async def _cmd_resume(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return
        self.operator_halt = False
        logger.info("TelegramBotService: operator resumed trading via Telegram")
        await update.effective_message.reply_text("Trading loop RESUMED.")  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Inline keyboard callback
    # ------------------------------------------------------------------

    async def _on_callback(self, update: Update, context: _Ctx) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._is_authorised(update):
            await query.answer()
            return

        await query.answer()
        data: str = query.data or ""

        if data.startswith("approve:"):
            signal_id = data[len("approve:"):]
            approved = True
            label = "Approved"
        elif data.startswith("block:"):
            signal_id = data[len("block:"):]
            approved = False
            label = "Blocked"
        else:
            return

        future = self._pending.get(signal_id)
        if future is not None and not future.done():
            future.set_result(approved)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.edit_message_text(
                text=f"{query.message.text}\n\n--- {label} by operator ---"  # type: ignore[union-attr]
            )
        except Exception:
            pass  # message may already be gone

    # ------------------------------------------------------------------
    # Redis listener — push drawdown-halt notifications
    # ------------------------------------------------------------------

    async def _redis_listener(self, event_bus: EventBus) -> None:
        """Background task: forward system_halted events to Telegram."""
        try:
            pubsub = await event_bus.subscribe(_HALTED_CHANNEL)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                    drawdown = payload.get("max_drawdown_pct", "?")
                    text = (
                        f"SYSTEM HALTED — drawdown limit breached "
                        f"(drawdown={drawdown}). "
                        f"All positions closed. Send /resume after reviewing."
                    )
                except Exception:
                    text = "SYSTEM HALTED — drawdown limit breached."
                await self.notify(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("TelegramBotService: redis listener error")
