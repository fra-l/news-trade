"""BenzingaNewsProvider — fetches news from the Benzinga API.

Phase 2 premium news source.  Requires a Benzinga API key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from news_trade.models.events import EventType, NewsEvent

_BENZINGA_NEWS_URL = "https://api.benzinga.com/api/v2/news"

_KEYWORD_MAP: list[tuple[frozenset[str], EventType]] = [
    (frozenset(["fda", "approval", "drug", "clinical", "trial"]), EventType.FDA_APPROVAL),
    (frozenset(["merger", "acquisition", "acquires", "takeover", "buyout"]), EventType.MERGER_ACQUISITION),
    (frozenset(["sec", "10-k", "10-q", "8-k"]), EventType.SEC_FILING),
    (frozenset(["analyst", "upgrade", "downgrade", "price target", "overweight", "underweight"]), EventType.ANALYST_RATING),
    (frozenset(["guidance", "outlook", "forecast", "raises guidance", "lowers guidance"]), EventType.GUIDANCE),
    (frozenset(["earnings", "eps", "revenue", "quarterly results", "net income"]), EventType.EARNINGS),
    (frozenset(["fed", "rate hike", "inflation", "gdp", "unemployment", "macro"]), EventType.MACRO),
]


def _classify(headline: str) -> EventType:
    lower = headline.lower()
    for keywords, event_type in _KEYWORD_MAP:
        if any(kw in lower for kw in keywords):
            return event_type
    return EventType.OTHER


def _parse_dt(value: str) -> datetime:
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


class BenzingaNewsProvider:
    """Fetches articles from the Benzinga News API (v2).

    Requires a valid Benzinga API key.  Returns up to 50 articles per call.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "benzinga"

    async def fetch(
        self,
        tickers: list[str],
        since: datetime | None = None,
    ) -> list[NewsEvent]:
        """Fetch recent articles from Benzinga filtered to the given tickers."""
        params: dict[str, str | int] = {
            "token": self._api_key,
            "pageSize": 50,
            "displayOutput": "full",
        }
        if tickers:
            params["tickers"] = ",".join(tickers)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_BENZINGA_NEWS_URL, params=params)
            resp.raise_for_status()
            articles: list[dict] = resp.json()

        events: list[NewsEvent] = []
        for article in articles:
            article_tickers = [s["name"] for s in (article.get("stocks") or [])]
            published_at = _parse_dt(article.get("created", ""))
            if since is not None and published_at < since:
                continue
            events.append(
                NewsEvent(
                    event_id=str(article["id"]),
                    headline=article.get("title", ""),
                    summary=article.get("teaser", ""),
                    source="benzinga",
                    url=article.get("url", ""),
                    tickers=article_tickers,
                    event_type=_classify(article.get("title", "")),
                    published_at=published_at,
                )
            )
        return events
