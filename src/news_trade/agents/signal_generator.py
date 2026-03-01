"""SignalGeneratorAgent — combines sentiment and market data into signals."""

from __future__ import annotations

from news_trade.agents.base import BaseAgent
from news_trade.models import SentimentResult, TradeSignal


class SignalGeneratorAgent(BaseAgent):
    """Generates actionable trade signals from sentiment + market context.

    Responsibilities:
        - Pair each SentimentResult with the corresponding market context.
        - Apply conviction thresholds and directional logic.
        - Compute suggested position size, stop-loss, and take-profit.
        - Emit TradeSignal instances for downstream risk validation.
    """

    async def run(self, state: dict) -> dict:
        """Generate trade signals from sentiment results and market context.

        Returns:
            ``{"trade_signals": [TradeSignal, ...]}``
        """
        raise NotImplementedError

    def _build_signal(
        self,
        sentiment: SentimentResult,
        market_ctx: dict,
    ) -> TradeSignal | None:
        """Create a TradeSignal from a sentiment result and market snapshot.

        Returns None if conviction is below the configured threshold.
        """
        raise NotImplementedError

    def _compute_position_size(
        self, ticker: str, conviction: float, volatility: float
    ) -> int:
        """Determine the number of shares to trade.

        Uses a volatility-adjusted sizing model scaled by conviction.
        """
        raise NotImplementedError

    def _compute_stop_loss(
        self, entry_price: float, volatility: float, direction: str
    ) -> float:
        """Calculate a volatility-based stop-loss level."""
        raise NotImplementedError
