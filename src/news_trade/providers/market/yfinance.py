"""YFinanceMarketProvider — fetches OHLCV data via the yfinance library.

Phase 1 free-tier market data source.  No API key required.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from news_trade.models.market import MarketSnapshot, OHLCVBar

_logger = logging.getLogger(__name__)


class YFinanceMarketProvider:
    """Fetches market data using the open-source yfinance library."""

    @property
    def name(self) -> str:
        return "yfinance"

    async def get_snapshot(self, ticker: str) -> MarketSnapshot:
        """Fetch the last 30 daily bars and compute volatility metrics."""
        import yfinance as yf  # lazy import — optional dependency

        hist = yf.Ticker(ticker).history(period="30d", interval="1d")
        if hist.empty:
            raise ValueError(f"yfinance returned no data for {ticker}")

        bars: list[OHLCVBar] = []
        for ts, row in hist.iterrows():
            bars.append(
                OHLCVBar(
                    timestamp=ts.to_pydatetime().replace(tzinfo=timezone.utc),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]),
                    vwap=float(row["Close"]),  # yfinance doesn't provide VWAP
                )
            )

        latest = bars[-1]
        volatility_20d = _compute_volatility(bars)
        atr_14d = _compute_atr(bars)
        relative_volume = _compute_relative_volume(bars)

        return MarketSnapshot(
            ticker=ticker,
            latest_close=latest.close,
            volume=latest.volume,
            vwap=latest.vwap,
            volatility_20d=volatility_20d,
            atr_14d=atr_14d,
            relative_volume=relative_volume,
            bars=bars,
            fetched_at=datetime.now(timezone.utc),
        )

    async def get_snapshots(self, tickers: list[str]) -> dict[str, MarketSnapshot]:
        """Batch-fetch snapshots for multiple tickers."""
        results: dict[str, MarketSnapshot] = {}
        for ticker in tickers:
            try:
                results[ticker] = await self.get_snapshot(ticker)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("yfinance snapshot failed for %s: %s", ticker, exc)
        return results


def _compute_volatility(bars: list[OHLCVBar]) -> float:
    """Annualized realized volatility from log returns of daily closes."""
    closes = [b.close for b in bars]
    if len(closes) < 2:
        return 0.0
    import math
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    return math.sqrt(variance * 252)  # annualize


def _compute_atr(bars: list[OHLCVBar], period: int = 14) -> float | None:
    """Average True Range over the last ``period`` bars."""
    if len(bars) < 2:
        return None
    true_ranges: list[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - prev_close),
            abs(bars[i].low - prev_close),
        )
        true_ranges.append(tr)
    window = true_ranges[-period:]
    return sum(window) / len(window)


def _compute_relative_volume(bars: list[OHLCVBar]) -> float | None:
    """Today's volume divided by the 20-day average volume."""
    if len(bars) < 2:
        return None
    avg_vol = sum(b.volume for b in bars[:-1]) / len(bars[:-1])
    if avg_vol == 0:
        return None
    return bars[-1].volume / avg_vol
