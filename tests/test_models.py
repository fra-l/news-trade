"""Unit tests for all Pydantic models."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from news_trade.models.events import EventType, NewsEvent
from news_trade.models.market import MarketSnapshot, OHLCVBar
from news_trade.models.orders import Order, OrderSide, OrderStatus, OrderType
from news_trade.models.portfolio import PortfolioState, Position
from news_trade.models.sentiment import SentimentLabel, SentimentResult
from news_trade.models.signals import SignalDirection, TradeSignal
from news_trade.models.surprise import (
    EarningsSurprise,
    EstimatesData,
    MetricSurprise,
    SignalStrength,
    SurpriseDirection,
)

NOW = datetime(2026, 3, 2, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# SentimentResult
# ---------------------------------------------------------------------------


class TestSentimentResult:
    def _make(self, **kwargs) -> SentimentResult:
        defaults = dict(
            event_id="evt-1",
            ticker="AAPL",
            label=SentimentLabel.BULLISH,
            score=0.8,
            confidence=0.9,
        )
        return SentimentResult(**(defaults | kwargs))

    def test_happy_path(self):
        sr = self._make()
        assert sr.event_id == "evt-1"
        assert sr.ticker == "AAPL"
        assert sr.label == SentimentLabel.BULLISH
        assert sr.score == 0.8
        assert sr.confidence == 0.9

    def test_serialization_round_trip(self):
        sr = self._make()
        assert SentimentResult.model_validate(sr.model_dump()) == sr

    def test_optional_field_defaults(self):
        sr = self._make()
        assert sr.reasoning == ""
        assert sr.model_id == "claude-sonnet-4-6"
        assert isinstance(sr.analyzed_at, datetime)

    def test_invalid_label_raises(self):
        with pytest.raises(ValidationError):
            self._make(label="not_a_label")

    def test_score_above_max_raises(self):
        with pytest.raises(ValidationError):
            self._make(score=1.1)

    def test_score_below_min_raises(self):
        with pytest.raises(ValidationError):
            self._make(score=-1.1)

    def test_confidence_above_max_raises(self):
        with pytest.raises(ValidationError):
            self._make(confidence=1.1)

    def test_confidence_below_min_raises(self):
        with pytest.raises(ValidationError):
            self._make(confidence=-0.1)


# ---------------------------------------------------------------------------
# NewsEvent
# ---------------------------------------------------------------------------


class TestNewsEvent:
    def _make(self, **kwargs) -> NewsEvent:
        defaults = dict(
            event_id="evt-2",
            headline="AAPL beats earnings",
            source="benzinga",
            published_at=NOW,
        )
        return NewsEvent(**(defaults | kwargs))

    def test_happy_path(self):
        ev = self._make()
        assert ev.event_id == "evt-2"
        assert ev.headline == "AAPL beats earnings"
        assert ev.source == "benzinga"

    def test_serialization_round_trip(self):
        ev = self._make()
        assert NewsEvent.model_validate(ev.model_dump()) == ev

    def test_optional_field_defaults(self):
        ev = self._make()
        assert ev.summary == ""
        assert ev.url == ""
        assert ev.tickers == []
        assert ev.event_type == EventType.OTHER
        assert isinstance(ev.ingested_at, datetime)

    def test_invalid_event_type_raises(self):
        with pytest.raises(ValidationError):
            self._make(event_type="not_a_type")

    def test_explicit_tickers(self):
        ev = self._make(tickers=["AAPL", "MSFT"])
        assert ev.tickers == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


class TestOrder:
    def _make(self, **kwargs) -> Order:
        defaults = dict(
            order_id="ord-1",
            signal_id="sig-1",
            ticker="MSFT",
            side=OrderSide.BUY,
            qty=10,
        )
        return Order(**(defaults | kwargs))

    def test_happy_path(self):
        o = self._make()
        assert o.ticker == "MSFT"
        assert o.side == OrderSide.BUY
        assert o.qty == 10

    def test_serialization_round_trip(self):
        o = self._make()
        assert Order.model_validate(o.model_dump()) == o

    def test_optional_field_defaults(self):
        o = self._make()
        assert o.broker_order_id is None
        assert o.order_type == OrderType.MARKET
        assert o.status == OrderStatus.PENDING
        assert o.filled_qty == 0
        assert o.filled_avg_price is None

    def test_invalid_side_raises(self):
        with pytest.raises(ValidationError):
            self._make(side="sideways")

    def test_qty_below_min_raises(self):
        with pytest.raises(ValidationError):
            self._make(qty=0)

    def test_filled_qty_negative_raises(self):
        with pytest.raises(ValidationError):
            self._make(filled_qty=-1)

    def test_invalid_order_type_raises(self):
        with pytest.raises(ValidationError):
            self._make(order_type="twap")

    def test_invalid_status_raises(self):
        with pytest.raises(ValidationError):
            self._make(status="lost")


# ---------------------------------------------------------------------------
# TradeSignal
# ---------------------------------------------------------------------------


class TestTradeSignal:
    def _make(self, **kwargs) -> TradeSignal:
        defaults = dict(
            signal_id="sig-1",
            event_id="evt-1",
            ticker="NVDA",
            direction=SignalDirection.LONG,
            conviction=0.75,
            suggested_qty=50,
        )
        return TradeSignal(**(defaults | kwargs))

    def test_happy_path(self):
        ts = self._make()
        assert ts.ticker == "NVDA"
        assert ts.direction == SignalDirection.LONG
        assert ts.conviction == 0.75

    def test_serialization_round_trip(self):
        ts = self._make()
        assert TradeSignal.model_validate(ts.model_dump()) == ts

    def test_optional_field_defaults(self):
        ts = self._make()
        assert ts.entry_price is None
        assert ts.stop_loss is None
        assert ts.take_profit is None
        assert ts.rationale == ""

    def test_invalid_direction_raises(self):
        with pytest.raises(ValidationError):
            self._make(direction="sideways")

    def test_conviction_above_max_raises(self):
        with pytest.raises(ValidationError):
            self._make(conviction=1.1)

    def test_conviction_below_min_raises(self):
        with pytest.raises(ValidationError):
            self._make(conviction=-0.1)

    def test_suggested_qty_negative_raises(self):
        with pytest.raises(ValidationError):
            self._make(suggested_qty=-1)


# ---------------------------------------------------------------------------
# PortfolioState
# ---------------------------------------------------------------------------


class TestPortfolioState:
    def _make(self, **kwargs) -> PortfolioState:
        defaults = dict(equity=100_000.0, cash=50_000.0)
        return PortfolioState(**(defaults | kwargs))

    def test_happy_path(self):
        ps = self._make()
        assert ps.equity == 100_000.0
        assert ps.cash == 50_000.0

    def test_serialization_round_trip(self):
        ps = self._make()
        assert PortfolioState.model_validate(ps.model_dump()) == ps

    def test_optional_field_defaults(self):
        ps = self._make()
        assert ps.positions == []
        assert ps.buying_power == 0.0
        assert ps.daily_pnl == 0.0
        assert ps.max_drawdown_pct == 0.0

    def test_position_count_property_empty(self):
        ps = self._make()
        assert ps.position_count == 0

    def test_position_count_property_with_positions(self):
        pos = Position(ticker="AAPL", qty=10, avg_entry_price=150.0)
        ps = self._make(positions=[pos])
        assert ps.position_count == 1

    def test_get_position_found(self):
        pos = Position(ticker="AAPL", qty=10, avg_entry_price=150.0)
        ps = self._make(positions=[pos])
        result = ps.get_position("AAPL")
        assert result is not None
        assert result.ticker == "AAPL"

    def test_get_position_not_found(self):
        ps = self._make()
        assert ps.get_position("TSLA") is None

    def test_get_position_returns_correct_ticker(self):
        pos_aapl = Position(ticker="AAPL", qty=10, avg_entry_price=150.0)
        pos_msft = Position(ticker="MSFT", qty=5, avg_entry_price=300.0)
        ps = self._make(positions=[pos_aapl, pos_msft])
        assert ps.get_position("MSFT").ticker == "MSFT"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# MarketSnapshot / OHLCVBar
# ---------------------------------------------------------------------------


class TestMarketSnapshot:
    def _make_bar(self, **kwargs) -> OHLCVBar:
        defaults = dict(
            timestamp=NOW,
            open=100.0,
            high=105.0,
            low=99.0,
            close=104.0,
            volume=1_000_000,
            vwap=102.5,
        )
        return OHLCVBar(**(defaults | kwargs))

    def _make(self, **kwargs) -> MarketSnapshot:
        defaults = dict(
            ticker="TSLA",
            latest_close=250.0,
            volume=5_000_000,
            vwap=248.0,
            volatility_20d=0.45,
            fetched_at=NOW,
        )
        return MarketSnapshot(**(defaults | kwargs))

    def test_ohlcv_bar_happy_path(self):
        bar = self._make_bar()
        assert bar.close == 104.0
        assert bar.volume == 1_000_000

    def test_market_snapshot_happy_path(self):
        ms = self._make()
        assert ms.ticker == "TSLA"
        assert ms.latest_close == 250.0

    def test_serialization_round_trip(self):
        bar = self._make_bar()
        ms = self._make(bars=[bar])
        assert MarketSnapshot.model_validate(ms.model_dump()) == ms

    def test_optional_field_defaults(self):
        ms = self._make()
        assert ms.bars == []
        assert ms.atr_14d is None
        assert ms.relative_volume is None

    def test_optional_metrics_accepted(self):
        ms = self._make(atr_14d=3.5, relative_volume=1.8)
        assert ms.atr_14d == 3.5
        assert ms.relative_volume == 1.8

    def test_nested_bar_serialization(self):
        bar = self._make_bar()
        ms = self._make(bars=[bar])
        dumped = ms.model_dump()
        assert len(dumped["bars"]) == 1
        assert dumped["bars"][0]["close"] == 104.0

    def test_multiple_bars(self):
        bars = [self._make_bar(close=float(100 + i)) for i in range(5)]
        ms = self._make(bars=bars)
        assert len(ms.bars) == 5


# ---------------------------------------------------------------------------
# TradeSignal — Pattern C confidence fields
# ---------------------------------------------------------------------------


class TestTradeSignalConfidenceFields:
    def _make(self, **kwargs) -> TradeSignal:
        defaults = dict(
            signal_id="sig-99",
            event_id="evt-99",
            ticker="AAPL",
            direction=SignalDirection.LONG,
            conviction=0.70,
            suggested_qty=10,
        )
        return TradeSignal(**(defaults | kwargs))

    def test_new_optional_fields_default_none(self):
        ts = self._make()
        assert ts.signal_strength is None
        assert ts.confidence_score is None
        assert ts.rejection_reason is None

    def test_passed_confidence_gate_defaults_false(self):
        ts = self._make()
        assert ts.passed_confidence_gate is False

    def test_confidence_score_accepted_in_range(self):
        ts = self._make(confidence_score=0.75, passed_confidence_gate=True)
        assert ts.confidence_score == 0.75
        assert ts.passed_confidence_gate is True

    def test_confidence_score_above_max_raises(self):
        with pytest.raises(ValidationError):
            self._make(confidence_score=1.1)

    def test_confidence_score_below_min_raises(self):
        with pytest.raises(ValidationError):
            self._make(confidence_score=-0.1)

    def test_signal_strength_field_accepted(self):
        ts = self._make(signal_strength=SignalStrength.STRONG)
        assert ts.signal_strength == SignalStrength.STRONG

    def test_rejection_reason_field_accepted(self):
        ts = self._make(rejection_reason="confidence 0.40 below gate 0.55")
        assert ts.rejection_reason == "confidence 0.40 below gate 0.55"

    def test_serialization_round_trip_with_confidence_fields(self):
        ts = self._make(
            signal_strength=SignalStrength.MODERATE,
            confidence_score=0.65,
            passed_confidence_gate=True,
        )
        assert TradeSignal.model_validate(ts.model_dump()) == ts


# ---------------------------------------------------------------------------
# MetricSurprise
# ---------------------------------------------------------------------------


class TestMetricSurprise:
    def _make(self, **kwargs) -> MetricSurprise:
        defaults = dict(
            actual=2.10,
            consensus=2.00,
            estimate_high=2.20,
            estimate_low=1.80,
            analyst_count=10,
        )
        return MetricSurprise(**(defaults | kwargs))

    def test_beat_direction(self):
        ms = self._make()
        assert ms.direction == SurpriseDirection.BEAT

    def test_miss_direction(self):
        # actual < consensus by > 2%
        ms = self._make(actual=1.90, consensus=2.00)
        assert ms.direction == SurpriseDirection.MISS

    def test_in_line_direction(self):
        ms = self._make(actual=2.01, consensus=2.00)
        assert ms.direction == SurpriseDirection.IN_LINE

    def test_pct_surprise_formula(self):
        ms = self._make(actual=2.10, consensus=2.00)
        expected = ((2.10 - 2.00) / abs(2.00)) * 100.0
        assert abs(ms.pct_surprise - expected) < 1e-9

    def test_sigma_surprise_formula(self):
        ms = self._make(
            actual=2.10, consensus=2.00, estimate_high=2.20, estimate_low=1.80
        )
        std = (2.20 - 1.80) / 4.0  # = 0.10
        expected = (2.10 - 2.00) / std  # = 1.0
        assert abs(ms.sigma_surprise - expected) < 1e-9

    def test_sigma_surprise_zero_when_std_is_zero(self):
        ms = self._make(estimate_high=2.00, estimate_low=2.00)
        assert ms.sigma_surprise == 0.0

    def test_pct_surprise_zero_when_consensus_zero(self):
        ms = self._make(actual=1.0, consensus=0.0)
        assert ms.pct_surprise == 0.0

    def test_analyst_count_below_zero_raises(self):
        with pytest.raises(ValidationError):
            self._make(analyst_count=-1)

    def test_frozen_model_immutable(self):
        ms = self._make()
        with pytest.raises((TypeError, ValidationError)):
            ms.actual = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EarningsSurprise
# ---------------------------------------------------------------------------


class TestEarningsSurprise:
    def _make_metric(
        self, actual: float = 2.10, consensus: float = 2.00
    ) -> MetricSurprise:
        return MetricSurprise(
            actual=actual,
            consensus=consensus,
            estimate_high=2.20,
            estimate_low=1.80,
            analyst_count=10,
        )

    def _make(self, **kwargs) -> EarningsSurprise:
        defaults = dict(
            ticker="AAPL",
            report_date=date(2026, 1, 31),
            fiscal_quarter="Q1 2026",
            eps=self._make_metric(actual=2.10, consensus=2.00),
            revenue=self._make_metric(actual=1.05e9, consensus=1.00e9),
        )
        return EarningsSurprise(**(defaults | kwargs))

    def test_signal_strength_strong(self):
        # Need composite_surprise > 10 and composite_confidence > 0.7
        # With eps actual=4.0, consensus=2.0 → pct=100% → large composite
        big_beat = MetricSurprise(
            actual=4.0, consensus=2.0,
            estimate_high=2.5, estimate_low=1.5,
            analyst_count=15,
        )
        es = EarningsSurprise(
            ticker="AAPL",
            report_date=date(2026, 1, 31),
            fiscal_quarter="Q1 2026",
            eps=big_beat,
            revenue=big_beat,
        )
        assert es.signal_strength == SignalStrength.STRONG

    def test_signal_strength_none_when_low_surprise(self):
        # actual ≈ consensus → small pct surprise
        es = self._make(
            eps=self._make_metric(actual=2.01, consensus=2.00),
            revenue=self._make_metric(actual=1.001e9, consensus=1.00e9),
        )
        assert es.signal_strength == SignalStrength.NONE

    def test_composite_surprise_includes_guidance(self):
        es_no_guidance = self._make()
        es_with_guidance = self._make(guidance_sentiment=1.0)
        assert es_with_guidance.composite_surprise > es_no_guidance.composite_surprise

    def test_serialization_round_trip(self):
        es = self._make()
        assert EarningsSurprise.model_validate(es.model_dump()) == es


# ---------------------------------------------------------------------------
# EstimatesData
# ---------------------------------------------------------------------------


class TestEstimatesData:
    def _make(self, **kwargs) -> EstimatesData:
        defaults = dict(
            ticker="MSFT",
            fiscal_period="Q2 2026",
            report_date=date(2026, 4, 25),
            eps_estimate=3.00,
            eps_low=2.80,
            eps_high=3.20,
            revenue_estimate=65_000_000_000.0,
            revenue_low=63_000_000_000.0,
            revenue_high=67_000_000_000.0,
            num_analysts=20,
        )
        return EstimatesData(**(defaults | kwargs))

    def test_estimate_dispersion_computed(self):
        ed = self._make(eps_estimate=2.00, eps_low=1.80, eps_high=2.20)
        expected = (2.20 - 1.80) / (4.0 * abs(2.00))  # 0.40 / 8.0 = 0.05
        assert abs(ed.estimate_dispersion - expected) < 1e-9

    def test_estimate_dispersion_zero_when_eps_estimate_zero(self):
        ed = self._make(eps_estimate=0.0, eps_low=-0.1, eps_high=0.1)
        assert ed.estimate_dispersion == 0.0

    def test_optional_fields_default_none(self):
        ed = self._make()
        assert ed.eps_trailing_mean is None
        assert ed.historical_beat_rate is None
        assert ed.mean_eps_surprise is None

    def test_revenue_below_zero_raises(self):
        with pytest.raises(ValidationError):
            self._make(revenue_estimate=-1.0)
