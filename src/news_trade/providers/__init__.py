"""Provider factory — reads config and returns the right concrete provider.

Import ``get_news_provider``, ``get_market_data_provider``, or
``get_sentiment_provider`` to obtain a fully-configured provider instance
without coupling agent code to concrete implementations.
"""

from __future__ import annotations

from news_trade.config import (
    MarketDataProviderType,
    NewsProviderType,
    SentimentProviderType,
    Settings,
    get_settings,
)
from news_trade.providers.base import MarketDataProvider, NewsProvider, SentimentProvider


def get_news_provider(settings: Settings | None = None) -> NewsProvider:
    """Return the configured NewsProvider implementation."""
    cfg = settings or get_settings()
    match cfg.news_provider:
        case NewsProviderType.RSS:
            from news_trade.providers.news.rss import RSSNewsProvider
            return RSSNewsProvider(watchlist=cfg.watchlist)
        case NewsProviderType.BENZINGA:
            from news_trade.providers.news.benzinga import BenzingaNewsProvider
            return BenzingaNewsProvider(api_key=cfg.benzinga_api_key)
        case _:
            from news_trade.providers.news.rss import RSSNewsProvider
            return RSSNewsProvider(watchlist=cfg.watchlist)


def get_market_data_provider(settings: Settings | None = None) -> MarketDataProvider:
    """Return the configured MarketDataProvider implementation."""
    cfg = settings or get_settings()
    match cfg.market_data_provider:
        case MarketDataProviderType.YFINANCE:
            from news_trade.providers.market.yfinance import YFinanceMarketProvider
            return YFinanceMarketProvider()
        case MarketDataProviderType.POLYGON_FREE:
            from news_trade.providers.market.polygon_free import PolygonFreeMarketProvider
            return PolygonFreeMarketProvider(api_key=cfg.polygon_api_key)
        case MarketDataProviderType.POLYGON_PAID:
            from news_trade.providers.market.polygon_paid import PolygonPaidMarketProvider
            return PolygonPaidMarketProvider(api_key=cfg.polygon_api_key)
        case _:
            from news_trade.providers.market.yfinance import YFinanceMarketProvider
            return YFinanceMarketProvider()


def get_sentiment_provider(settings: Settings | None = None) -> SentimentProvider:
    """Return the configured SentimentProvider implementation."""
    cfg = settings or get_settings()
    match cfg.sentiment_provider:
        case SentimentProviderType.CLAUDE:
            from news_trade.providers.sentiment.claude import ClaudeSentimentProvider
            from news_trade.services.llm_client import LLMClientFactory
            return ClaudeSentimentProvider(
                llm=LLMClientFactory(cfg),
                daily_budget=cfg.claude_daily_budget_usd,
            )
        case SentimentProviderType.KEYWORD:
            from news_trade.providers.sentiment.keyword import KeywordSentimentProvider
            return KeywordSentimentProvider()
        case _:
            from news_trade.providers.sentiment.keyword import KeywordSentimentProvider
            return KeywordSentimentProvider()
