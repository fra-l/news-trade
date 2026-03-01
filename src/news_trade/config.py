"""Application configuration loaded from environment variables.

Uses pydantic-settings so that every value can be overridden via env vars
or a .env file at the project root.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the trading system."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Anthropic / Claude ---
    anthropic_api_key: str = Field(description="Anthropic API key")
    claude_model: str = Field(
        default="claude-sonnet-4-6",
        description="Claude model id for sentiment analysis",
    )

    # --- Alpaca Markets ---
    alpaca_api_key: str = Field(description="Alpaca API key id")
    alpaca_secret_key: str = Field(description="Alpaca API secret key")
    alpaca_base_url: str = Field(
        default="https://paper-api.alpaca.markets",
        description="Alpaca base URL (paper or live)",
    )

    # --- News provider ---
    news_provider: str = Field(
        default="benzinga",
        description="News data source: 'benzinga' or 'polygon'",
    )
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
        default=["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"],
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


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()  # type: ignore[call-arg]
