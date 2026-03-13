"""Unit tests for all Pydantic models."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from news_trade.models.events import EventType, NewsEvent
from news_trade.models.market import MarketSnapshot, OHLCVBar
from news_trade.models.orders import Order, OrderSide, OrderStatus, OrderType
from news_trade.models.portfolio import PortfolioState, Position
from news_trade.models.sentiment import SentimentLabel, SentimentResult
from news_trade.models.signals import SignalDirection, TradeSignal

NOW = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)


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
