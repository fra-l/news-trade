"""NewsIngestorAgent — polls news APIs and filters by watchlist."""

from __future__ import annotations

from news_trade.agents.base import BaseAgent
from news_trade.models import NewsEvent


class NewsIngestorAgent(BaseAgent):
    """Ingests news from Benzinga or Polygon.io and emits NewsEvent instances.

    Responsibilities:
        - Poll the configured news provider on a timer.
        - Deduplicate articles already seen (by event_id).
        - Filter articles to only those mentioning tickers on the watchlist.
        - Classify the event type (earnings, FDA, M&A, etc.).
        - Publish each new NewsEvent to the event bus and return them in state.
    """

    async def run(self, state: dict) -> dict:
        """Fetch latest news and return new events.

        Returns:
            ``{"news_events": [NewsEvent, ...]}``
        """
        raise NotImplementedError

    async def _fetch_benzinga(self) -> list[NewsEvent]:
        """Fetch recent articles from the Benzinga News API."""
        raise NotImplementedError

    async def _fetch_polygon(self) -> list[NewsEvent]:
        """Fetch recent articles from the Polygon.io Reference News API."""
        raise NotImplementedError

    def _matches_watchlist(self, tickers: list[str]) -> bool:
        """Return True if any ticker is on the configured watchlist."""
        raise NotImplementedError

    def _is_duplicate(self, event_id: str) -> bool:
        """Check whether this event has already been ingested."""
        raise NotImplementedError
