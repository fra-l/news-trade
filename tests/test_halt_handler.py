"""Unit tests for HaltHandlerAgent."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.agents.halt_handler import HaltHandlerAgent
from news_trade.config import Settings
from news_trade.models.portfolio import PortfolioState
from news_trade.models.positions import OpenStage1Position, Stage1Status
from news_trade.services.stage1_repository import Stage1Repository
from news_trade.services.tables import Base

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        anthropic_api_key="test-key",
        llm_provider="anthropic",
        llm_quick_model="claude-haiku-4-5-20251001",
        llm_deep_model="claude-sonnet-4-6",
    )
    return Settings(**(defaults | kwargs))


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_position(ticker: str = "AAPL") -> OpenStage1Position:
    return OpenStage1Position(
        id=str(uuid.uuid4()),
        ticker=ticker,
        direction="long",
        size_pct=0.33,
        entry_price=175.0,
        opened_at=datetime.utcnow() - timedelta(days=3),
        expected_report_date=date.today() + timedelta(days=2),
        fiscal_quarter="Q2 2026",
        historical_beat_rate=0.72,
    )


def _make_agent(
    alpaca_client: object = None,
    stage1_repo: object = None,
    **kwargs: object,
) -> HaltHandlerAgent:
    settings = _make_settings()
    event_bus = MagicMock()
    return HaltHandlerAgent(
        settings=settings,
        event_bus=event_bus,
        alpaca_client=alpaca_client,  # type: ignore[arg-type]
        stage1_repo=stage1_repo,  # type: ignore[arg-type]
    )


def _make_portfolio(**kwargs: object) -> PortfolioState:
    defaults: dict[str, object] = dict(equity=100_000.0, cash=50_000.0)
    return PortfolioState(**(defaults | kwargs))


# ---------------------------------------------------------------------------
# TestHaltHandlerNoBroker — None-safe when no client/repo injected
# ---------------------------------------------------------------------------


class TestHaltHandlerNoBroker:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    @pytest.mark.asyncio
    async def test_returns_empty_errors_when_no_deps(self) -> None:
        result = await self.agent.run({"system_halted": True})
        assert result == {"errors": []}

    @pytest.mark.asyncio
    async def test_does_not_raise_without_alpaca_client(self) -> None:
        # Must not raise even if Alpaca is unavailable
        await self.agent.run({})

    @pytest.mark.asyncio
    async def test_does_not_raise_without_stage1_repo(self) -> None:
        await self.agent.run({"portfolio": _make_portfolio()})

    @pytest.mark.asyncio
    async def test_preserves_existing_errors_from_state(self) -> None:
        result = await self.agent.run({"errors": ["prior-error"]})
        assert "prior-error" in result["errors"]


# ---------------------------------------------------------------------------
# TestHaltHandlerCancelOrders
# ---------------------------------------------------------------------------


class TestHaltHandlerCancelOrders:
    def setup_method(self) -> None:
        self.mock_alpaca = MagicMock()
        self.agent = _make_agent(alpaca_client=self.mock_alpaca)

    @pytest.mark.asyncio
    async def test_cancel_orders_called_once(self) -> None:
        with patch("news_trade.agents.halt_handler.asyncio.to_thread") as mock_thread:
            mock_thread.return_value = None
            await self.agent.run({"system_halted": True})
        # First call should be cancel_orders
        first_call_fn = mock_thread.call_args_list[0][0][0]
        assert first_call_fn == self.mock_alpaca.cancel_orders

    @pytest.mark.asyncio
    async def test_cancel_orders_error_accumulated_not_raised(self) -> None:
        self.mock_alpaca.cancel_orders.side_effect = RuntimeError("broker down")
        with patch(
            "news_trade.agents.halt_handler.asyncio.to_thread",
            side_effect=RuntimeError("broker down"),
        ):
            result = await self.agent.run({"system_halted": True})
        assert any("halt_cancel_orders" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# TestHaltHandlerClosePositions
# ---------------------------------------------------------------------------


class TestHaltHandlerClosePositions:
    def setup_method(self) -> None:
        self.mock_alpaca = MagicMock()
        self.agent = _make_agent(alpaca_client=self.mock_alpaca)

    @pytest.mark.asyncio
    async def test_close_all_positions_called_with_cancel_orders(self) -> None:
        with patch("news_trade.agents.halt_handler.asyncio.to_thread") as mock_thread:
            mock_thread.return_value = None
            await self.agent.run({"system_halted": True})
        # Second call should be close_all_positions with cancel_orders=True
        second_call = mock_thread.call_args_list[1]
        assert second_call[0][0] == self.mock_alpaca.close_all_positions
        assert second_call[1].get("cancel_orders") is True

    @pytest.mark.asyncio
    async def test_close_positions_error_accumulated(self) -> None:
        call_count = 0

        async def _side_effect(fn, **kw):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("close failed")

        with patch(
            "news_trade.agents.halt_handler.asyncio.to_thread",
            side_effect=_side_effect,
        ):
            result = await self.agent.run({"system_halted": True})
        assert any("halt_close_positions" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_close_positions_runs_even_if_cancel_orders_fails(self) -> None:
        """close_all_positions is still attempted when cancel_orders fails."""
        call_count = 0

        async def _side_effect(fn, **kw):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("cancel failed")

        with patch(
            "news_trade.agents.halt_handler.asyncio.to_thread",
            side_effect=_side_effect,
        ):
            result = await self.agent.run({"system_halted": True})
        # cancel_orders failed (call 1), close_all_positions still attempted (call 2)
        assert call_count == 2
        assert any("halt_cancel_orders" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# TestHaltHandlerStage1Expiry
# ---------------------------------------------------------------------------


class TestHaltHandlerStage1Expiry:
    def setup_method(self) -> None:
        self.pos1 = _make_position("AAPL")
        self.pos2 = _make_position("MSFT")
        self.mock_repo = MagicMock(spec=Stage1Repository)
        self.mock_repo.load_all_open.return_value = [self.pos1, self.pos2]
        self.agent = _make_agent(stage1_repo=self.mock_repo)

    @pytest.mark.asyncio
    async def test_load_all_open_called(self) -> None:
        await self.agent.run({})
        self.mock_repo.load_all_open.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_status_called_for_each_open_position(self) -> None:
        await self.agent.run({})
        assert self.mock_repo.update_status.call_count == 2
        self.mock_repo.update_status.assert_any_call(self.pos1.id, Stage1Status.EXPIRED)
        self.mock_repo.update_status.assert_any_call(self.pos2.id, Stage1Status.EXPIRED)

    @pytest.mark.asyncio
    async def test_no_update_calls_when_no_open_positions(self) -> None:
        self.mock_repo.load_all_open.return_value = []
        await self.agent.run({})
        self.mock_repo.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_stage1_error_accumulated_not_raised(self) -> None:
        self.mock_repo.load_all_open.side_effect = RuntimeError("db down")
        result = await self.agent.run({})
        assert any("halt_expire_stage1" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# TestHaltHandlerStatePassthrough
# ---------------------------------------------------------------------------


class TestHaltHandlerStatePassthrough:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    @pytest.mark.asyncio
    async def test_result_only_contains_errors_key(self) -> None:
        state = {
            "trade_signals": ["sig"],
            "approved_signals": [],
            "orders": [],
            "system_halted": True,
        }
        result = await self.agent.run(state)
        assert set(result.keys()) == {"errors"}

    @pytest.mark.asyncio
    async def test_does_not_set_system_halted_false(self) -> None:
        # HaltHandlerAgent must not clear the halt flag
        result = await self.agent.run({"system_halted": True})
        assert "system_halted" not in result


# ---------------------------------------------------------------------------
# TestHaltHandlerWithRealRepo — integration with in-memory SQLite
# ---------------------------------------------------------------------------


class TestHaltHandlerWithRealRepo:
    """Integration-style: real Stage1Repository + in-memory SQLite."""

    def setup_method(self) -> None:
        session = _make_session()
        self.repo = Stage1Repository(session)
        self.pos = _make_position("AAPL")
        self.repo.persist(self.pos)
        self.agent = _make_agent(stage1_repo=self.repo)

    @pytest.mark.asyncio
    async def test_open_position_is_marked_expired(self) -> None:
        await self.agent.run({"system_halted": True})
        # load_open returns None for non-OPEN positions
        assert self.repo.load_open("AAPL") is None

    @pytest.mark.asyncio
    async def test_returns_no_errors_on_success(self) -> None:
        result = await self.agent.run({"system_halted": True})
        assert result["errors"] == []
