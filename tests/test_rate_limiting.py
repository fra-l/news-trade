"""Stress tests for HTTP rate-limit handling.

Each provider is exercised with simulated 429 bursts followed by success.
Tests assert that:
  1. Results are eventually fetched — no tickers silently dropped on 429.
  2. Retries are attempted — the provider calls the API more than once.
  3. ``Retry-After`` header is honoured when present.
  4. Retries are exhausted cleanly — ``HTTPStatusError`` raised, not swallowed.

All tests mock ``news_trade.providers._http.asyncio.sleep`` to avoid real waits.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------

# Minimal valid Massive OHLCV payload.
# Needs ≥ 3 bars: _compute_volatility uses sample variance (n-1), so
# 2 bars → 1 log return → division by zero.
_MASSIVE_OK = {
    "results": [
        {"t": 1_700_000_000_000, "o": 100.0, "h": 105.0, "l": 95.0,
         "c": 100.0, "v": 1_000_000, "vw": 101.0},
        {"t": 1_700_086_400_000, "o": 100.0, "h": 107.0, "l": 97.0,
         "c": 102.0, "v": 1_100_000, "vw": 103.0},
        {"t": 1_700_172_800_000, "o": 102.0, "h": 108.0, "l": 98.0,
         "c": 104.0, "v": 1_200_000, "vw": 105.0},
    ]
}

# Minimal valid Finnhub /stock/candle payload (same constraint: ≥ 3 bars).
_FINNHUB_MARKET_OK = {
    "s": "ok",
    "t": [1_700_000_000, 1_700_086_400, 1_700_172_800],
    "o": [100.0, 100.0, 102.0],
    "h": [105.0, 107.0, 108.0],
    "l": [95.0, 97.0, 98.0],
    "c": [100.0, 102.0, 104.0],
    "v": [1_000_000, 1_100_000, 1_200_000],
}

# Minimal valid Finnhub earningsCalendar payload
_FINNHUB_CAL_OK = {
    "earningsCalendar": [
        {
            "symbol": "AAPL",
            "date": "2026-04-14",
            "hour": "bmo",
            "quarter": 2,
            "year": 2026,
            "epsEstimate": 1.50,
        }
    ]
}

_SLEEP = "news_trade.providers._http.asyncio.sleep"
_CLIENT = "httpx.AsyncClient"


def _resp(
    status: int,
    data: dict | None = None,
    retry_after: str | None = None,
) -> MagicMock:
    """Build a mock ``httpx.Response`` with the given status and JSON body."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.headers = {"Retry-After": retry_after} if retry_after else {}
    r.json.return_value = data or {}
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=r
        )
    else:
        r.raise_for_status.return_value = None
    return r


def _make_client(*responses: MagicMock) -> AsyncMock:
    """AsyncMock httpx client whose ``.get()`` yields *responses* in order."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=list(responses))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# ---------------------------------------------------------------------------
# TestHttpGetWithRetry — unit tests for the shared utility
# ---------------------------------------------------------------------------


class TestHttpGetWithRetry:
    """Pure unit tests for ``http_get_with_retry``."""

    async def test_returns_immediately_on_200(self) -> None:
        from news_trade.providers._http import http_get_with_retry

        ok = _resp(200, {"k": "v"})
        client = _make_client(ok)

        with patch(_SLEEP) as mock_sleep:
            result = await http_get_with_retry(client, "https://api.example.com/data")

        assert result is ok
        mock_sleep.assert_not_called()
        assert client.get.call_count == 1

    async def test_retries_once_on_429_then_succeeds(self) -> None:
        from news_trade.providers._http import http_get_with_retry

        client = _make_client(_resp(429), _resp(200, {"ok": True}))

        with patch(_SLEEP) as mock_sleep:
            result = await http_get_with_retry(client, "https://api.example.com/data")

        assert result.status_code == 200
        assert client.get.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    async def test_respects_retry_after_header(self) -> None:
        from news_trade.providers._http import http_get_with_retry

        client = _make_client(
            _resp(429, retry_after="5"),
            _resp(200, {"ok": True}),
        )

        with patch(_SLEEP) as mock_sleep:
            await http_get_with_retry(client, "https://api.example.com/data")

        mock_sleep.assert_called_once_with(5.0)

    async def test_raises_after_max_retries_exhausted(self) -> None:
        from news_trade.providers._http import http_get_with_retry

        # 4 consecutive 429s — default max_retries=3 means 4 total attempts
        client = _make_client(_resp(429), _resp(429), _resp(429), _resp(429))

        with patch(_SLEEP), pytest.raises(httpx.HTTPStatusError):
            await http_get_with_retry(client, "https://api.example.com/data")

        assert client.get.call_count == 4

    async def test_retries_on_503(self) -> None:
        from news_trade.providers._http import http_get_with_retry

        client = _make_client(_resp(503), _resp(200, {"ok": True}))

        with patch(_SLEEP) as mock_sleep:
            result = await http_get_with_retry(client, "https://api.example.com/data")

        assert result.status_code == 200
        mock_sleep.assert_called_once()

    async def test_does_not_retry_on_404(self) -> None:
        from news_trade.providers._http import http_get_with_retry

        client = _make_client(_resp(404))

        with patch(_SLEEP) as mock_sleep, pytest.raises(httpx.HTTPStatusError):
            await http_get_with_retry(client, "https://api.example.com/data")

        # 404 is not retryable — single attempt, no sleep
        assert client.get.call_count == 1
        mock_sleep.assert_not_called()

    async def test_exponential_backoff_doubles_delay(self) -> None:
        from news_trade.providers._http import http_get_with_retry

        # 3 failures then success — delays should be 1 s, 2 s, 4 s
        client = _make_client(
            _resp(429), _resp(429), _resp(429), _resp(200, {"ok": True})
        )

        with patch(_SLEEP) as mock_sleep:
            await http_get_with_retry(client, "https://api.example.com/data")

        assert mock_sleep.call_args_list == [call(1.0), call(2.0), call(4.0)]


# ---------------------------------------------------------------------------
# TestMassiveFreeRateLimiting
# ---------------------------------------------------------------------------


class TestMassiveFreeRateLimiting:
    """MassiveFreeMarketProvider handles 429 bursts without dropping tickers."""

    async def test_get_snapshot_retries_on_429_and_returns_snapshot(self) -> None:
        from news_trade.providers.market.massive_free import MassiveFreeMarketProvider

        provider = MassiveFreeMarketProvider(api_key="test")
        mock_client = _make_client(_resp(429), _resp(200, _MASSIVE_OK))

        with patch(_CLIENT, return_value=mock_client), patch(_SLEEP) as mock_sleep:
            snapshot = await provider.get_snapshot("AAPL")

        assert snapshot.ticker == "AAPL"
        assert snapshot.latest_close == pytest.approx(104.0)
        mock_sleep.assert_called_once_with(1.0)

    async def test_get_snapshots_all_tickers_succeed_despite_burst(self) -> None:
        """5 tickers, each first call is 429 → all eventually return a snapshot."""
        from news_trade.providers.market.massive_free import MassiveFreeMarketProvider

        provider = MassiveFreeMarketProvider(api_key="test")
        tickers = ["A", "B", "C", "D", "E"]
        # 2 responses per ticker (429 then 200), 5 tickers = 10 total
        responses = [_resp(429), _resp(200, _MASSIVE_OK)] * len(tickers)
        mock_client = _make_client(*responses)

        with patch(_CLIENT, return_value=mock_client), patch(_SLEEP):
            snapshots = await provider.get_snapshots(tickers)

        assert set(snapshots.keys()) == set(tickers)
        assert all(s.latest_close == pytest.approx(104.0) for s in snapshots.values())

    async def test_get_snapshot_raises_after_retries_exhausted(self) -> None:
        """Persistent 429 → HTTPStatusError propagates to caller."""
        from news_trade.providers.market.massive_free import MassiveFreeMarketProvider

        provider = MassiveFreeMarketProvider(api_key="test")
        mock_client = _make_client(*[_resp(429)] * 4)

        with (
            patch(_CLIENT, return_value=mock_client),
            patch(_SLEEP),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await provider.get_snapshot("AAPL")


# ---------------------------------------------------------------------------
# TestMassivePaidRateLimiting
# ---------------------------------------------------------------------------


class TestMassivePaidRateLimiting:
    async def test_get_snapshot_retries_on_429(self) -> None:
        from news_trade.providers.market.massive_paid import MassivePaidMarketProvider

        provider = MassivePaidMarketProvider(api_key="test")
        mock_client = _make_client(_resp(429), _resp(200, _MASSIVE_OK))

        with patch(_CLIENT, return_value=mock_client), patch(_SLEEP) as mock_sleep:
            snapshot = await provider.get_snapshot("AAPL")

        assert snapshot.ticker == "AAPL"
        mock_sleep.assert_called_once_with(1.0)

    async def test_get_snapshots_skips_ticker_after_retries_exhausted(self) -> None:
        """When all retries fail, get_snapshots logs a warning and skips the ticker."""
        from news_trade.providers.market.massive_paid import MassivePaidMarketProvider

        provider = MassivePaidMarketProvider(api_key="test")
        # AAPL: always 429 (4 attempts); MSFT: 429 → 200
        responses = [*([_resp(429)] * 4), _resp(429), _resp(200, _MASSIVE_OK)]
        mock_client = _make_client(*responses)

        with patch(_CLIENT, return_value=mock_client), patch(_SLEEP):
            snapshots = await provider.get_snapshots(["AAPL", "MSFT"])

        assert "AAPL" not in snapshots   # exhausted — skipped by get_snapshots
        assert "MSFT" in snapshots       # recovered after one retry


# ---------------------------------------------------------------------------
# TestFinnhubMarketRateLimiting
# ---------------------------------------------------------------------------


class TestFinnhubMarketRateLimiting:
    async def test_get_snapshot_retries_on_429_and_returns_snapshot(self) -> None:
        from news_trade.providers.market.finnhub import FinnhubMarketDataProvider

        provider = FinnhubMarketDataProvider(api_key="test")
        mock_client = _make_client(_resp(429), _resp(200, _FINNHUB_MARKET_OK))

        with patch(_CLIENT, return_value=mock_client), patch(_SLEEP) as mock_sleep:
            snapshot = await provider.get_snapshot("AAPL")

        assert snapshot.ticker == "AAPL"
        assert snapshot.latest_close == pytest.approx(104.0)
        mock_sleep.assert_called_once_with(1.0)

    async def test_get_snapshots_all_tickers_recovered(self) -> None:
        """Concurrent get_snapshots: all tickers recover after one 429 each."""
        from news_trade.providers.market.finnhub import FinnhubMarketDataProvider

        provider = FinnhubMarketDataProvider(api_key="test")
        tickers = ["AAPL", "MSFT", "GOOG"]
        responses = [_resp(429), _resp(200, _FINNHUB_MARKET_OK)] * len(tickers)
        mock_client = _make_client(*responses)

        with patch(_CLIENT, return_value=mock_client), patch(_SLEEP):
            snapshots = await provider.get_snapshots(tickers)

        assert set(snapshots.keys()) == set(tickers)


# ---------------------------------------------------------------------------
# TestFinnhubCalendarRateLimiting
# ---------------------------------------------------------------------------


class TestFinnhubCalendarRateLimiting:
    async def test_broad_scan_retries_on_429(self) -> None:
        from news_trade.providers.calendar.finnhub import FinnhubCalendarProvider

        provider = FinnhubCalendarProvider(api_key="test")
        mock_client = _make_client(_resp(429), _resp(200, _FINNHUB_CAL_OK))
        from_d, to_d = date(2026, 4, 11), date(2026, 4, 25)

        with patch(_CLIENT, return_value=mock_client), patch(_SLEEP) as mock_sleep:
            entries = await provider._broad_scan(from_d, to_d)

        assert len(entries) == 1
        assert entries[0].ticker == "AAPL"
        mock_sleep.assert_called_once_with(1.0)

    async def test_broad_scan_returns_empty_after_retries_exhausted(self) -> None:
        """Persistent 429 on broad scan → empty list (caught by existing handler)."""
        from news_trade.providers.calendar.finnhub import FinnhubCalendarProvider

        provider = FinnhubCalendarProvider(api_key="test")
        mock_client = _make_client(*[_resp(429)] * 4)
        from_d, to_d = date(2026, 4, 11), date(2026, 4, 25)

        with patch(_CLIENT, return_value=mock_client), patch(_SLEEP):
            entries = await provider._broad_scan(from_d, to_d)

        # HTTPStatusError is caught by _broad_scan's except httpx.HTTPError block
        assert entries == []

    async def test_per_ticker_scan_retries_on_429(self) -> None:
        """Each ticker in per-ticker scan retries independently on 429."""
        from news_trade.providers.calendar.finnhub import FinnhubCalendarProvider

        provider = FinnhubCalendarProvider(api_key="test")
        # Two tickers; each gets one 429 then a 200
        mock_client = _make_client(
            _resp(429),
            _resp(200, _FINNHUB_CAL_OK),
            _resp(429),
            _resp(200, _FINNHUB_CAL_OK),
        )
        from_d, to_d = date(2026, 4, 11), date(2026, 4, 25)

        with patch(_CLIENT, return_value=mock_client), patch(_SLEEP) as mock_sleep:
            entries = await provider._per_ticker_scan(["AAPL", "MSFT"], from_d, to_d)

        assert len(entries) == 2
        assert mock_sleep.call_count == 2  # one sleep per ticker retry


# ---------------------------------------------------------------------------
# TestFinnhubVolatilityDivZero — regression for sample-variance bug
# ---------------------------------------------------------------------------


class TestFinnhubVolatilityDivZero:
    """_compute_volatility must not raise ZeroDivisionError with thin data."""

    def _make_bar(self, day: int, close: float) -> object:
        from datetime import UTC, datetime

        from news_trade.models.market import OHLCVBar

        return OHLCVBar(
            timestamp=datetime(2024, 1, day, tzinfo=UTC),
            open=close,
            high=close + 5.0,
            low=close - 5.0,
            close=close,
            volume=1_000_000,
            vwap=close,
        )

    def test_returns_zero_for_single_bar(self) -> None:
        from news_trade.providers.market.finnhub import _compute_volatility

        assert _compute_volatility([self._make_bar(1, 100.0)]) == 0.0

    def test_returns_zero_for_two_bars(self) -> None:
        """2 bars → 1 log return → sample variance needs n>=2; must return 0."""
        from news_trade.providers.market.finnhub import _compute_volatility

        bars = [self._make_bar(i, float(100 + i)) for i in range(1, 3)]
        assert _compute_volatility(bars) == 0.0

    def test_returns_nonzero_for_three_bars(self) -> None:
        """3 bars → 2 log returns → sample variance is well-defined."""
        from news_trade.providers.market.finnhub import _compute_volatility

        bars = [self._make_bar(i, float(100 + i)) for i in range(1, 4)]
        assert _compute_volatility(bars) > 0.0


# ---------------------------------------------------------------------------
# TestFinnhubSnapshotConcurrency — semaphore bounds thundering herd
# ---------------------------------------------------------------------------


class TestFinnhubSnapshotConcurrency:
    """get_snapshots must limit concurrent requests to _SNAPSHOT_CONCURRENCY."""

    async def test_all_tickers_returned_under_semaphore(self) -> None:
        """All tickers are fetched even when concurrency is bounded."""
        from news_trade.providers.market.finnhub import FinnhubMarketDataProvider

        provider = FinnhubMarketDataProvider(api_key="test")
        tickers = ["A", "B", "C", "D", "E"]
        responses = [_resp(200, _FINNHUB_MARKET_OK)] * len(tickers)
        mock_client = _make_client(*responses)

        with patch(_CLIENT, return_value=mock_client), patch(_SLEEP):
            snapshots = await provider.get_snapshots(tickers)

        assert set(snapshots.keys()) == set(tickers)

    async def test_semaphore_limits_peak_concurrency(self) -> None:
        """Peak in-flight count must not exceed _SNAPSHOT_CONCURRENCY."""
        import asyncio as aio
        from datetime import UTC, datetime

        from news_trade.models.market import MarketSnapshot
        from news_trade.providers.market.finnhub import (
            _SNAPSHOT_CONCURRENCY,
            FinnhubMarketDataProvider,
        )

        provider = FinnhubMarketDataProvider(api_key="test")
        tickers = [f"T{i}" for i in range(6)]

        peak_concurrent = 0
        current_concurrent = 0

        async def _tracked(ticker: str) -> MarketSnapshot:
            nonlocal peak_concurrent, current_concurrent
            current_concurrent += 1
            peak_concurrent = max(peak_concurrent, current_concurrent)
            await aio.sleep(0)  # yield so other coroutines can advance
            current_concurrent -= 1
            return MarketSnapshot(
                ticker=ticker,
                latest_close=104.0,
                volume=1_200_000,
                vwap=105.0,
                volatility_20d=0.1,
                bars=[],
                fetched_at=datetime.now(UTC),
            )

        provider.get_snapshot = _tracked  # type: ignore[method-assign]
        snapshots = await provider.get_snapshots(tickers)

        assert set(snapshots.keys()) == set(tickers)
        assert peak_concurrent <= _SNAPSHOT_CONCURRENCY
