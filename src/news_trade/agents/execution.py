"""ExecutionAgent — places and manages orders via Alpaca paper trading."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import TimeInForce
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.requests import MarketOrderRequest
from pydantic import BaseModel
from sqlalchemy.orm import Session

from news_trade.agents.base import BaseAgent
from news_trade.config import Settings
from news_trade.models.orders import Order, OrderSide, OrderStatus
from news_trade.models.portfolio import PortfolioState
from news_trade.models.signals import SignalDirection, TradeSignal
from news_trade.services.event_bus import EventBus
from news_trade.services.tables import OrderRow


class _TradeExecutedEvent(BaseModel):
    """Published to Redis when ExecutionAgent places an order."""

    event: str = "TRADE_EXECUTED"
    ticker: str
    side: str
    qty: int
    order_id: str
    status: str


class ExecutionAgent(BaseAgent):
    """Translates approved signals into live orders on Alpaca paper trading.

    Responsibilities:
        - Convert each approved TradeSignal into an Alpaca order request.
        - Submit orders via the Alpaca Trading API.
        - Track order status (filled, partial, rejected).
        - Log every order to the database for audit.
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        alpaca_client: TradingClient | None = None,
        session: Session | None = None,
    ) -> None:
        super().__init__(settings, event_bus)
        self._alpaca = alpaca_client
        self._session = session

    async def run(self, state: dict) -> dict:  # type: ignore[type-arg]
        """Execute approved trade signals.

        Returns:
            ``{"orders": [Order, ...], "errors": [...]}``
        """
        approved_signals: list[TradeSignal] = state.get("approved_signals", [])
        portfolio: PortfolioState | None = state.get("portfolio")
        orders: list[Order] = []
        errors: list[str] = list(state.get("errors", []))

        for signal in approved_signals:
            try:
                order = await self._submit_order(signal, portfolio)
                close_after = (
                    date.today() + timedelta(days=signal.horizon_days)
                    if signal.horizon_days
                    else None
                )
                self._log_order(order, close_after_date=close_after)
                try:
                    await self.event_bus.publish(
                        "trade_executed",
                        _TradeExecutedEvent(
                            ticker=order.ticker,
                            side=order.side.value,
                            qty=order.qty,
                            order_id=order.order_id,
                            status=order.status.value,
                        ),
                    )
                except Exception:
                    self.logger.warning(
                        "trade_executed publish failed for order %s", order.order_id
                    )
                orders.append(order)
            except Exception as exc:
                self.logger.error(
                    "Order submission failed for signal %s (%s): %s",
                    signal.signal_id,
                    signal.ticker,
                    exc,
                )
                errors.append(f"execution:{signal.signal_id}:{exc}")

        return {"orders": orders, "errors": errors}

    async def _submit_order(
        self, signal: TradeSignal, portfolio: PortfolioState | None
    ) -> Order:
        """Submit a single order to Alpaca and return the resulting Order model."""
        side = _signal_to_order_side(signal, portfolio)
        order_id = str(uuid.uuid4())

        if self._alpaca is None:
            # No broker client injected — return a PENDING stub (dry-run / test)
            return Order(
                order_id=order_id,
                signal_id=signal.signal_id,
                ticker=signal.ticker,
                side=side,
                qty=signal.suggested_qty,
                status=OrderStatus.PENDING,
            )

        request = MarketOrderRequest(
            symbol=signal.ticker,
            qty=signal.suggested_qty,
            side=AlpacaOrderSide(side.value),
            time_in_force=TimeInForce.DAY,
        )
        # alpaca-py stubs submit_order as Order | dict; annotation is conservative
        alpaca_order: AlpacaOrder = await asyncio.to_thread(
            self._alpaca.submit_order,  # type: ignore[arg-type]
            request,
        )
        return _alpaca_to_order(alpaca_order, order_id, signal.signal_id)

    async def _sync_order_status(self, order: Order) -> Order:
        """Poll Alpaca for the latest status of a submitted order."""
        if self._alpaca is None or order.broker_order_id is None:
            return order
        # alpaca-py stubs get_order_by_id as Order | dict; annotation is conservative
        alpaca_order: AlpacaOrder = await asyncio.to_thread(
            self._alpaca.get_order_by_id,  # type: ignore[arg-type]
            order.broker_order_id,
        )
        return order.model_copy(
            update={
                "status": OrderStatus(str(alpaca_order.status.value)),
                "filled_qty": int(alpaca_order.filled_qty or 0),
                "filled_avg_price": (
                    float(alpaca_order.filled_avg_price)
                    if alpaca_order.filled_avg_price
                    else None
                ),
                "filled_at": alpaca_order.filled_at,
            }
        )

    async def _cancel_order(self, order: Order) -> Order:
        """Cancel an open order on Alpaca."""
        if self._alpaca is None or order.broker_order_id is None:
            return order.model_copy(update={"status": OrderStatus.CANCELLED})
        await asyncio.to_thread(
            self._alpaca.cancel_order_by_id, order.broker_order_id
        )
        return order.model_copy(update={"status": OrderStatus.CANCELLED})

    def _log_order(
        self, order: Order, close_after_date: date | None = None
    ) -> None:
        """Persist the order to the SQLite database."""
        if self._session is None:
            return
        existing = (
            self._session.query(OrderRow).filter_by(order_id=order.order_id).first()
        )
        if existing is None:
            row = OrderRow(
                order_id=order.order_id,
                broker_order_id=order.broker_order_id,
                signal_id=order.signal_id,
                ticker=order.ticker,
                side=order.side.value,
                order_type=order.order_type.value,
                qty=order.qty,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                status=order.status.value,
                filled_qty=order.filled_qty,
                filled_avg_price=order.filled_avg_price,
                submitted_at=order.submitted_at,
                filled_at=order.filled_at,
                close_after_date=close_after_date,
            )
            self._session.add(row)
        else:
            existing.status = order.status.value
            existing.filled_qty = order.filled_qty
            existing.filled_avg_price = order.filled_avg_price
            existing.filled_at = order.filled_at
            existing.broker_order_id = order.broker_order_id
            # close_after_date is intentionally left unchanged on upsert
        self._session.commit()

    async def scan_expired_pead(self, state: dict) -> dict:  # type: ignore[type-arg]
        """Daily cron: close Stage 2 PEAD positions past their horizon_days.

        Queries OrderRow for filled orders whose ``close_after_date`` has passed,
        then calls ``TradingClient.close_position()`` on each ticker via
        ``asyncio.to_thread`` to keep the event loop unblocked.

        Returns:
            ``{"errors": [...]}`` — empty list on full success.
        """
        if self._session is None or self._alpaca is None:
            return {"errors": []}

        today = date.today()
        expired: list[OrderRow] = (
            self._session.query(OrderRow)
            .filter(
                OrderRow.close_after_date.isnot(None),
                OrderRow.close_after_date <= today,
                OrderRow.status.in_(["filled", "submitted", "partially_filled"]),
            )
            .all()
        )

        errors: list[str] = []
        for row in expired:
            try:
                await asyncio.to_thread(
                    self._alpaca.close_position,
                    row.ticker,
                )
                row.status = "pead_closed"
                self._session.commit()
                self.logger.info(
                    "PEAD horizon expired: closed %s (order_id=%s, was due %s)",
                    row.ticker,
                    row.order_id,
                    row.close_after_date,
                )
            except Exception as exc:
                self.logger.error(
                    "PEAD close failed for %s (order_id=%s): %s",
                    row.ticker,
                    row.order_id,
                    exc,
                )
                errors.append(f"pead_close:{row.order_id}:{exc}")

        return {"errors": errors}


def _signal_to_order_side(
    signal: TradeSignal, portfolio: PortfolioState | None
) -> OrderSide:
    """Map signal direction to order side; CLOSE uses existing position sign."""
    if signal.direction == SignalDirection.LONG:
        return OrderSide.BUY
    if signal.direction == SignalDirection.SHORT:
        return OrderSide.SELL
    # CLOSE: inspect existing position to determine correct side
    if portfolio is not None:
        pos = portfolio.get_position(signal.ticker)
        if pos is not None and pos.qty < 0:
            return OrderSide.BUY  # short position → buy to close
    return OrderSide.SELL  # long position (or unknown) → sell to close


def _alpaca_to_order(
    alpaca_order: AlpacaOrder,
    order_id: str,
    signal_id: str,
) -> Order:
    """Map alpaca-py Order model → internal Order model."""
    # Alpaca has more statuses than our enum (e.g. "accepted", "new").
    # Map accepted/new intermediary states to SUBMITTED; fall back for others.
    raw_status = str(alpaca_order.status.value) if alpaca_order.status else "submitted"
    try:
        internal_status = OrderStatus(raw_status)
    except ValueError:
        internal_status = OrderStatus.SUBMITTED
    return Order(
        order_id=order_id,
        broker_order_id=str(alpaca_order.id),
        signal_id=signal_id,
        ticker=alpaca_order.symbol,
        side=OrderSide(alpaca_order.side.value if alpaca_order.side else "buy"),
        qty=int(alpaca_order.qty or 0),
        status=internal_status,
        filled_qty=int(alpaca_order.filled_qty or 0),
        filled_avg_price=(
            float(alpaca_order.filled_avg_price)
            if alpaca_order.filled_avg_price
            else None
        ),
        submitted_at=alpaca_order.submitted_at,
        filled_at=alpaca_order.filled_at,
    )
