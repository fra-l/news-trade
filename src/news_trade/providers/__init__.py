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
from news_trade.providers.base import (
    CalendarProvider,
    EstimatesProvider,
    MarketDataProvider,
    NewsProvider,
    SentimentProvider,
)


def get_news_provider(settings: Settings | None = None) -> NewsProvider:
    """Return the configured NewsProvider implementation."""
    cfg = settings or get_settings()
    match cfg.news_provider:
        case NewsProviderType.RSS:
            from news_trade.providers.news.rss import RSSNewsProvider
            return RSSNewsProvider()
        case NewsProviderType.BENZINGA:
            from news_trade.providers.news.benzinga import BenzingaNewsProvider
            return BenzingaNewsProvider(api_key=cfg.benzinga_api_key)
        case NewsProviderType.FINNHUB:
            from news_trade.providers.news.finnhub import FinnhubNewsProvider
            return FinnhubNewsProvider(api_key=cfg.finnhub_api_key)
        case _:
            from news_trade.providers.news.rss import RSSNewsProvider
            return RSSNewsProvider()


def get_market_data_provider(settings: Settings | None = None) -> MarketDataProvider:
    """Return the configured MarketDataProvider implementation."""
    cfg = settings or get_settings()
    match cfg.market_data_provider:
        case MarketDataProviderType.YFINANCE:
            from news_trade.providers.market.yfinance import YFinanceMarketProvider
            return YFinanceMarketProvider()
        case MarketDataProviderType.POLYGON_FREE:
            from news_trade.providers.market.polygon_free import (
                PolygonFreeMarketProvider,
            )
            return PolygonFreeMarketProvider(api_key=cfg.polygon_api_key)
        case MarketDataProviderType.POLYGON_PAID:
            from news_trade.providers.market.polygon_paid import (
                PolygonPaidMarketProvider,
            )
            return PolygonPaidMarketProvider(api_key=cfg.polygon_api_key)
        case MarketDataProviderType.FINNHUB:
            from news_trade.providers.market.finnhub import FinnhubMarketDataProvider
            return FinnhubMarketDataProvider(api_key=cfg.finnhub_api_key)
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
                max_concurrent=cfg.sentiment_max_concurrent,
            )
        case SentimentProviderType.KEYWORD:
            from news_trade.providers.sentiment.keyword import KeywordSentimentProvider
            return KeywordSentimentProvider()
        case _:
            from news_trade.providers.sentiment.keyword import KeywordSentimentProvider
            return KeywordSentimentProvider()


def get_estimates_provider(
    settings: Settings | None = None,
) -> EstimatesProvider | None:
    """Return an FMPEstimatesProvider when an FMP API key is configured.

    Returns ``None`` when no key is set so callers fall back to the static
    ``earn_default_beat_rate`` without raising.
    """
    cfg = settings or get_settings()
    if cfg.fmp_api_key:
        from news_trade.providers.estimates.fmp import FMPEstimatesProvider
        return FMPEstimatesProvider(api_key=cfg.fmp_api_key)
    return None


def get_calendar_provider(settings: Settings | None = None) -> CalendarProvider:
    """Return the primary CalendarProvider.

    Priority: Finnhub (free tier, supports broad market scan) → FMP (paid,
    richer EPS data) → yfinance (per-ticker only, no key required).

    FMP is kept for EPS beat-rate estimates (``get_estimates_provider``) but
    Finnhub is preferred for the calendar because its free tier supports
    broad date-range scans without specifying individual tickers.
    """
    cfg = settings or get_settings()
    if cfg.finnhub_api_key:
        from news_trade.providers.calendar.finnhub import FinnhubCalendarProvider
        return FinnhubCalendarProvider(api_key=cfg.finnhub_api_key)
    if cfg.fmp_api_key:
        from news_trade.providers.calendar.fmp import FMPCalendarProvider
        return FMPCalendarProvider(api_key=cfg.fmp_api_key)
    from news_trade.providers.calendar.yfinance_provider import YFinanceCalendarProvider
    return YFinanceCalendarProvider()
