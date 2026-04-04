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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session, sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from news_trade.config import Settings
from news_trade.services.tables import OpenStage1PositionRow, OrderRow, TradeSignalRow

if TYPE_CHECKING:
    from news_trade.models import PortfolioState
    from news_trade.services.event_bus import EventBus

# Shorthand so handler signatures fit on one line.
_Ctx = ContextTypes.DEFAULT_TYPE

logger = logging.getLogger(__name__)

# Redis channels the listener subscribes to
_HALTED_CHANNEL = "system_halted"
_TRADE_CHANNEL = "trade_executed"


def _fmt_signed_money(value: float) -> str:
    """Format a signed dollar value: +$1,234.56 or -$1,234.56."""
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


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
        stop_callback: Callable[[], None] | None = None,
        get_state: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._stop_callback = stop_callback
        self._get_state = get_state
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
        self._app.add_handler(CommandHandler("stop", self._cmd_stop))
        self._app.add_handler(
            CallbackQueryHandler(self._cb_stop_confirm, pattern="^stop_confirm$")
        )
        self._app.add_handler(
            CallbackQueryHandler(self._cb_stop_cancel, pattern="^stop_cancel$")
        )

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
            "/status    — portfolio equity, P&L, open positions\n"
            "/portfolio — today's orders and Stage 1 positions\n"
            "/signals N — last N trade signals (default 5)\n"
            "/stop      — cancel all orders, close all positions, exit loop\n"
            "/help      — this message"
        )
        await update.effective_message.reply_text(text)  # type: ignore[union-attr]

    async def _cmd_status(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return

        state = self._get_state() if self._get_state is not None else None
        portfolio: PortfolioState | None = (
            state["portfolio"] if state is not None else None
        )
        system_halted: bool = (
            bool(state["system_halted"]) if state is not None else False
        )

        lines: list[str] = []

        if system_halted:
            lines.append("*** SYSTEM HALTED ***")
        lines.append(f"System: {'HALTED' if system_halted else 'running'}")
        lines.append(f"As of:  {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")

        if portfolio is None:
            lines.append("")
            lines.append("Portfolio data not yet available (waiting for first cycle).")
        else:
            prev_equity = portfolio.equity - portfolio.daily_pnl
            pnl_pct = (
                portfolio.daily_pnl / prev_equity * 100 if prev_equity != 0.0 else 0.0
            )
            pnl_pct_str = f"+{pnl_pct:.2f}%" if pnl_pct >= 0 else f"{pnl_pct:.2f}%"

            lines.append("")
            lines.append("Portfolio")
            lines.append(f"  Equity:       ${portfolio.equity:,.2f}")
            lines.append(
                f"  Daily P&L:    "
                f"{_fmt_signed_money(portfolio.daily_pnl)} ({pnl_pct_str})"
            )
            lines.append(f"  Drawdown:     {portfolio.max_drawdown_pct:.2f}%")
            lines.append(f"  Cash:         ${portfolio.cash:,.2f}")
            lines.append(f"  Buying power: ${portfolio.buying_power:,.2f}")

            lines.append("")
            if portfolio.positions:
                total_unrealized = sum(p.unrealized_pnl for p in portfolio.positions)
                lines.append(f"Positions ({len(portfolio.positions)} open):")
                for pos in portfolio.positions:
                    direction = "LONG" if pos.qty > 0 else "SHORT"
                    lines.append(
                        f"  {pos.ticker:<6} {direction:<5} {abs(pos.qty):>5}  "
                        f"P&L: {_fmt_signed_money(pos.unrealized_pnl)}"
                    )
                lines.append(
                    f"  Total unrealized: {_fmt_signed_money(total_unrealized)}"
                )
            else:
                lines.append("No open positions.")

        # Stage 1 pending — always query DB regardless of portfolio availability
        with self._session_factory() as session:
            stage1_rows = (
                session.query(OpenStage1PositionRow)
                .filter(OpenStage1PositionRow.status == "open")
                .order_by(OpenStage1PositionRow.expected_report_date.asc())
                .all()
            )

        lines.append("")
        if stage1_rows:
            lines.append(f"Stage 1 pending ({len(stage1_rows)}):")
            today = datetime.now(UTC).date()
            for row in stage1_rows:
                days = (row.expected_report_date - today).days
                days_str = f"{days} day{'s' if days != 1 else ''}"
                lines.append(
                    f"  {row.ticker:<6} {row.direction.upper():<5} "
                    f"report {row.expected_report_date}  ({days_str})"
                )
        else:
            lines.append("No pending Stage 1 positions.")

        await update.effective_message.reply_text("\n".join(lines))  # type: ignore[union-attr]

    async def _cmd_portfolio(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return

        today_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        today_str = datetime.now(UTC).strftime("%Y-%m-%d")

        with self._session_factory() as session:
            today_orders = (
                session.query(OrderRow)
                .filter(OrderRow.created_at >= today_start)
                .all()
            )
            stage1_rows = (
                session.query(OpenStage1PositionRow)
                .filter(OpenStage1PositionRow.status == "open")
                .order_by(OpenStage1PositionRow.expected_report_date.asc())
                .all()
            )
            recent_rows = (
                session.query(OrderRow)
                .order_by(OrderRow.created_at.desc())
                .limit(10)
                .all()
            )

        buys = sum(1 for r in today_orders if r.side == "buy")
        sells = sum(1 for r in today_orders if r.side == "sell")

        lines: list[str] = []

        lines.append(f"Today's activity ({today_str}):")
        if today_orders:
            n = len(today_orders)
            lines.append(
                f"  {n} order{'s' if n != 1 else ''} — "
                f"{buys} buy{'s' if buys != 1 else ''}, "
                f"{sells} sell{'s' if sells != 1 else ''}"
            )
        else:
            lines.append("  No orders today.")

        lines.append("")
        if stage1_rows:
            lines.append(f"Stage 1 positions ({len(stage1_rows)} open):")
            for s1 in stage1_rows:
                lines.append(
                    f"  {s1.ticker:<6} {s1.direction.upper():<5} "
                    f"report {s1.expected_report_date}  size {s1.size_pct:.1f}%"
                )
        else:
            lines.append("No open Stage 1 positions.")

        lines.append("")
        if not recent_rows:
            lines.append("No recent orders found.")
        else:
            lines.append("Recent orders (last 10):")
            for order in recent_rows:
                fill = (
                    f"{order.filled_avg_price:.2f}" if order.filled_avg_price else "—"
                )
                ts = order.created_at.strftime("%m-%d %H:%M")
                lines.append(
                    f"  {order.ticker} {order.side} qty={order.qty} "
                    f"status={order.status} fill={fill} {ts}"
                )

        await update.effective_message.reply_text("\n".join(lines))  # type: ignore[union-attr]

    async def _cmd_signals(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return
        limit = 5
        if context.args:
            with contextlib.suppress(ValueError):
                limit = max(1, min(20, int(context.args[0])))

        today_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        today_str = datetime.now(UTC).strftime("%Y-%m-%d")

        with self._session_factory() as session:
            today_signals = (
                session.query(TradeSignalRow)
                .filter(TradeSignalRow.created_at >= today_start)
                .all()
            )
            rows = (
                session.query(TradeSignalRow)
                .order_by(TradeSignalRow.created_at.desc())
                .limit(limit)
                .all()
            )

        lines: list[str] = []

        if today_signals:
            approved_count = sum(1 for s in today_signals if s.approved)
            rejected_count = len(today_signals) - approved_count
            lines.append(
                f"Today's signals ({today_str}): {len(today_signals)} total — "
                f"{approved_count} approved, {rejected_count} rejected"
            )
        else:
            lines.append(f"Today's signals ({today_str}): none")

        lines.append("")
        if not rows:
            lines.append("No recent signals found.")
        else:
            lines.append(f"Last {limit} signals:")
            for row in rows:
                gate = "PASS" if row.approved else "FAIL"
                lines.append(
                    f"  {row.ticker} {row.direction} conv={row.conviction:.2f} "
                    f"gate={gate} {row.created_at.strftime('%m-%d %H:%M')}"
                )

        await update.effective_message.reply_text("\n".join(lines))  # type: ignore[union-attr]

    async def _cmd_stop(self, update: Update, context: _Ctx) -> None:
        if not self._is_authorised(update):
            return
        if self._stop_callback is None:
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "Stop not available."
            )
            return
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Yes, stop & close all", callback_data="stop_confirm"
            ),
            InlineKeyboardButton("Cancel", callback_data="stop_cancel"),
        ]])
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Are you sure you want to cancel all orders, close all positions, "
            "and exit the trading loop?",
            reply_markup=keyboard,
        )

    async def _cb_stop_confirm(self, update: Update, context: _Ctx) -> None:
        query = update.callback_query
        if query is None or query.from_user is None:
            return
        if query.from_user.id != self._settings.telegram_chat_id:
            await query.answer()
            return
        await query.answer()
        await query.edit_message_text(  # type: ignore[union-attr]
            "Confirmed. Stopping trading loop and closing all positions. "
            "This may take a moment..."
        )
        self._stop_callback()  # type: ignore[misc]

    async def _cb_stop_cancel(self, update: Update, context: _Ctx) -> None:
        query = update.callback_query
        if query is None or query.from_user is None:
            return
        if query.from_user.id != self._settings.telegram_chat_id:
            await query.answer()
            return
        await query.answer()
        await query.edit_message_text(  # type: ignore[union-attr]
            "Stop cancelled. Trading loop continues."
        )

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
