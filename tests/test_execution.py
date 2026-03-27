"""Tests for ExecutionAgent and its helper functions."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.agents.execution import ExecutionAgent, _signal_to_order_side
from news_trade.config import Settings
from news_trade.models.orders import Order, OrderSide, OrderStatus
from news_trade.models.portfolio import PortfolioState, Position
from news_trade.models.signals import SignalDirection, TradeSignal
from news_trade.services.tables import Base, OrderRow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    defaults: dict[str, object] = dict(
        anthropic_api_key="test",
        alpaca_api_key="test",
        alpaca_secret_key="test",
    )
    return Settings(**(defaults | kwargs))


def _make_agent(**kwargs) -> ExecutionAgent:
    settings = kwargs.pop("settings", _make_settings())
    return ExecutionAgent(settings, MagicMock(), **kwargs)


def _make_signal(**kwargs) -> TradeSignal:
    defaults: dict[str, object] = dict(
        signal_id="sig-1",
        event_id="ev-1",
        ticker="AAPL",
        direction=SignalDirection.LONG,
        conviction=0.75,
        suggested_qty=10,
        entry_price=100.0,
        passed_confidence_gate=True,
    )
    return TradeSignal(**(defaults | kwargs))


def _make_portfolio(**kwargs) -> PortfolioState:
    defaults: dict[str, object] = dict(
        equity=100_000.0,
        cash=50_000.0,
        positions=[],
    )
    return PortfolioState(**(defaults | kwargs))


def _make_position(**kwargs) -> Position:
    defaults: dict[str, object] = dict(
        ticker="AAPL",
        qty=100,
        avg_entry_price=100.0,
    )
    return Position(**(defaults | kwargs))


def _make_order(**kwargs) -> Order:
    defaults: dict[str, object] = dict(
        order_id="ord-1",
        signal_id="sig-1",
        ticker="AAPL",
        side=OrderSide.BUY,
        qty=10,
        status=OrderStatus.PENDING,
    )
    return Order(**(defaults | kwargs))


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# ---------------------------------------------------------------------------
# TestSignalToOrderSide
# ---------------------------------------------------------------------------


class TestSignalToOrderSide:
    def test_long_maps_to_buy(self) -> None:
        signal = _make_signal(direction=SignalDirection.LONG)
        assert _signal_to_order_side(signal, None) == OrderSide.BUY

    def test_short_maps_to_sell(self) -> None:
        signal = _make_signal(direction=SignalDirection.SHORT)
        assert _signal_to_order_side(signal, None) == OrderSide.SELL

    def test_close_long_position_maps_to_sell(self) -> None:
        signal = _make_signal(direction=SignalDirection.CLOSE)
        portfolio = _make_portfolio(positions=[_make_position(ticker="AAPL", qty=100)])
        assert _signal_to_order_side(signal, portfolio) == OrderSide.SELL

    def test_close_short_position_maps_to_buy(self) -> None:
        signal = _make_signal(direction=SignalDirection.CLOSE)
        portfolio = _make_portfolio(positions=[_make_position(ticker="AAPL", qty=-50)])
        assert _signal_to_order_side(signal, portfolio) == OrderSide.BUY

    def test_close_no_portfolio_defaults_to_sell(self) -> None:
        signal = _make_signal(direction=SignalDirection.CLOSE)
        assert _signal_to_order_side(signal, None) == OrderSide.SELL

    def test_close_no_position_defaults_to_sell(self) -> None:
        signal = _make_signal(direction=SignalDirection.CLOSE, ticker="MSFT")
        portfolio = _make_portfolio(positions=[_make_position(ticker="AAPL", qty=100)])
        assert _signal_to_order_side(signal, portfolio) == OrderSide.SELL


# ---------------------------------------------------------------------------
# TestSubmitOrderNoBroker
# ---------------------------------------------------------------------------


class TestSubmitOrderNoBroker:
    def setup_method(self) -> None:
        self.agent = _make_agent(alpaca_client=None)

    async def test_returns_pending_order_without_broker(self) -> None:
        order = await self.agent._submit_order(_make_signal(), None)
        assert order.status == OrderStatus.PENDING

    async def test_order_id_is_unique(self) -> None:
        order1 = await self.agent._submit_order(_make_signal(), None)
        order2 = await self.agent._submit_order(_make_signal(), None)
        assert order1.order_id != order2.order_id

    async def test_ticker_and_side_correct(self) -> None:
        signal = _make_signal(direction=SignalDirection.SHORT, ticker="TSLA")
        order = await self.agent._submit_order(signal, None)
        assert order.ticker == "TSLA"
        assert order.side == OrderSide.SELL


# ---------------------------------------------------------------------------
# TestSubmitOrderWithBroker
# ---------------------------------------------------------------------------


class TestSubmitOrderWithBroker:
    def _fake_alpaca_order(self) -> MagicMock:
        fake = MagicMock()
        fake.id = "broker-123"
        fake.symbol = "AAPL"
        fake.side.value = "buy"
        fake.qty = 10
        fake.status.value = "accepted"
        fake.filled_qty = 0
        fake.filled_avg_price = None
        fake.submitted_at = None
        fake.filled_at = None
        return fake

    async def test_calls_alpaca_submit_order(self) -> None:
        mock_client = MagicMock()
        mock_client.submit_order.return_value = self._fake_alpaca_order()
        agent = _make_agent(alpaca_client=mock_client)
        await agent._submit_order(_make_signal(), None)
        mock_client.submit_order.assert_called_once()

    async def test_broker_order_id_set(self) -> None:
        mock_client = MagicMock()
        mock_client.submit_order.return_value = self._fake_alpaca_order()
        agent = _make_agent(alpaca_client=mock_client)
        order = await agent._submit_order(_make_signal(), None)
        assert order.broker_order_id == "broker-123"

    async def test_submit_order_uses_asyncio_to_thread(self) -> None:
        mock_client = MagicMock()
        mock_client.submit_order.return_value = self._fake_alpaca_order()
        agent = _make_agent(alpaca_client=mock_client)
        with patch("news_trade.agents.execution.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.return_value = self._fake_alpaca_order()
            await agent._submit_order(_make_signal(), None)
            mock_to_thread.assert_called_once()


# ---------------------------------------------------------------------------
# TestLogOrder
# ---------------------------------------------------------------------------


class TestLogOrder:
    def setup_method(self) -> None:
        self._session = _make_session()
        self.agent = _make_agent(session=self._session)

    def test_persists_new_order(self) -> None:
        self.agent._log_order(_make_order())
        row = self._session.query(OrderRow).filter_by(order_id="ord-1").first()
        assert row is not None
        assert row.ticker == "AAPL"

    def test_upsert_updates_status(self) -> None:
        order = _make_order()
        self.agent._log_order(order)
        updated = order.model_copy(
            update={"status": OrderStatus.FILLED, "filled_qty": 10}
        )
        self.agent._log_order(updated)
        rows = self._session.query(OrderRow).filter_by(order_id="ord-1").all()
        assert len(rows) == 1
        assert rows[0].status == "filled"
        assert rows[0].filled_qty == 10

    def test_no_session_does_not_raise(self) -> None:
        agent = _make_agent(session=None)
        agent._log_order(_make_order())  # must not raise

    def test_close_after_date_persisted(self) -> None:
        tomorrow = date.today() + timedelta(days=1)
        self.agent._log_order(_make_order(), close_after_date=tomorrow)
        row = self._session.query(OrderRow).filter_by(order_id="ord-1").first()
        assert row is not None
        assert row.close_after_date == tomorrow

    def test_close_after_date_none_by_default(self) -> None:
        self.agent._log_order(_make_order())
        row = self._session.query(OrderRow).filter_by(order_id="ord-1").first()
        assert row is not None
        assert row.close_after_date is None

    def test_upsert_preserves_close_after_date(self) -> None:
        tomorrow = date.today() + timedelta(days=1)
        order = _make_order()
        self.agent._log_order(order, close_after_date=tomorrow)
        # upsert without close_after_date should NOT overwrite the existing value
        updated = order.model_copy(update={"status": OrderStatus.FILLED})
        self.agent._log_order(updated)
        row = self._session.query(OrderRow).filter_by(order_id="ord-1").first()
        assert row is not None
        assert row.close_after_date == tomorrow


# ---------------------------------------------------------------------------
# TestRunIntegration
# ---------------------------------------------------------------------------


class TestRunIntegration:
    def setup_method(self) -> None:
        self.agent = _make_agent(alpaca_client=None, session=None)

    async def test_returns_orders_list(self) -> None:
        signals = [
            _make_signal(signal_id="s1", ticker="AAPL"),
            _make_signal(signal_id="s2", ticker="MSFT"),
        ]
        result = await self.agent.run({"approved_signals": signals})
        assert len(result["orders"]) == 2

    async def test_errors_accumulate_on_failure(self) -> None:
        async def _raise(*_a, **_kw) -> Order:
            raise RuntimeError("broker down")

        self.agent._submit_order = _raise  # type: ignore[method-assign]
        result = await self.agent.run({"approved_signals": [_make_signal()]})
        assert result["orders"] == []
        assert any("execution:sig-1" in e for e in result["errors"])

    async def test_empty_approved_signals(self) -> None:
        result = await self.agent.run({"approved_signals": []})
        assert result["orders"] == []

    async def test_existing_errors_preserved(self) -> None:
        result = await self.agent.run(
            {"approved_signals": [], "errors": ["prior-error"]}
        )
        assert "prior-error" in result["errors"]


# ---------------------------------------------------------------------------
# TestScanExpiredPead
# ---------------------------------------------------------------------------


def _make_order_row(
    order_id: str = "ord-1",
    ticker: str = "AAPL",
    status: str = "filled",
    close_after_date: date | None = None,
    session: Session | None = None,
) -> OrderRow:
    row = OrderRow(
        order_id=order_id,
        signal_id="sig-1",
        ticker=ticker,
        side="buy",
        order_type="market",
        qty=10,
        status=status,
        filled_qty=10,
        close_after_date=close_after_date,
    )
    if session is not None:
        session.add(row)
        session.commit()
    return row


class TestScanExpiredPead:
    def setup_method(self) -> None:
        self._session = _make_session()
        self._mock_alpaca = MagicMock()
        self.agent = _make_agent(
            alpaca_client=self._mock_alpaca, session=self._session
        )

    async def test_no_op_when_no_expired_rows(self) -> None:
        result = await self.agent.scan_expired_pead({})
        assert result == {"errors": []}
        self._mock_alpaca.close_position.assert_not_called()

    async def test_no_op_when_no_alpaca_client(self) -> None:
        agent = _make_agent(alpaca_client=None, session=self._session)
        _make_order_row(
            close_after_date=date.today() - timedelta(days=1),
            session=self._session,
        )
        result = await agent.scan_expired_pead({})
        assert result == {"errors": []}

    async def test_closes_expired_position(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        _make_order_row(close_after_date=yesterday, session=self._session)
        result = await self.agent.scan_expired_pead({})
        assert result["errors"] == []
        self._mock_alpaca.close_position.assert_called_once_with("AAPL")
        row = self._session.query(OrderRow).filter_by(order_id="ord-1").first()
        assert row is not None
        assert row.status == "pead_closed"

    async def test_skips_future_rows(self) -> None:
        tomorrow = date.today() + timedelta(days=1)
        _make_order_row(close_after_date=tomorrow, session=self._session)
        result = await self.agent.scan_expired_pead({})
        assert result["errors"] == []
        self._mock_alpaca.close_position.assert_not_called()

    async def test_skips_rows_without_close_after_date(self) -> None:
        _make_order_row(close_after_date=None, session=self._session)
        await self.agent.scan_expired_pead({})
        self._mock_alpaca.close_position.assert_not_called()

    async def test_skips_already_closed_rows(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        _make_order_row(
            close_after_date=yesterday, status="pead_closed", session=self._session
        )
        await self.agent.scan_expired_pead({})
        self._mock_alpaca.close_position.assert_not_called()

    async def test_alpaca_error_logged_and_returned(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        _make_order_row(close_after_date=yesterday, session=self._session)
        self._mock_alpaca.close_position.side_effect = RuntimeError("api error")
        result = await self.agent.scan_expired_pead({})
        assert any("pead_close:ord-1" in e for e in result["errors"])
        # status must not change on error
        row = self._session.query(OrderRow).filter_by(order_id="ord-1").first()
        assert row is not None
        assert row.status == "filled"

    async def test_closes_multiple_expired_positions(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        _make_order_row(
            order_id="ord-1", ticker="AAPL",
            close_after_date=yesterday, session=self._session,
        )
        _make_order_row(
            order_id="ord-2", ticker="MSFT",
            close_after_date=yesterday, session=self._session,
        )
        result = await self.agent.scan_expired_pead({})
        assert result["errors"] == []
        assert self._mock_alpaca.close_position.call_count == 2
