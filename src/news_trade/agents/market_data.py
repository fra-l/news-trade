"""MarketDataAgent.

Fetches OHLCV bars and volatility via an injected MarketDataProvider.
"""

from __future__ import annotations

from news_trade.agents.base import BaseAgent
from news_trade.providers.base import MarketDataProvider


class MarketDataAgent(BaseAgent):
    """Fetches market data for tickers mentioned in news events.

    Responsibilities:
        - Collect unique tickers from news events in state.
        - Delegate bar-fetching and metric computation to the injected provider.
        - Return a market context dict keyed by ticker.
    """

    def __init__(self, settings, event_bus, provider: MarketDataProvider) -> None:  # type: ignore[override]
        super().__init__(settings, event_bus)
        self._provider = provider

    async def run(self, state: dict) -> dict:
        """Fetch market data for tickers found in ``state["news_events"]``.

        Returns:
            ``{"market_context": {"AAPL": MarketSnapshot, ...}}``
        """
        news_events = state.get("news_events") or []
        tickers: list[str] = list(
            {ticker for event in news_events for ticker in event.tickers}
        )

        if not tickers:
            self.logger.debug("No tickers to fetch market data for")
            return {"market_context": {}}

        self.logger.info(
            "MarketData: fetching data for tickers=%s via %s",
            tickers,
            self._provider.name,
        )

        try:
            snapshots = await self._provider.get_snapshots(tickers)
        except Exception as exc:
            self.logger.error("Market data fetch failed: %s", exc)
            existing = state.get("errors") or []
            return {"market_context": {}, "errors": [*existing, str(exc)]}

        for ticker, snap in snapshots.items():
            self.logger.info(
                "MarketData: %-6s  close=%.2f  vol_20d=%.4f  atr_14d=%s",
                ticker,
                snap.latest_close,
                snap.volatility_20d,
                f"{snap.atr_14d:.4f}" if snap.atr_14d is not None else "n/a",
            )

        self.logger.info(
            "MarketData: fetched %d/%d tickers via %s",
            len(snapshots),
            len(tickers),
            self._provider.name,
        )
        return {"market_context": snapshots}
