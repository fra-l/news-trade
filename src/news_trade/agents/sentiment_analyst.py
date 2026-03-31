"""SentimentAnalystAgent — classifies news via an injected SentimentProvider."""

from __future__ import annotations

from typing import Any

from news_trade.agents.base import BaseAgent
from news_trade.providers.base import SentimentProvider
from news_trade.services.watchlist_manager import WatchlistManager


class SentimentAnalystAgent(BaseAgent):
    """Analyses sentiment for news events using the injected provider.

    Responsibilities:
        - Apply optional keyword pre-filter to skip irrelevant articles.
        - Delegate scoring to the injected SentimentProvider.
        - Return a flat list of SentimentResult objects.
    """

    def __init__(  # type: ignore[override]
        self,
        settings,
        event_bus,
        provider: SentimentProvider,
        watchlist_manager: WatchlistManager,
    ) -> None:
        super().__init__(settings, event_bus)
        self._provider = provider
        self._watchlist_manager = watchlist_manager

    async def run(self, state: dict) -> dict:
        """Analyse sentiment for all news events in state.

        Returns:
            ``{"sentiment_results": [SentimentResult, ...]}``
        """
        news_events = state.get("news_events") or []

        if not news_events:
            return {"sentiment_results": []}

        # Optional keyword pre-filter: skip events whose headline/summary
        # does not contain at least one watchlist ticker symbol.
        if self.settings.news_keyword_prefilter:
            watchlist_set = set(self._watchlist_manager.get_active_watchlist())
            news_events = [
                e for e in news_events
                if set(e.tickers) & watchlist_set
            ]

        if not news_events:
            self.logger.debug("All events filtered out by keyword pre-filter")
            return {"sentiment_results": []}

        estimates: dict[str, Any] | None = state.get("estimates")

        try:
            results = await self._provider.analyse_batch(
                news_events, estimates=estimates
            )
        except Exception as exc:
            self.logger.error("Sentiment analysis failed: %s", exc)
            existing = state.get("errors") or []
            return {"sentiment_results": [], "errors": [*existing, str(exc)]}

        for r in results:
            self.logger.info(
                "Sentiment: %-6s  label=%-14s  score=%+.2f  conf=%.2f  reasoning=%r",
                r.ticker,
                r.label.value,
                r.score,
                r.confidence,
                (r.reasoning or "")[:120],
            )

        self.logger.info(
            "Sentiment: scored %d events → %d results via %s",
            len(news_events),
            len(results),
            self._provider.name,
        )
        return {"sentiment_results": results}
