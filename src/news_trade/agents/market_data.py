"""MarketDataAgent — fetches OHLCV bars and volatility for tickers."""

from __future__ import annotations

from news_trade.agents.base import BaseAgent
from news_trade.models import MarketSnapshot


class MarketDataAgent(BaseAgent):
    """Fetches market data from Alpaca for tickers mentioned in news events.

    Responsibilities:
        - Retrieve recent OHLCV bars for each ticker.
        - Compute short-term realized volatility.
        - Return a market context dict keyed by ticker.
    """

    async def run(self, state: dict) -> dict:
        """Fetch market data for tickers found in ``state["news_events"]``.

        Returns:
            ``{"market_context": {"AAPL": {...}, ...}}``
        """
        raise NotImplementedError

    async def _get_bars(self, ticker: str, limit: int = 30) -> list[dict]:
        """Retrieve recent OHLCV bars from Alpaca Market Data API.

        Args:
            ticker: Stock symbol.
            limit: Number of bars to fetch.
        """
        raise NotImplementedError

    def _compute_volatility(self, bars: list[dict]) -> float:
        """Calculate annualized realized volatility from daily close prices."""
        raise NotImplementedError

    def _build_context(self, ticker: str, bars: list[dict]) -> MarketSnapshot:
        """Build a market context snapshot for a single ticker."""
        raise NotImplementedError
