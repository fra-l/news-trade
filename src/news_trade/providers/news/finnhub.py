"""FinnhubNewsProvider — fetches company news from the Finnhub API.

Free tier: 60 req/min.  Requires FINNHUB_API_KEY.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from news_trade.models.events import EventType, NewsEvent

_logger = logging.getLogger(__name__)

_BASE_URL = "https://finnhub.io/api/v1"

_KEYWORD_MAP: list[tuple[frozenset[str], EventType]] = [
    (
        frozenset(["fda", "approval", "drug", "clinical", "trial"]),
        EventType.FDA_APPROVAL,
    ),
    (
        frozenset(["merger", "acquisition", "acquires", "takeover", "buyout"]),
        EventType.MERGER_ACQUISITION,
    ),
    (frozenset(["sec", "10-k", "10-q", "8-k"]), EventType.SEC_FILING),
    (
        frozenset(
            [
                "analyst", "upgrade", "downgrade",
                "price target", "overweight", "underweight",
            ]
        ),
        EventType.ANALYST_RATING,
    ),
    (
        frozenset(
            ["guidance", "outlook", "forecast", "raises guidance", "lowers guidance"]
        ),
        EventType.GUIDANCE,
    ),
    (
        frozenset(["earnings", "eps", "revenue", "quarterly results", "net income"]),
        EventType.EARNINGS,
    ),
    (
        frozenset(["fed", "rate hike", "inflation", "gdp", "unemployment", "macro"]),
        EventType.MACRO,
    ),
]


def _classify(headline: str) -> EventType:
    lower = headline.lower()
    for keywords, event_type in _KEYWORD_MAP:
        if any(kw in lower for kw in keywords):
            return event_type
    return EventType.OTHER


class FinnhubNewsProvider:
    """Fetches company news from the Finnhub /company-news endpoint.

    One request per ticker per poll cycle.  The ``since`` parameter sets the
    ``from`` date; when not given it defaults to 3 days back so that each
    30-second poll cycle only picks up recent articles.
    """

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("FinnhubNewsProvider requires a non-empty api_key")
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "finnhub"

    async def fetch(
        self,
        tickers: list[str],
        since: datetime | None = None,
    ) -> list[NewsEvent]:
        """Fetch company news for each ticker from Finnhub."""
        now = datetime.now(UTC)
        from_date = (since.date() if since else (now - timedelta(days=3)).date())
        to_date = now.date()

        events: list[NewsEvent] = []
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(timeout=15.0) as client:
            for ticker in tickers:
                try:
                    resp = await client.get(
                        f"{_BASE_URL}/company-news",
                        params={
                            "symbol": ticker,
                            "from": str(from_date),
                            "to": str(to_date),
                            "token": self._api_key,
                        },
                    )
                    resp.raise_for_status()
                    articles = resp.json()
                    if not isinstance(articles, list):
                        continue
                    for article in articles:
                        event = _article_to_event(article, ticker)
                        if event is not None and event.event_id not in seen_ids:
                            seen_ids.add(event.event_id)
                            events.append(event)
                except httpx.HTTPError as exc:
                    _logger.warning("Finnhub news fetch failed for %s: %s", ticker, exc)

        return events


def _article_to_event(article: dict, ticker: str) -> NewsEvent | None:
    """Convert a Finnhub company-news dict to a NewsEvent."""
    headline = (article.get("headline") or "").strip()
    if not headline:
        return None
    article_id = article.get("id")
    if article_id is None:
        return None
    pub_ts = article.get("datetime")
    published_at = (
        datetime.fromtimestamp(int(pub_ts), tz=UTC)
        if pub_ts
        else datetime.now(UTC)
    )
    return NewsEvent(
        event_id=f"finnhub:{article_id}",
        headline=headline,
        summary=article.get("summary") or "",
        source=article.get("source") or "finnhub",
        url=article.get("url") or "",
        tickers=[ticker],
        event_type=_classify(headline),
        published_at=published_at,
    )
