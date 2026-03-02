"""Market data models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class OHLCVBar(BaseModel):
    """A single OHLCV candlestick bar."""

    timestamp: datetime = Field(description="Bar open timestamp (UTC)")
    open: float = Field(description="Opening price")
    high: float = Field(description="Intrabar high price")
    low: float = Field(description="Intrabar low price")
    close: float = Field(description="Closing price")
    volume: int = Field(description="Total shares traded during the bar")
    vwap: float = Field(description="Volume-weighted average price for the bar")


class MarketSnapshot(BaseModel):
    """Market data snapshot for a single ticker at pipeline execution time.

    Produced by ``MarketDataAgent._build_context()`` and stored in
    ``PipelineState.market_context`` keyed by ticker symbol.
    """

    ticker: str = Field(description="Stock ticker symbol")
    latest_close: float = Field(description="Most-recent bar closing price")
    volume: int = Field(description="Most-recent bar volume")
    vwap: float = Field(description="Most-recent bar VWAP")
    volatility_20d: float = Field(
        description="Annualized 20-day realized volatility (e.g. 0.25 = 25%)"
    )
    bars: list[OHLCVBar] = Field(
        default_factory=list,
        description="Raw OHLCV bars used to compute the snapshot",
    )
    fetched_at: datetime = Field(description="UTC timestamp when data was retrieved")
