"""Unit tests for provider Protocol implementations and factory functions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from news_trade.config import (
    MarketDataProviderType,
    NewsProviderType,
    SentimentProviderType,
    Settings,
)
from news_trade.models.events import EventType, NewsEvent
from news_trade.models.market import MarketSnapshot, OHLCVBar
from news_trade.models.sentiment import SentimentLabel, SentimentResult
from news_trade.providers import (
    get_market_data_provider,
    get_news_provider,
    get_sentiment_provider,
)
from news_trade.providers.base import MarketDataProvider, NewsProvider, SentimentProvider

NOW = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        anthropic_api_key="test",
        alpaca_api_key="test",
        alpaca_secret_key="test",
    )
    return Settings(**(defaults | kwargs))


def _make_event(event_id: str = "ev-1", ticker: str = "AAPL") -> NewsEvent:
    return NewsEvent(
        event_id=event_id,
        headline="Test headline",
        source="test",
        tickers=[ticker],
        published_at=NOW,
    )


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_rss_is_news_provider(self):
        from news_trade.providers.news.rss import RSSNewsProvider
        p = RSSNewsProvider(watchlist=["AAPL"])
        assert isinstance(p, NewsProvider)

    def test_benzinga_is_news_provider(self):
        from news_trade.providers.news.benzinga import BenzingaNewsProvider
        p = BenzingaNewsProvider(api_key="key")
        assert isinstance(p, NewsProvider)

    def test_yfinance_is_market_provider(self):
        from news_trade.providers.market.yfinance import YFinanceMarketProvider
        p = YFinanceMarketProvider()
        assert isinstance(p, MarketDataProvider)

    def test_polygon_free_is_market_provider(self):
        from news_trade.providers.market.polygon_free import PolygonFreeMarketProvider
        p = PolygonFreeMarketProvider(api_key="key")
        assert isinstance(p, MarketDataProvider)

    def test_polygon_paid_is_market_provider(self):
        from news_trade.providers.market.polygon_paid import PolygonPaidMarketProvider
        p = PolygonPaidMarketProvider(api_key="key")
        assert isinstance(p, MarketDataProvider)

    def test_keyword_is_sentiment_provider(self):
        from news_trade.providers.sentiment.keyword import KeywordSentimentProvider
        p = KeywordSentimentProvider()
        assert isinstance(p, SentimentProvider)


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


class TestGetNewsProvider:
    def test_rss_returns_rss_provider(self):
        s = _make_settings(news_provider=NewsProviderType.RSS)
        p = get_news_provider(s)
        assert p.name == "rss"

    def test_benzinga_returns_benzinga_provider(self):
        s = _make_settings(news_provider=NewsProviderType.BENZINGA)
        p = get_news_provider(s)
        assert p.name == "benzinga"


class TestGetMarketDataProvider:
    def test_yfinance_returns_yfinance_provider(self):
        s = _make_settings(market_data_provider=MarketDataProviderType.YFINANCE)
        p = get_market_data_provider(s)
        assert p.name == "yfinance"

    def test_polygon_free_returns_polygon_free_provider(self):
        s = _make_settings(market_data_provider=MarketDataProviderType.POLYGON_FREE)
        p = get_market_data_provider(s)
        assert p.name == "polygon_free"

    def test_polygon_paid_returns_polygon_paid_provider(self):
        s = _make_settings(market_data_provider=MarketDataProviderType.POLYGON_PAID)
        p = get_market_data_provider(s)
        assert p.name == "polygon_paid"


class TestGetSentimentProvider:
    def test_keyword_returns_keyword_provider(self):
        s = _make_settings(sentiment_provider=SentimentProviderType.KEYWORD)
        p = get_sentiment_provider(s)
        assert p.name == "keyword"

    def test_claude_returns_claude_provider(self):
        s = _make_settings(sentiment_provider=SentimentProviderType.CLAUDE)
        p = get_sentiment_provider(s)
        assert p.name == "claude"


# ---------------------------------------------------------------------------
# KeywordSentimentProvider logic
# ---------------------------------------------------------------------------


class TestKeywordSentimentProvider:
    @pytest.fixture()
    def provider(self):
        from news_trade.providers.sentiment.keyword import KeywordSentimentProvider
        return KeywordSentimentProvider()

    async def test_bullish_headline(self, provider):
        event = NewsEvent(
            event_id="ev-1",
            headline="Apple beats earnings and surges",
            source="test",
            tickers=["AAPL"],
            published_at=NOW,
        )
        result = await provider.analyse(event)
        assert result.score > 0
        assert result.label in (SentimentLabel.BULLISH, SentimentLabel.VERY_BULLISH)

    async def test_bearish_headline(self, provider):
        event = NewsEvent(
            event_id="ev-2",
            headline="Company files for bankruptcy amid fraud investigation",
            source="test",
            tickers=["XYZ"],
            published_at=NOW,
        )
        result = await provider.analyse(event)
        assert result.score < 0
        assert result.label in (SentimentLabel.BEARISH, SentimentLabel.VERY_BEARISH)

    async def test_neutral_headline(self, provider):
        event = NewsEvent(
            event_id="ev-3",
            headline="Company announces meeting date",
            source="test",
            tickers=["ABC"],
            published_at=NOW,
        )
        result = await provider.analyse(event)
        assert result.label == SentimentLabel.NEUTRAL

    async def test_batch_returns_one_per_event(self, provider):
        events = [_make_event(f"ev-{i}") for i in range(3)]
        results = await provider.analyse_batch(events)
        assert len(results) == 3

    def test_confidence_is_fixed(self, provider):
        import asyncio
        event = _make_event()
        result = asyncio.run(provider.analyse(event))
        assert result.confidence == 0.4


# ---------------------------------------------------------------------------
# Settings: provider enums and cost controls
# ---------------------------------------------------------------------------


class TestSettings:
    def test_default_news_provider_is_rss(self):
        s = _make_settings()
        assert s.news_provider == NewsProviderType.RSS

    def test_default_market_provider_is_yfinance(self):
        s = _make_settings()
        assert s.market_data_provider == MarketDataProviderType.YFINANCE

    def test_default_sentiment_provider_is_claude(self):
        s = _make_settings()
        assert s.sentiment_provider == SentimentProviderType.CLAUDE

    def test_default_daily_budget(self):
        s = _make_settings()
        assert s.claude_daily_budget_usd == 2.00

    def test_default_dry_run_false(self):
        s = _make_settings()
        assert s.sentiment_dry_run is False

    def test_default_keyword_prefilter_true(self):
        s = _make_settings()
        assert s.news_keyword_prefilter is True

    def test_enum_coercion_from_string(self):
        s = _make_settings(news_provider="benzinga")
        assert s.news_provider == NewsProviderType.BENZINGA
