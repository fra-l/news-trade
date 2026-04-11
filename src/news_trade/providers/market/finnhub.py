"""FinnhubMarketDataProvider — Finnhub.io market data.

Uses the /stock/candle endpoint (daily resolution) which is available on the
free tier.  Requires a FINNHUB_API_KEY.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime

import httpx

from news_trade.models.market import MarketSnapshot, OHLCVBar
from news_trade.providers._http import http_get_with_retry

_logger = logging.getLogger(__name__)
_CANDLE_URL = "https://finnhub.io/api/v1/stock/candle"
# Bound concurrent candle requests — avoids thundering herd on 429 bursts.
_SNAPSHOT_CONCURRENCY = 3


class FinnhubMarketDataProvider:
    """Fetches OHLCV bars from Finnhub using the daily candle endpoint."""

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "finnhub"

    async def get_snapshot(self, ticker: str) -> MarketSnapshot:
        """Fetch ~45 daily bars from Finnhub and compute volatility metrics."""
        now = int(datetime.now(UTC).timestamp())
        # 45 calendar days back covers ~30 trading days
        from_ = now - 45 * 86400

        params = {
            "symbol": ticker,
            "resolution": "D",
            "from": from_,
            "to": now,
            "token": self._api_key,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await http_get_with_retry(client, _CANDLE_URL, params=params)
            data = resp.json()

        if data.get("s") != "ok":
            status = data.get("s")
            raise ValueError(f"Finnhub returned no data for {ticker} (status={status})")

        timestamps: list[int] = data["t"]
        opens: list[float] = data["o"]
        highs: list[float] = data["h"]
        lows: list[float] = data["l"]
        closes: list[float] = data["c"]
        volumes: list[int] = data["v"]

        bars: list[OHLCVBar] = [
            OHLCVBar(
                timestamp=datetime.fromtimestamp(t, tz=UTC),
                open=float(o),
                high=float(h),
                low=float(low),
                close=float(c),
                volume=int(v),
                # Finnhub free tier does not provide VWAP; use close as fallback
                vwap=float(c),
            )
            for t, o, h, low, c, v in zip(
                timestamps, opens, highs, lows, closes, volumes, strict=True
            )
        ]

        latest = bars[-1]
        return MarketSnapshot(
            ticker=ticker,
            latest_close=latest.close,
            volume=latest.volume,
            vwap=latest.vwap,
            volatility_20d=_compute_volatility(bars),
            atr_14d=_compute_atr(bars),
            relative_volume=_compute_relative_volume(bars),
            bars=bars,
            fetched_at=datetime.now(UTC),
        )

    async def get_snapshots(self, tickers: list[str]) -> dict[str, MarketSnapshot]:
        """Batch-fetch snapshots concurrently with bounded concurrency."""
        sem = asyncio.Semaphore(_SNAPSHOT_CONCURRENCY)

        async def _safe(ticker: str) -> tuple[str, MarketSnapshot | None]:
            async with sem:
                try:
                    return ticker, await self.get_snapshot(ticker)
                except Exception as exc:
                    _logger.warning("Finnhub snapshot failed for %s: %s", ticker, exc)
                    return ticker, None

        pairs = await asyncio.gather(*(_safe(t) for t in tickers))
        return {ticker: snap for ticker, snap in pairs if snap is not None}


def _compute_volatility(bars: list[OHLCVBar]) -> float:
    closes = [b.close for b in bars]
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    # Sample variance requires at least 2 log returns (3 bars); return 0 for thin data.
    if len(log_returns) < 2:
        return 0.0
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
