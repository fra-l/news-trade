"""NewsIngestorAgent — polls news APIs and filters by watchlist."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from news_trade.agents.base import BaseAgent
from news_trade.models import NewsEvent
from news_trade.models.events import EventType
from news_trade.services.database import build_engine
from news_trade.services.tables import Base, NewsEventRow

_BENZINGA_NEWS_URL = "https://api.benzinga.com/api/v2/news"
_POLYGON_NEWS_URL = "https://api.polygon.io/v2/reference/news"

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
    """Ingests news from Benzinga or Polygon.io and emits NewsEvent instances.

    Responsibilities:
        - Poll the configured news provider on a timer.
        - Deduplicate articles already seen (by event_id).
        - Filter articles to only those mentioning tickers on the watchlist.
        - Classify the event type (earnings, FDA, M&A, etc.).
        - Publish each new NewsEvent to the event bus and return them in state.
    """

    def __init__(self, settings, event_bus) -> None:  # type: ignore[override]
        super().__init__(settings, event_bus)
        self._engine = build_engine(settings)
        Base.metadata.create_all(self._engine)

    async def run(self, state: dict) -> dict:  # type: ignore[override]
        """Fetch latest news and return new events.

        Returns:
            ``{"news_events": [NewsEvent, ...]}``
        """
        provider = self.settings.news_provider.lower()
        try:
            if provider == "polygon":
                candidates = await self._fetch_polygon()
            else:
                if provider != "benzinga":
                    self.logger.warning(
                        "Unknown news_provider %r — falling back to benzinga", provider
                    )
                candidates = await self._fetch_benzinga()
        except httpx.HTTPError as exc:
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

    async def _fetch_benzinga(self) -> list[NewsEvent]:
        """Fetch recent articles from the Benzinga News API."""
        params: dict[str, str | int] = {
            "token": self.settings.benzinga_api_key,
            "pageSize": 50,
            "displayOutput": "full",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_BENZINGA_NEWS_URL, params=params)
            resp.raise_for_status()
            articles: list[dict] = resp.json()

        events = []
        for article in articles:
            tickers = [s["name"] for s in (article.get("stocks") or [])]
            events.append(
                NewsEvent(
                    event_id=str(article["id"]),
                    headline=article.get("title", ""),
                    summary=article.get("teaser", ""),
                    source="benzinga",
                    url=article.get("url", ""),
                    tickers=tickers,
                    event_type=_classify_event_type(article.get("title", "")),
                    published_at=_parse_dt(article.get("created", "")),
                )
            )
        return events

    async def _fetch_polygon(self) -> list[NewsEvent]:
        """Fetch recent articles from the Polygon.io Reference News API."""
        params: dict[str, str | int] = {
            "apiKey": self.settings.polygon_api_key,
            "limit": 50,
            "order": "desc",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_POLYGON_NEWS_URL, params=params)
            resp.raise_for_status()
            data: dict = resp.json()

        events = []
        for article in data.get("results", []):
            events.append(
                NewsEvent(
                    event_id=article["id"],
                    headline=article.get("title", ""),
                    summary=article.get("description", ""),
                    source="polygon",
                    url=article.get("article_url", ""),
                    tickers=article.get("tickers", []),
                    event_type=_classify_event_type(article.get("title", "")),
                    published_at=_parse_dt(article.get("published_utc", "")),
                )
            )
        return events

    def _matches_watchlist(self, tickers: list[str]) -> bool:
        """Return True if any ticker is on the configured watchlist."""
        watchlist = set(self.settings.watchlist)
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
# Module-level helpers
# ---------------------------------------------------------------------------


def _classify_event_type(headline: str) -> EventType:
    """Classify a news headline into an EventType via keyword matching."""
    lower = headline.lower()
    for keywords, event_type in _KEYWORD_MAP:
        if any(kw in lower for kw in keywords):
            return event_type
    return EventType.OTHER


def _parse_dt(value: str) -> datetime:
    """Parse an RFC 2822 or ISO 8601 datetime string, falling back to utcnow."""
    if not value:
        return datetime.now(timezone.utc)
    # RFC 2822 (Benzinga): "Mon, 02 Jan 2006 15:04:05 +0000"
    try:
        return parsedate_to_datetime(value)
    except Exception:  # noqa: BLE001
        pass
    # ISO 8601 (Polygon): "2006-01-02T15:04:05Z" — supported natively in Python 3.11+
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)
