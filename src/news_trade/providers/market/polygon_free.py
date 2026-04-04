"""PolygonFreeMarketProvider — Polygon.io free-tier market data.

Phase 1 free-tier.  Requires a free Polygon.io API key (no subscription).
Rate-limited to 5 API calls / minute on the free tier.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

import httpx

from news_trade.models.market import MarketSnapshot, OHLCVBar

_logger = logging.getLogger(__name__)
_AGGS_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{from_}/{to}"


class PolygonFreeMarketProvider:
    """Fetches OHLCV bars from Polygon.io using the free-tier aggregates endpoint."""

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "polygon_free"

    async def get_snapshot(self, ticker: str) -> MarketSnapshot:
        """Fetch 30 daily bars from Polygon and compute volatility metrics."""
        today = datetime.now(UTC).date()
        from_ = (today - timedelta(days=45)).isoformat()
        to = today.isoformat()

        url = _AGGS_URL.format(ticker=ticker, from_=from_, to=to)
        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 30,
            "apiKey": self._api_key,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results") or []
        if not results:
            raise ValueError(f"Polygon returned no data for {ticker}")

        bars: list[OHLCVBar] = []
        for r in results:
            bars.append(
                OHLCVBar(
                    timestamp=datetime.fromtimestamp(r["t"] / 1000, tz=UTC),
                    open=float(r["o"]),
                    high=float(r["h"]),
                    low=float(r["l"]),
                    close=float(r["c"]),
                    volume=int(r["v"]),
                    vwap=float(r.get("vw", r["c"])),
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
            fetched_at=datetime.now(UTC),
        )

    async def get_snapshots(self, tickers: list[str]) -> dict[str, MarketSnapshot]:
        """Batch-fetch snapshots (sequential to respect free-tier rate limits)."""
        results: dict[str, MarketSnapshot] = {}
        for ticker in tickers:
            try:
                results[ticker] = await self.get_snapshot(ticker)
            except Exception as exc:
                _logger.warning("Polygon free snapshot failed for %s: %s", ticker, exc)
        return results


def _compute_volatility(bars: list[OHLCVBar]) -> float:
    closes = [b.close for b in bars]
    if len(closes) < 2:
        return 0.0
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    return math.sqrt(variance * 252)


def _compute_atr(bars: list[OHLCVBar], period: int = 14) -> float | None:
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
    if len(bars) < 2:
        return None
    avg_vol = sum(b.volume for b in bars[:-1]) / len(bars[:-1])
    if avg_vol == 0:
        return None
    return bars[-1].volume / avg_vol
