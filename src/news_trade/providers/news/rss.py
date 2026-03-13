"""RSSNewsProvider — fetches news from Yahoo Finance, MarketWatch, and EDGAR RSS feeds.

Phase 1 free-tier news source.  No API key required.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from news_trade.models.events import EventType, NewsEvent

_logger = logging.getLogger(__name__)

# RSS feed templates — {ticker} is replaced at runtime where applicable
_YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
_MARKETWATCH_RSS = "https://feeds.marketwatch.com/marketwatch/realtimeheadlines/"
_EDGAR_RSS = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom"

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
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


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

        async with httpx.AsyncClient(timeout=15.0) as client:
            # Per-ticker Yahoo Finance feeds
            for ticker in tickers:
                try:
                    resp = await client.get(_YAHOO_RSS.format(ticker=ticker))
                    resp.raise_for_status()
                    items = _parse_rss_feed(resp.text, source="rss_yahoo")
                    for item in items:
                        if item.event_id not in seen_ids:
                            seen_ids.add(item.event_id)
                            if not item.tickers:
                                item = item.model_copy(update={"tickers": [ticker]})
                            events.append(item)
                except httpx.HTTPError as exc:
                    _logger.warning("Yahoo RSS fetch failed for %s: %s", ticker, exc)

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
        def _text(tag: str) -> str:
            el = item.find(tag) or item.find(f"atom:{tag}", ns)
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
