"""Application configuration loaded from environment variables.

Uses pydantic-settings so that every value can be overridden via env vars
or a .env file at the project root.
"""

from enum import StrEnum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NewsProviderType(StrEnum):
    RSS = "rss"
    BENZINGA = "benzinga"
    FINNHUB = "finnhub"


class MarketDataProviderType(StrEnum):
    YFINANCE = "yfinance"
    POLYGON_FREE = "polygon_free"
    POLYGON_PAID = "polygon_paid"
    ALPACA = "alpaca"
    FINNHUB = "finnhub"


class SentimentProviderType(StrEnum):
    CLAUDE = "claude"
    KEYWORD = "keyword"


class Settings(BaseSettings):
    """Central configuration for the trading system."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Anthropic / Claude ---
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    claude_model: str = Field(
        default="claude-sonnet-4-6",
        description="Claude model id for sentiment analysis",
    )

    # --- LLM tier configuration ---
    llm_provider: str = Field(
        default="anthropic",
        description="LLM provider: 'anthropic' or 'ollama'",
    )
    llm_quick_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Cheap/fast model for classification and debate rounds",
    )
    llm_deep_model: str = Field(
        default="claude-sonnet-4-6",
        description="Accurate model for confidence scoring and signal synthesis",
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1",
        description="Base URL for Ollama's OpenAI-compatible API",
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

    # --- Financial Modeling Prep (earnings calendar + estimates) ---
    fmp_api_key: str = Field(default="", description="Financial Modeling Prep API key")

    # --- Finnhub (news + earnings calendar, free tier) ---
    finnhub_api_key: str = Field(default="", description="Finnhub API key")

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

    # --- Startup ticker selection ---
    small_cap_max_market_cap_usd: int = Field(
        default=2_000_000_000,
        description="Market-cap ceiling (USD) for small-cap filter at startup",
    )
    max_startup_tickers: int = Field(
        default=5,
        description="Max tickers selected at startup (-1 = unlimited)",
    )

    # --- Trading parameters ---
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
        description=(
            "Market data source: yfinance, polygon_free, polygon_paid, or alpaca"
        ),
    )
    sentiment_provider: SentimentProviderType = Field(
        default=SentimentProviderType.CLAUDE,
        description="Sentiment analysis provider: 'claude' or 'keyword'",
    )

    # --- Earnings / surprise thresholds ---
    earn_beat_pct_threshold: float = Field(
        default=2.0,
        description="EPS % surprise above this threshold is classified as BEAT",
    )
    earn_miss_pct_threshold: float = Field(
        default=-2.0,
        description="EPS % surprise below this threshold is classified as MISS",
    )
    earn_strong_sigma_threshold: float = Field(
        default=2.0,
        description="Sigma surprise above this contributes to STRONG signal_strength",
    )
    earn_min_analyst_count: int = Field(
        default=3,
        description=(
            "Minimum analyst count for full coverage_score; below this returns 0.1"
        ),
    )
    earn_guidance_weight: float = Field(
        default=0.20,
        description="Weight of guidance_sentiment in composite_surprise calculation",
    )
    earn_default_beat_rate: float = Field(
        default=0.65,
        description=(
            "Fallback beat rate used for EARN_PRE sizing when Stage1Repository "
            "has fewer than 4 observed outcomes and no FMP data is available"
        ),
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
    sentiment_max_concurrent: int = Field(
        default=5,
        description=(
            "Maximum parallel LLM calls in analyse_batch (semaphore limit). "
            "Reduce when using a local Ollama instance to avoid 429 errors."
        ),
    )
    news_keyword_prefilter: bool = Field(
        default=True,
        description="Pre-filter articles by ticker keyword before Claude analysis",
    )
    article_decay_halflife_hours: float = Field(
        default=72.0,
        description=(
            "Half-life (hours) for exponential article age decay in per-ticker "
            "sentiment aggregation. Older articles contribute less to the aggregated "
            "signal; 72 h gives a broad pre-earnings window."
        ),
    )
    signal_debate_rounds: int = Field(
        default=0,
        description="Number of bull/bear debate rounds per signal (0 = disabled)",
    )
    signal_debate_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model for debate rounds (quick/cheap)",
    )
    signal_debate_threshold: float = Field(
        default=0.70,
        description="Only debate signals whose confidence_score exceeds this value",
    )

    # --- PEAD horizon ---
    pead_horizon_days: int = Field(
        default=5,
        description=(
            "Calendar days after an EARN_BEAT/MISS order fill before the position "
            "is auto-closed by the daily PEAD expiry cron"
        ),
    )

    # --- Risk manager ---
    max_open_positions: int = Field(
        default=5,
        description="Max concurrent open positions; Stage 2 ADD signals are exempt",
    )
    risk_dry_run: bool = Field(
        default=False,
        description="Log risk rejections without blocking signals (calibration mode)",
    )

    # --- Telegram Bot ---
    telegram_bot_token: str = Field(
        default="",
        description="Telegram Bot API token from @BotFather (empty = bot disabled)",
    )
    telegram_chat_id: int = Field(
        default=0,
        description="Telegram chat ID to send messages to (0 = bot disabled)",
    )

    # --- LangSmith observability (optional) ---
    langchain_tracing_v2: bool = Field(
        default=False,
        description="Enable LangSmith tracing (set LANGCHAIN_TRACING_V2=true)",
    )
    langchain_api_key: str = Field(
        default="",
        description="LangSmith API key from smith.langchain.com",
    )
    langchain_project: str = Field(
        default="news-trade",
        description="LangSmith project name for grouping runs",
    )
    langchain_endpoint: str = Field(
        default="https://api.smith.langchain.com",
        description="LangSmith ingestion endpoint (default is the hosted service)",
    )


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()  # type: ignore[call-arg]
