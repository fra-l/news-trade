"""Provider Protocol definitions.

All external data sources are typed as Protocols so agents depend on an
interface, never a concrete implementation.  Any class with the right
methods satisfies a Protocol — no inheritance required.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from news_trade.models.calendar import EarningsCalendarEntry
from news_trade.models.events import NewsEvent
from news_trade.models.market import MarketSnapshot
from news_trade.models.sentiment import SentimentResult
from news_trade.models.surprise import EstimatesData


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
        self,
        events: list[NewsEvent],
        estimates: dict[str, EstimatesData] | None = None,
    ) -> list[SentimentResult]:
        """Score multiple events (may batch into fewer API calls).

        Args:
            events: News events to score.
            estimates: Optional mapping of ticker → pre-announcement consensus
                       estimates. When provided and the event is EARN_PRE,
                       providers may inject the estimates narrative into the
                       prompt for richer context.
        """
        ...


@runtime_checkable
class CalendarProvider(Protocol):
    """Fetches upcoming earnings calendar entries."""

    @property
    def name(self) -> str:
        """Human-readable provider name for logging (e.g. 'fmp_calendar')."""
        ...

    async def get_upcoming_earnings(
        self,
        tickers: list[str],
        from_date: date,
        to_date: date,
    ) -> list[EarningsCalendarEntry]:
        """Return scheduled earnings for the given tickers within the date window.

        Args:
            tickers: Watchlist symbols to filter by.
            from_date: Start of scan window (inclusive).
            to_date: End of scan window (inclusive).
        """
        ...


@runtime_checkable
class EstimatesProvider(Protocol):
    """Fetches historical EPS beat rates for use in EARN_PRE sizing."""

    @property
    def name(self) -> str:
        """Human-readable provider name for logging (e.g. 'fmp_estimates')."""
        ...

    async def get_historical_beat_rate(
        self, ticker: str, lookback: int = 8
    ) -> float | None:
        """Return the fraction of recent quarters where EPS beat consensus.

        Args:
            ticker: Stock symbol to look up.
            lookback: Number of past quarters to include. Defaults to 8.

        Returns:
            Beat rate in ``[0.0, 1.0]``, or ``None`` when the data is
            unavailable or insufficient.
        """
        ...
