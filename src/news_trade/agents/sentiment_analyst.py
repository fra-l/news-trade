"""SentimentAnalystAgent — classifies news via an injected SentimentProvider."""

from __future__ import annotations

from typing import Any

from news_trade.agents.base import BaseAgent
from news_trade.providers.base import SentimentProvider


class SentimentAnalystAgent(BaseAgent):
    """Analyses sentiment for news events using the injected provider.

    Responsibilities:
        - Apply optional keyword pre-filter to skip irrelevant articles.
        - Delegate scoring to the injected SentimentProvider.
        - Return a flat list of SentimentResult objects.
    """

    def __init__(self, settings, event_bus, provider: SentimentProvider) -> None:  # type: ignore[override]
        super().__init__(settings, event_bus)
        self._provider = provider

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
            watchlist_set = set(self.settings.watchlist)
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

        self.logger.info(
            "Scored %d events → %d results via %s",
            len(news_events),
            len(results),
            self._provider.name,
        )
        return {"sentiment_results": results}
