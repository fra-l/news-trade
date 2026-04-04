"""PolygonPaidMarketProvider — Polygon.io Starter+ (premium) market data.

Phase 2 premium.  Requires a Polygon.io Starter or higher subscription.
Unlocks higher rate limits, real-time data, and more history.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

import httpx

from news_trade.models.market import MarketSnapshot, OHLCVBar

_logger = logging.getLogger(__name__)
_AGGS_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{from_}/{to}"


class PolygonPaidMarketProvider:
    """Fetches OHLCV bars from Polygon.io using a paid-tier API key.

    Identical implementation to the free-tier provider but uses a separate
    configuration key to make the billing tier explicit in settings.
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "polygon_paid"

    async def get_snapshot(self, ticker: str) -> MarketSnapshot:
        today = datetime.now(UTC).date()
        from_ = (today - timedelta(days=45)).isoformat()
        to = today.isoformat()

        url = _AGGS_URL.format(ticker=ticker, from_=from_, to=to)
        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50,
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
        closes = [b.close for b in bars]
        log_returns = (
            [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
            if len(closes) > 1
            else []
        )
        if log_returns:
            n = len(log_returns)
            mean = sum(log_returns) / n
            variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
            volatility_20d = math.sqrt(variance * 252)
        else:
            volatility_20d = 0.0

        return MarketSnapshot(
            ticker=ticker,
            latest_close=latest.close,
            volume=latest.volume,
            vwap=latest.vwap,
            volatility_20d=volatility_20d,
            bars=bars,
            fetched_at=datetime.now(UTC),
        )

    async def get_snapshots(self, tickers: list[str]) -> dict[str, MarketSnapshot]:
        results: dict[str, MarketSnapshot] = {}
        for ticker in tickers:
            try:
                results[ticker] = await self.get_snapshot(ticker)
            except Exception as exc:
                _logger.warning("Polygon paid snapshot failed for %s: %s", ticker, exc)
        return results
