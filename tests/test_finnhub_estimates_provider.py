"""Tests for FinnhubEstimatesProvider and its _compute_beat_rate helper.

All tests mock http_get_with_retry — no real network calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from news_trade.providers.base import EstimatesProvider
from news_trade.providers.estimates.finnhub import (
    FinnhubEstimatesProvider,
    _compute_beat_rate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(api_key: str = "test-key") -> FinnhubEstimatesProvider:
    return FinnhubEstimatesProvider(api_key=api_key)


def _make_record(actual: float | None, estimate: float | None) -> dict:
    return {"actual": actual, "estimate": estimate, "period": "2024-03-31"}


def _mock_http(records: list[dict]) -> AsyncMock:
    """Return an AsyncMock for http_get_with_retry that yields *records*."""
    resp = MagicMock()
    resp.json.return_value = records
    return AsyncMock(return_value=resp)


# ---------------------------------------------------------------------------
# TestFinnhubEstimatesProviderInit
# ---------------------------------------------------------------------------


class TestFinnhubEstimatesProviderInit:
    def test_name(self) -> None:
        assert _make_provider().name == "finnhub_estimates"

    def test_raises_on_empty_key(self) -> None:
        with pytest.raises(ValueError, match="non-empty api_key"):
            FinnhubEstimatesProvider(api_key="")

    def test_protocol_compliance(self) -> None:
        provider = _make_provider()
        assert isinstance(provider, EstimatesProvider)


# ---------------------------------------------------------------------------
# TestComputeBeatRate (pure unit — no I/O)
# ---------------------------------------------------------------------------


class TestComputeBeatRate:
    def test_majority_beats(self) -> None:
        records = [
            _make_record(2.0, 1.5),  # beat
            _make_record(1.8, 1.9),  # miss
            _make_record(3.0, 2.5),  # beat
            _make_record(2.2, 2.0),  # beat
        ]
        rate = _compute_beat_rate(records, "AAPL")
        assert rate == pytest.approx(3 / 4)

    def test_all_beat(self) -> None:
        records = [_make_record(2.0, 1.0)] * 4
        assert _compute_beat_rate(records, "AAPL") == pytest.approx(1.0)

    def test_all_miss(self) -> None:
        records = [_make_record(1.0, 2.0)] * 4
        assert _compute_beat_rate(records, "AAPL") == pytest.approx(0.0)

    def test_empty_records_returns_none(self) -> None:
        assert _compute_beat_rate([], "AAPL") is None

    def test_all_null_actual_returns_none(self) -> None:
        records = [_make_record(None, 1.5)] * 4
        assert _compute_beat_rate(records, "AAPL") is None

    def test_all_null_estimate_returns_none(self) -> None:
        records = [_make_record(2.0, None)] * 4
        assert _compute_beat_rate(records, "AAPL") is None

    def test_partial_nulls_skipped(self) -> None:
        records = [
            _make_record(None, 1.5),  # skipped
            _make_record(2.0, 1.5),  # beat
            _make_record(1.0, 2.0),  # miss
        ]
        rate = _compute_beat_rate(records, "AAPL")
        assert rate == pytest.approx(0.5)

    def test_in_line_counts_as_miss(self) -> None:
        # actual == estimated → not strictly greater → counts as miss
        records = [_make_record(1.5, 1.5)]
        assert _compute_beat_rate(records, "AAPL") == pytest.approx(0.0)

    def test_non_numeric_strings_skipped(self) -> None:
        records = [
            {"actual": "n/a", "estimate": 1.5, "period": "2024-03-31"},
            _make_record(2.0, 1.5),  # beat
        ]
        rate = _compute_beat_rate(records, "AAPL")
        assert rate == pytest.approx(1.0)

    def test_eight_quarter_default(self) -> None:
        records = [_make_record(float(i + 1), float(i)) for i in range(8)]
        assert _compute_beat_rate(records, "AAPL") == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# TestFinnhubEstimatesProviderGetBeatRate (http_get_with_retry mocked)
# ---------------------------------------------------------------------------


class TestFinnhubEstimatesProviderGetBeatRate:
    _PATCH = "news_trade.providers.estimates.finnhub.http_get_with_retry"

    async def test_happy_path_six_of_eight(self) -> None:
        records = [
            _make_record(2.0, 1.5),  # beat
            _make_record(1.8, 1.9),  # miss
            _make_record(3.0, 2.5),  # beat
            _make_record(2.2, 2.0),  # beat
            _make_record(1.5, 1.2),  # beat
            _make_record(0.8, 1.0),  # miss
            _make_record(2.1, 1.8),  # beat
            _make_record(1.7, 1.6),  # beat
        ]
        with patch(self._PATCH, _mock_http(records)):
            rate = await _make_provider().get_historical_beat_rate("AAPL")
        assert rate == pytest.approx(6 / 8)

    async def test_empty_response_returns_none(self) -> None:
        with patch(self._PATCH, _mock_http([])):
            rate = await _make_provider().get_historical_beat_rate("AAPL")
        assert rate is None

    async def test_network_exception_returns_none(self) -> None:
        with patch(self._PATCH, AsyncMock(side_effect=OSError("timeout"))):
            rate = await _make_provider().get_historical_beat_rate("AAPL")
        assert rate is None

    async def test_ticker_uppercased_in_params(self) -> None:
        mock = _mock_http([_make_record(2.0, 1.5)])
        with patch(self._PATCH, mock):
            await _make_provider().get_historical_beat_rate("aapl")
        _, kwargs = mock.call_args
        params = kwargs.get("params", {})
        assert params.get("symbol") == "AAPL"

    async def test_lookback_slices_records(self) -> None:
        """Only the first `lookback` records should count."""
        # 6 records total; lookback=3 → only first 3 used → 3 beats out of 3
        records = [_make_record(2.0, 1.5)] * 3 + [_make_record(1.0, 2.0)] * 3
        with patch(self._PATCH, _mock_http(records)):
            rate = await _make_provider().get_historical_beat_rate("MSFT", lookback=3)
        assert rate == pytest.approx(1.0)

    async def test_token_in_params(self) -> None:
        mock = _mock_http([_make_record(2.0, 1.5)])
        with patch(self._PATCH, mock):
            await _make_provider().get_historical_beat_rate("TSLA")
        _, kwargs = mock.call_args
        params = kwargs.get("params", {})
        assert params.get("token") == "test-key"
