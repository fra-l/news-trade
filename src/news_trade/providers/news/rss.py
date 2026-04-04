"""RSSNewsProvider — fetches news from yfinance, MarketWatch, and EDGAR RSS feeds.

Phase 1 free-tier news source.  No API key required.

Note: Yahoo Finance shut down their per-ticker RSS endpoint
(feeds.finance.yahoo.com) circa 2024.  Per-ticker news is now fetched via
the yfinance library's Ticker.news property, which uses Yahoo's unofficial
JSON API and remains functional.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from news_trade.models.events import EventType, NewsEvent

_logger = logging.getLogger(__name__)

# Global RSS feeds (still operational)
_MARKETWATCH_RSS = "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"
_EDGAR_RSS = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom"

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


def _parse_dt(value: str) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return parsedate_to_datetime(value)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _extract_tickers_from_text(text: str, watchlist: set[str]) -> list[str]:
    """Extract watchlist tickers mentioned in text via word-boundary match."""
    found = []
    for ticker in watchlist:
        if re.search(rf"\b{re.escape(ticker)}\b", text):
            found.append(ticker)
    return found


class RSSNewsProvider:
    """Fetches news from free RSS feeds and maps items to NewsEvent objects.

    Combines Yahoo Finance per-ticker feeds with a global MarketWatch feed
    and the SEC EDGAR 8-K atom feed.
    """

    def __init__(self, watchlist: list[str]) -> None:
        self._watchlist = watchlist
        self._watchlist_set = set(watchlist)

    @property
    def name(self) -> str:
        return "rss"

    async def fetch(
        self,
        tickers: list[str],
        since: datetime | None = None,
    ) -> list[NewsEvent]:
        """Fetch news from RSS feeds for the given tickers."""
        events: list[NewsEvent] = []
        seen_ids: set[str] = set()

        # Per-ticker news via yfinance (replaces dead Yahoo RSS per-ticker endpoint)
        for ticker in tickers:
            try:
                raw_news = await asyncio.to_thread(_fetch_yfinance_news, ticker)
                for article in raw_news:
                    item = _yfinance_article_to_event(article, ticker)
                    if item is not None and item.event_id not in seen_ids:
                        seen_ids.add(item.event_id)
                        events.append(item)
            except Exception as exc:
                _logger.warning("yfinance news fetch failed for %s: %s", ticker, exc)

        async with httpx.AsyncClient(timeout=15.0) as client:
            # Global MarketWatch feed — extract tickers from headlines
            try:
                resp = await client.get(_MARKETWATCH_RSS)
                resp.raise_for_status()
                items = _parse_rss_feed(resp.text, source="rss_marketwatch")
                watchlist_set = set(tickers)
                for item in items:
                    if item.event_id not in seen_ids:
                        found = _extract_tickers_from_text(
                            item.headline + " " + item.summary,
                            watchlist_set,
                        )
                        if found:
                            seen_ids.add(item.event_id)
                            events.append(item.model_copy(update={"tickers": found}))
            except httpx.HTTPError as exc:
                _logger.warning("MarketWatch RSS fetch failed: %s", exc)

        if since is not None:
            events = [e for e in events if e.published_at >= since]

        return events


def _fetch_yfinance_news(ticker: str) -> list[dict]:
    """Synchronous yfinance call — run via asyncio.to_thread."""
    import yfinance as yf  # type: ignore[import-untyped]  # lazy import
    return yf.Ticker(ticker).news or []


def _yfinance_article_to_event(article: dict, ticker: str) -> NewsEvent | None:
    """Convert a yfinance news dict to a NewsEvent.

    yfinance >=0.2.50 nests article data under a ``content`` key.
    Older flat format (``title``, ``link``, ``providerPublishTime``) is
    also supported as a fallback.
    """
    # New nested format (yfinance >=0.2.50)
    content: dict = article.get("content") or article

    title = content.get("title", "").strip()
    if not title:
        return None

    uid = (
        article.get("id")
        or content.get("id")
        or content.get("canonicalUrl", {}).get("url")
        or title
    )
    summary = content.get("summary") or ""

    # Publisher
    provider = content.get("provider") or {}
    source = provider.get("displayName") or content.get("publisher") or "yfinance_news"

    # URL
    canon = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
    url = canon.get("url") or content.get("link") or ""

    # Published timestamp — ISO string (new) or Unix int (old)
    pub_date_str = content.get("pubDate")
    pub_ts = content.get("providerPublishTime")
    if pub_date_str:
        published_at = _parse_dt(pub_date_str)
    elif pub_ts:
        published_at = datetime.fromtimestamp(int(pub_ts), tz=UTC)
    else:
        published_at = datetime.now(UTC)

    return NewsEvent(
        event_id=f"yfinance_news:{uid}",
        headline=title,
        summary=summary,
        source=source,
        url=url,
        tickers=[ticker],
        event_type=_classify(title),
        published_at=published_at,
    )


def _parse_rss_feed(xml_text: str, source: str) -> list[NewsEvent]:
    """Parse an RSS 2.0 XML response into NewsEvent objects."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        _logger.warning("Failed to parse RSS XML from %s", source)
        return []

    # Support both RSS 2.0 (<item>) and Atom (<entry>)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") or root.findall(".//atom:entry", ns)

    events: list[NewsEvent] = []
    for item in items:
        def _text(tag: str, _item: ET.Element = item) -> str:
            el = _item.find(tag) or _item.find(f"atom:{tag}", ns)
            return (el.text or "").strip() if el is not None else ""

        guid = _text("guid") or _text("id") or _text("link")
        headline = _text("title")
        summary = _text("description") or _text("summary")
        url = _text("link")
        pub_date = _text("pubDate") or _text("published") or _text("updated")

        if not guid or not headline:
            continue

        events.append(
            NewsEvent(
                event_id=f"{source}:{guid}",
                headline=headline,
                summary=summary,
                source=source,
                url=url,
                tickers=[],
                event_type=_classify(headline),
                published_at=_parse_dt(pub_date),
            )
        )

    return events
