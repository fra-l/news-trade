"""Application configuration loaded from environment variables.

Uses pydantic-settings so that every value can be overridden via env vars
or a .env file at the project root.
"""

from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NewsProviderType(str, Enum):
    RSS = "rss"
    BENZINGA = "benzinga"


class MarketDataProviderType(str, Enum):
    YFINANCE = "yfinance"
    POLYGON_FREE = "polygon_free"
    POLYGON_PAID = "polygon_paid"
    ALPACA = "alpaca"


class SentimentProviderType(str, Enum):
    CLAUDE = "claude"
    KEYWORD = "keyword"


class Settings(BaseSettings):
    """Central configuration for the trading system."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Anthropic / Claude ---
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    claude_model: str = Field(
        default="claude-sonnet-4-6",
        description="Claude model id for sentiment analysis",
    )

    # --- Alpaca Markets ---
    alpaca_api_key: str = Field(default="", description="Alpaca API key id")
    alpaca_secret_key: str = Field(default="", description="Alpaca API secret key")
    alpaca_base_url: str = Field(
        default="https://paper-api.alpaca.markets",
        description="Alpaca base URL (paper or live)",
    )

    # --- News provider (legacy field kept for backwards compat) ---
    benzinga_api_key: str = Field(default="", description="Benzinga API key")
    polygon_api_key: str = Field(default="", description="Polygon.io API key")

    # --- Redis ---
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for the event bus",
    )

    # --- Database ---
    database_url: str = Field(
        default="sqlite:///data/trades.db",
        description="SQLAlchemy connection string",
    )

    # --- Watchlist & trading parameters ---
    watchlist: list[str] = Field(
        default=["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"],
        description="Tickers to monitor for news",
    )
    news_poll_interval_sec: int = Field(
        default=30,
        description="Seconds between news API polls",
    )
    max_position_pct: float = Field(
        default=0.05,
        description="Max fraction of equity per position",
    )
    max_total_positions: int = Field(
        default=10,
        description="Maximum number of concurrent open positions",
    )
    max_drawdown_pct: float = Field(
        default=0.03,
        description="Hard stop: max peak-to-trough drawdown fraction",
    )
    min_signal_conviction: float = Field(
        default=0.6,
        description="Minimum conviction score to generate a trade signal",
    )

    # --- Provider selection ---
    news_provider: NewsProviderType = Field(
        default=NewsProviderType.RSS,
        description="News data source: 'rss' (free) or 'benzinga' (premium)",
    )
    market_data_provider: MarketDataProviderType = Field(
        default=MarketDataProviderType.YFINANCE,
        description="Market data source: yfinance, polygon_free, polygon_paid, or alpaca",
    )
    sentiment_provider: SentimentProviderType = Field(
        default=SentimentProviderType.CLAUDE,
        description="Sentiment analysis provider: 'claude' or 'keyword'",
    )

    # --- Cost controls ---
    claude_daily_budget_usd: float = Field(
        default=2.00,
        description="Maximum daily Claude API spend in USD",
    )
    sentiment_dry_run: bool = Field(
        default=False,
        description="Skip real sentiment API calls; use mock neutral scores",
    )
    news_keyword_prefilter: bool = Field(
        default=True,
        description="Pre-filter articles by ticker keyword before Claude analysis",
    )


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()  # type: ignore[call-arg]
