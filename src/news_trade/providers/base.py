"""Provider Protocol definitions.

All external data sources are typed as Protocols so agents depend on an
interface, never a concrete implementation.  Any class with the right
methods satisfies a Protocol — no inheritance required.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from news_trade.models.events import NewsEvent
from news_trade.models.market import MarketSnapshot
from news_trade.models.sentiment import SentimentResult


@runtime_checkable
class NewsProvider(Protocol):
    """Fetches news articles and returns typed NewsEvent objects."""

    @property
    def name(self) -> str:
        """Human-readable provider name for logging (e.g. 'benzinga', 'rss')."""
        ...

    async def fetch(
        self,
        tickers: list[str],
        since: datetime | None = None,
    ) -> list[NewsEvent]:
        """Return recent news for the given tickers.

        Args:
            tickers: Watchlist symbols to filter by.
            since: Only return articles published after this timestamp.
                   None means use provider-specific default lookback.
        """
        ...


@runtime_checkable
class MarketDataProvider(Protocol):
    """Fetches price bars and computes derived market context."""

    @property
    def name(self) -> str:
        """Human-readable provider name for logging."""
        ...

    async def get_snapshot(self, ticker: str) -> MarketSnapshot:
        """Fetch bars and compute volatility/ATR for a single ticker."""
        ...

    async def get_snapshots(self, tickers: list[str]) -> dict[str, MarketSnapshot]:
        """Batch fetch. Default implementation loops get_snapshot()."""
        ...


@runtime_checkable
class SentimentProvider(Protocol):
    """Analyses news sentiment and returns a typed result."""

    @property
    def name(self) -> str:
        """Human-readable provider name for logging."""
        ...

    async def analyse(self, event: NewsEvent) -> SentimentResult:
        """Score a single news event."""
        ...

    async def analyse_batch(
        self, events: list[NewsEvent]
    ) -> list[SentimentResult]:
        """Score multiple events (may batch into fewer API calls)."""
        ...
