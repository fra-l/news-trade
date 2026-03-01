"""RiskManagerAgent — validates signals against portfolio risk limits."""

from __future__ import annotations

from news_trade.agents.base import BaseAgent
from news_trade.models import PortfolioState, TradeSignal


class RiskManagerAgent(BaseAgent):
    """Gate-keeper that approves or rejects trade signals based on risk rules.

    Responsibilities:
        - Check per-position concentration limits.
        - Enforce max number of concurrent positions.
        - Verify drawdown is within acceptable bounds.
        - Prevent duplicate/conflicting signals for the same ticker.
        - Split signals into approved and rejected lists.
    """

    async def run(self, state: dict) -> dict:
        """Validate trade signals against current portfolio state.

        Returns:
            ``{"approved_signals": [...], "rejected_signals": [...]}``
        """
        raise NotImplementedError

    def _check_position_limit(
        self, signal: TradeSignal, portfolio: PortfolioState
    ) -> bool:
        """Return True if the signal respects per-position size limits."""
        raise NotImplementedError

    def _check_max_positions(self, portfolio: PortfolioState) -> bool:
        """Return True if there is room for another position."""
        raise NotImplementedError

    def _check_drawdown(self, portfolio: PortfolioState) -> bool:
        """Return True if the portfolio drawdown is within the hard limit."""
        raise NotImplementedError

    def _has_conflicting_position(
        self, signal: TradeSignal, portfolio: PortfolioState
    ) -> bool:
        """Return True if an existing position conflicts with this signal."""
        raise NotImplementedError
