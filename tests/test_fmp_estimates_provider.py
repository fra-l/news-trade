"""Tests for FMPEstimatesProvider and its _compute_beat_rate helper.

All tests mock the aiohttp layer — no real network calls.
aiohttp is not installed in the test environment; tests that exercise the HTTP
path inject a fake ``aiohttp`` module into ``sys.modules`` so the lazy import
inside ``get_historical_beat_rate()`` resolves to the mock.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from news_trade.providers.base import EstimatesProvider
from news_trade.providers.estimates.fmp import FMPEstimatesProvider, _compute_beat_rate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(api_key: str = "test-key") -> FMPEstimatesProvider:
    return FMPEstimatesProvider(api_key=api_key)


def _make_record(actual: float | None, estimated: float | None) -> dict:
    return {"actualEarningResult": actual, "estimatedEarning": estimated}


def _mock_response(status: int, json_data: object) -> MagicMock:
    """Build an aiohttp response mock usable as an async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(resp: MagicMock) -> MagicMock:
    """Build an aiohttp ClientSession mock usable as an async context manager."""
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_fake_aiohttp(session: MagicMock) -> ModuleType:
    """Return a fake aiohttp module whose ClientSession returns *session*."""
    mod = ModuleType("aiohttp")
    mod.ClientSession = MagicMock(return_value=session)  # type: ignore[attr-defined]
    mod.ClientTimeout = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
    # ClientError for exception tests
    mod.ClientError = Exception  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# TestFMPEstimatesProviderInit
# ---------------------------------------------------------------------------


class TestFMPEstimatesProviderInit:
    def test_name(self) -> None:
        assert _make_provider().name == "fmp_estimates"

    def test_raises_on_empty_key(self) -> None:
        with pytest.raises(ValueError, match="non-empty api_key"):
            FMPEstimatesProvider(api_key="")

    def test_protocol_compliance(self) -> None:
        provider = _make_provider()
        assert isinstance(provider, EstimatesProvider)


# ---------------------------------------------------------------------------
# TestComputeBeatRate (pure unit — no I/O)
# ---------------------------------------------------------------------------


class TestComputeBeatRate:
    def test_majority_beats(self) -> None:
        records = [
            _make_record(2.0, 1.5),   # beat
            _make_record(1.8, 1.9),   # miss
            _make_record(3.0, 2.5),   # beat
            _make_record(2.2, 2.0),   # beat
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

    def test_all_null_estimated_returns_none(self) -> None:
        records = [_make_record(2.0, None)] * 4
        assert _compute_beat_rate(records, "AAPL") is None

    def test_partial_nulls_skipped(self) -> None:
        records = [
            _make_record(None, 1.5),   # skipped
            _make_record(2.0, 1.5),    # beat
            _make_record(1.0, 2.0),    # miss
        ]
        rate = _compute_beat_rate(records, "AAPL")
        assert rate == pytest.approx(0.5)

    def test_in_line_counts_as_miss(self) -> None:
        # actual == estimated → not strictly greater → counts as miss
        records = [_make_record(1.5, 1.5)]
        assert _compute_beat_rate(records, "AAPL") == pytest.approx(0.0)

    def test_non_numeric_strings_skipped(self) -> None:
        records = [
            {"actualEarningResult": "n/a", "estimatedEarning": 1.5},
            _make_record(2.0, 1.5),  # beat
        ]
        rate = _compute_beat_rate(records, "AAPL")
        assert rate == pytest.approx(1.0)

    def test_eight_quarter_default(self) -> None:
        records = [_make_record(float(i + 1), float(i)) for i in range(8)]
        rate = _compute_beat_rate(records, "AAPL")
        assert rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# TestFMPEstimatesProviderGetBeatRate (with aiohttp mocked via sys.modules)
# ---------------------------------------------------------------------------


class TestFMPEstimatesProviderGetBeatRate:
    """Exercise the HTTP path by injecting a fake aiohttp into sys.modules."""

    def setup_method(self) -> None:
        # Remove any cached aiohttp from the lazy-import module cache so the
        # function re-imports from sys.modules on each test call.
        sys.modules.pop("aiohttp", None)

    def teardown_method(self) -> None:
        sys.modules.pop("aiohttp", None)

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
        resp = _mock_response(200, records)
        session = _mock_session(resp)
        sys.modules["aiohttp"] = _make_fake_aiohttp(session)

        rate = await _make_provider().get_historical_beat_rate("AAPL")

        assert rate == pytest.approx(6 / 8)

    async def test_empty_response_returns_none(self) -> None:
        resp = _mock_response(200, [])
        session = _mock_session(resp)
        sys.modules["aiohttp"] = _make_fake_aiohttp(session)

        rate = await _make_provider().get_historical_beat_rate("AAPL")

        assert rate is None

    async def test_http_error_returns_none(self) -> None:
        resp = _mock_response(429, [])
        session = _mock_session(resp)
        sys.modules["aiohttp"] = _make_fake_aiohttp(session)

        rate = await _make_provider().get_historical_beat_rate("AAPL")

        assert rate is None

    async def test_network_exception_returns_none(self) -> None:
        session = MagicMock()
        session.__aenter__ = AsyncMock(side_effect=OSError("timeout"))
        session.__aexit__ = AsyncMock(return_value=False)
        sys.modules["aiohttp"] = _make_fake_aiohttp(session)

        rate = await _make_provider().get_historical_beat_rate("AAPL")

        assert rate is None

    async def test_ticker_uppercased_in_url(self) -> None:
        """Provider should uppercase the ticker in the URL."""
        records = [_make_record(2.0, 1.5)]
        resp = _mock_response(200, records)
        session = _mock_session(resp)
        sys.modules["aiohttp"] = _make_fake_aiohttp(session)

        await _make_provider().get_historical_beat_rate("aapl")

        url = session.get.call_args[0][0]
        assert "AAPL" in url

    async def test_lookback_reflected_in_url(self) -> None:
        records = [_make_record(2.0, 1.5)]
        resp = _mock_response(200, records)
        session = _mock_session(resp)
        sys.modules["aiohttp"] = _make_fake_aiohttp(session)

        await _make_provider().get_historical_beat_rate("MSFT", lookback=4)

        url = session.get.call_args[0][0]
        assert "limit=4" in url

    async def test_custom_base_url_used(self) -> None:
        records = [_make_record(2.0, 1.5)]
        resp = _mock_response(200, records)
        session = _mock_session(resp)
        sys.modules["aiohttp"] = _make_fake_aiohttp(session)

        provider = FMPEstimatesProvider(
            api_key="test-key", base_url="https://mock.fmp.local"
        )
        await provider.get_historical_beat_rate("AAPL")

        url = session.get.call_args[0][0]
        assert url.startswith("https://mock.fmp.local")
