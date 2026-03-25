"""Unit tests for EstimatesRenderer."""

from datetime import date

import pytest

from news_trade.models.surprise import EstimatesData
from news_trade.services.estimates_renderer import EstimatesRenderer


def _make_estimates(**kwargs) -> EstimatesData:
    defaults = dict(
        ticker="AAPL",
        fiscal_period="Q1 2026",
        report_date=date(2026, 1, 31),
        eps_estimate=2.00,
        eps_low=1.80,
        eps_high=2.20,
        eps_trailing_mean=1.90,
        revenue_estimate=1_500_000_000.0,
        revenue_low=1_400_000_000.0,
        revenue_high=1_600_000_000.0,
        historical_beat_rate=0.75,
        mean_eps_surprise=0.05,
        num_analysts=10,
    )
    return EstimatesData(**(defaults | kwargs))


@pytest.fixture()
def renderer() -> EstimatesRenderer:
    return EstimatesRenderer()


@pytest.fixture()
def data() -> EstimatesData:
    return _make_estimates()


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------


class TestRender:
    def test_render_contains_expected_sections(
        self, renderer: EstimatesRenderer, data: EstimatesData
    ):
        output = renderer.render("AAPL", data)
        assert "EPS consensus" in output
        assert "Revenue" in output
        assert "Historical beat rate" in output
        assert "Analyst coverage" in output
        assert "Estimate dispersion" in output

    def test_render_includes_ticker_header(
        self, renderer: EstimatesRenderer, data: EstimatesData
    ):
        output = renderer.render("AAPL", data)
        assert "=== EARNINGS ESTIMATES: AAPL ===" in output

    def test_render_na_for_none_trailing_mean(self, renderer: EstimatesRenderer):
        data = _make_estimates(eps_trailing_mean=None)
        output = renderer.render("AAPL", data)
        assert "N/A" in output

    def test_render_na_for_none_beat_rate(self, renderer: EstimatesRenderer):
        data = _make_estimates(historical_beat_rate=None)
        output = renderer.render("AAPL", data)
        assert "N/A" in output

    def test_render_na_for_none_mean_eps_surprise(self, renderer: EstimatesRenderer):
        data = _make_estimates(mean_eps_surprise=None)
        output = renderer.render("AAPL", data)
        assert "N/A" in output

    def test_render_is_deterministic(
        self, renderer: EstimatesRenderer, data: EstimatesData
    ):
        assert renderer.render("AAPL", data) == renderer.render("AAPL", data)

    def test_render_revenue_in_millions(self, renderer: EstimatesRenderer):
        data = _make_estimates(revenue_estimate=1_500_000_000.0)
        output = renderer.render("AAPL", data)
        assert "1500M" in output

    def test_render_report_date_present(
        self, renderer: EstimatesRenderer, data: EstimatesData
    ):
        output = renderer.render("AAPL", data)
        assert "2026-01-31" in output

    def test_render_fiscal_period_present(
        self, renderer: EstimatesRenderer, data: EstimatesData
    ):
        output = renderer.render("AAPL", data)
        assert "Q1 2026" in output

    def test_render_analyst_count(
        self, renderer: EstimatesRenderer, data: EstimatesData
    ):
        output = renderer.render("AAPL", data)
        assert "10 analysts" in output

    def test_render_different_ticker(
        self, renderer: EstimatesRenderer, data: EstimatesData
    ):
        output = renderer.render("MSFT", data)
        assert "=== EARNINGS ESTIMATES: MSFT ===" in output


# ---------------------------------------------------------------------------
# compute_pre_surprise_delta()
# ---------------------------------------------------------------------------


class TestComputePreSurpriseDelta:
    def test_uses_trailing_mean_when_available(self, renderer: EstimatesRenderer):
        # (3.0 - 2.0) / 2.0 = 0.50
        data = _make_estimates(eps_estimate=3.00, eps_trailing_mean=2.00)
        assert abs(renderer.compute_pre_surprise_delta(data) - 0.50) < 1e-9

    def test_negative_delta_when_below_trailing_mean(self, renderer: EstimatesRenderer):
        # (1.5 - 2.0) / 2.0 = -0.25
        data = _make_estimates(eps_estimate=1.50, eps_trailing_mean=2.00)
        assert abs(renderer.compute_pre_surprise_delta(data) - (-0.25)) < 1e-9

    def test_fallback_to_mean_surprise_when_no_trailing_mean(
        self, renderer: EstimatesRenderer
    ):
        data = _make_estimates(eps_trailing_mean=None, mean_eps_surprise=0.08)
        assert abs(renderer.compute_pre_surprise_delta(data) - 0.08) < 1e-9

    def test_returns_zero_when_no_history(self, renderer: EstimatesRenderer):
        data = _make_estimates(eps_trailing_mean=None, mean_eps_surprise=None)
        assert renderer.compute_pre_surprise_delta(data) == 0.0

    def test_clamped_to_plus_one(self, renderer: EstimatesRenderer):
        # (10.0 - 1.0) / 1.0 = 9.0 → clamped to 1.0
        data = _make_estimates(eps_estimate=10.00, eps_trailing_mean=1.00)
        assert renderer.compute_pre_surprise_delta(data) == 1.0

    def test_clamped_to_minus_one(self, renderer: EstimatesRenderer):
        # (-9.0 - 1.0) / 1.0 = -10.0 → clamped to -1.0
        data = _make_estimates(eps_estimate=-9.00, eps_trailing_mean=1.00)
        assert renderer.compute_pre_surprise_delta(data) == -1.0

    def test_trailing_mean_zero_falls_back_to_mean_surprise(
        self, renderer: EstimatesRenderer
    ):
        data = _make_estimates(eps_trailing_mean=0.0, mean_eps_surprise=0.03)
        # eps_trailing_mean is 0.0 → falls back to mean_eps_surprise
        assert abs(renderer.compute_pre_surprise_delta(data) - 0.03) < 1e-9

    def test_result_in_valid_range(self, renderer: EstimatesRenderer):
        data = _make_estimates(eps_estimate=2.00, eps_trailing_mean=1.80)
        delta = renderer.compute_pre_surprise_delta(data)
        assert -1.0 <= delta <= 1.0
