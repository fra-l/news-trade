"""NewsIngestorAgent — fetches news via an injected NewsProvider."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from news_trade.agents.base import BaseAgent
from news_trade.models import NewsEvent
from news_trade.models.events import EventType
from news_trade.providers.base import NewsProvider
from news_trade.services.database import build_engine
from news_trade.services.tables import Base, NewsEventRow
from news_trade.services.watchlist_manager import WatchlistManager

# Ordered from most-specific to least-specific to avoid false matches
_KEYWORD_MAP: list[tuple[frozenset[str], EventType]] = [
    (frozenset(["fda", "approval", "drug", "clinical", "trial"]), EventType.FDA_APPROVAL),
    (
        frozenset(["merger", "acquisition", "acquires", "takeover", "buyout"]),
        EventType.MERGER_ACQUISITION,
    ),
    (frozenset(["sec", "10-k", "10-q", "8-k"]), EventType.SEC_FILING),
    (
        frozenset(["analyst", "upgrade", "downgrade", "price target", "overweight", "underweight"]),
        EventType.ANALYST_RATING,
    ),
    (
        frozenset(["guidance", "outlook", "forecast", "raises guidance", "lowers guidance"]),
        EventType.GUIDANCE,
    ),
    (
        frozenset(["earnings", "eps", "revenue", "quarterly results", "net income"]),
        EventType.EARNINGS,
    ),
    (frozenset(["fed", "rate hike", "inflation", "gdp", "unemployment", "macro"]), EventType.MACRO),
]


class NewsIngestorAgent(BaseAgent):
    """Ingests news via the injected NewsProvider and emits NewsEvent instances.

    Responsibilities:
        - Delegate fetching to the injected provider.
        - Deduplicate articles already seen (by event_id).
        - Filter articles to only those mentioning tickers on the watchlist.
        - Publish each new NewsEvent to the event bus and return them in state.
    """

    def __init__(  # type: ignore[override]
        self,
        settings,
        event_bus,
        provider: NewsProvider,
        watchlist_manager: WatchlistManager,
    ) -> None:
        super().__init__(settings, event_bus)
        self._provider = provider
        self._watchlist_manager = watchlist_manager
        self._engine = build_engine(settings)
        Base.metadata.create_all(self._engine)

    async def run(self, state: dict) -> dict:  # type: ignore[override]
        """Fetch latest news and return new events.

        Returns:
            ``{"news_events": [NewsEvent, ...]}``
        """
        try:
            candidates = await self._provider.fetch(
                tickers=self._watchlist_manager.get_active_watchlist(),
                since=state.get("last_poll"),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("News fetch failed: %s", exc)
            existing = state.get("errors") or []
            return {"news_events": [], "errors": [*existing, str(exc)]}

        new_events: list[NewsEvent] = []
        seen_in_batch: set[str] = set()

        with Session(self._engine) as session:
            for event in candidates:
                if not self._matches_watchlist(event.tickers):
                    continue
                if event.event_id in seen_in_batch:
                    continue
                if self._is_duplicate(event.event_id, session):
                    continue
                seen_in_batch.add(event.event_id)
                self._persist(event, session)
                new_events.append(event)
            session.commit()

        for event in new_events:
            try:
                await self.event_bus.publish("news_events", event)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "Failed to publish event %s: %s", event.event_id, exc
                )

        self.logger.info("Ingested %d new events", len(new_events))
        return {"news_events": new_events}

    def _matches_watchlist(self, tickers: list[str]) -> bool:
        """Return True if any ticker is on the configured watchlist."""
        watchlist = set(self._watchlist_manager.get_active_watchlist())
        return bool(set(tickers) & watchlist)

    def _is_duplicate(self, event_id: str, session: Session) -> bool:
        """Check whether this event has already been ingested."""
        return (
            session.execute(
                select(NewsEventRow.id)
                .where(NewsEventRow.event_id == event_id)
                .limit(1)
            ).first()
            is not None
        )

    def _persist(self, event: NewsEvent, session: Session) -> None:
        """Insert a NewsEvent row into the database (does not commit)."""
        row = NewsEventRow(
            event_id=event.event_id,
            headline=event.headline,
            summary=event.summary,
            source=event.source,
            url=event.url,
            event_type=event.event_type,
            published_at=event.published_at,
        )
        row.tickers = event.tickers
        session.add(row)


# ---------------------------------------------------------------------------
# Module-level helpers (kept for backwards compatibility with tests)
# ---------------------------------------------------------------------------


def _classify_event_type(headline: str) -> EventType:
    """Classify a news headline into an EventType via keyword matching."""
    lower = headline.lower()
    for keywords, event_type in _KEYWORD_MAP:
        if any(kw in lower for kw in keywords):
            return event_type
    return EventType.OTHER


def _parse_dt(value: str) -> "datetime":  # noqa: F821  (forward ref for compat)
    """Parse an RFC 2822 or ISO 8601 datetime string, falling back to utcnow."""
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    if not value:
        return datetime.now(timezone.utc)
    try:
        return parsedate_to_datetime(value)
    except Exception:  # noqa: BLE001
        pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)
