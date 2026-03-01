"""ExecutionAgent — places and manages orders via Alpaca paper trading."""

from __future__ import annotations

from news_trade.agents.base import BaseAgent
from news_trade.models import Order, TradeSignal


class ExecutionAgent(BaseAgent):
    """Translates approved signals into live orders on Alpaca paper trading.

    Responsibilities:
        - Convert each approved TradeSignal into an Alpaca order request.
        - Submit orders via the Alpaca Trading API.
        - Track order status (filled, partial, rejected).
        - Log every order to the database for audit.
    """

    async def run(self, state: dict) -> dict:
        """Execute approved trade signals.

        Returns:
            ``{"orders": [Order, ...]}``
        """
        raise NotImplementedError

    async def _submit_order(self, signal: TradeSignal) -> Order:
        """Submit a single order to Alpaca and return the resulting Order model."""
        raise NotImplementedError

    async def _sync_order_status(self, order: Order) -> Order:
        """Poll Alpaca for the latest status of a submitted order."""
        raise NotImplementedError

    async def _cancel_order(self, order: Order) -> Order:
        """Cancel an open order on Alpaca."""
        raise NotImplementedError

    def _log_order(self, order: Order) -> None:
        """Persist the order to the SQLite database."""
        raise NotImplementedError
