"""Unit and integration tests for RiskManagerAgent.

Uses in-memory SQLite for Stage1Repository so no external DB is needed.
All async tests rely on asyncio_mode = "auto" from pyproject.toml.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.agents.risk_manager import RiskManagerAgent
from news_trade.config import Settings
from news_trade.models.portfolio import PortfolioState, Position
from news_trade.models.signals import SignalDirection, TradeSignal
from news_trade.services.event_bus import EventBus
from news_trade.services.stage1_repository import Stage1Repository
from news_trade.services.tables import Base

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_settings(**kwargs) -> Settings:
    defaults: dict = dict(
        max_drawdown_pct=0.10,
        max_open_positions=5,
        max_position_pct=0.15,
        risk_dry_run=False,
    )
    return Settings(**(defaults | kwargs))  # type: ignore[call-arg]


def _make_signal(**kwargs) -> TradeSignal:
    defaults: dict[str, object] = dict(
        signal_id=str(uuid.uuid4()),
        event_id="evt-1",
        ticker="AAPL",
        direction=SignalDirection.LONG,
        conviction=0.80,
        suggested_qty=10,
        passed_confidence_gate=True,
    )
    return TradeSignal(**(defaults | kwargs))


def _make_portfolio(**kwargs) -> PortfolioState:
    defaults: dict[str, object] = dict(
        equity=100_000.0,
        cash=50_000.0,
        max_drawdown_pct=0.00,
        positions=[],
    )
    return PortfolioState(**(defaults | kwargs))


def _make_position_obj(ticker: str = "MSFT", qty: int = 100) -> Position:
    return Position(ticker=ticker, qty=qty, avg_entry_price=300.0, current_price=305.0)


def _make_event_bus() -> EventBus:
    bus = MagicMock(spec=EventBus)
    bus.publish = AsyncMock()
    return bus


def _make_agent(
    settings: Settings | None = None, session: Session | None = None
) -> RiskManagerAgent:
    if settings is None:
        settings = _make_settings()
    if session is None:
        session = _make_session()
    repo = Stage1Repository(session)
    bus = _make_event_bus()
    return RiskManagerAgent(settings=settings, event_bus=bus, stage1_repo=repo)


# ---------------------------------------------------------------------------
# Layer 1 — Confidence gate
# ---------------------------------------------------------------------------


class TestConfidenceGateLayer:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    def test_rejects_signal_with_gate_false(self) -> None:
        signal = _make_signal(
            passed_confidence_gate=False, rejection_reason="score too low"
        )
        passed, reason, _ = self.agent._evaluate(signal, _make_portfolio(), 0, [])
        assert not passed
        assert "score too low" in (reason or "")

    def test_rejects_signal_with_gate_false_default_reason(self) -> None:
        signal = _make_signal(passed_confidence_gate=False, rejection_reason=None)
        passed, reason, _ = self.agent._evaluate(signal, _make_portfolio(), 0, [])
        assert not passed
        assert reason is not None
        assert "confidence gate" in reason.lower()

    def test_accepts_signal_with_gate_true(self) -> None:
        signal = _make_signal(passed_confidence_gate=True)
        passed, reason, _ = self.agent._evaluate(signal, _make_portfolio(), 0, [])
        assert passed
        assert reason is None


# ---------------------------------------------------------------------------
# Layer 2a — Drawdown halt
# ---------------------------------------------------------------------------


class TestCheckDrawdown:
    def setup_method(self) -> None:
        self.agent = _make_agent(settings=_make_settings(max_drawdown_pct=0.10))

    def test_rejects_when_drawdown_exceeds_limit(self) -> None:
        portfolio = _make_portfolio(max_drawdown_pct=0.12)
        signal = _make_signal(direction=SignalDirection.LONG)
        passed, reason, _ = self.agent._evaluate(signal, portfolio, 0, [])
        assert not passed
        assert "drawdown" in (reason or "").lower()

    def test_rejects_at_exact_limit(self) -> None:
        portfolio = _make_portfolio(max_drawdown_pct=0.10)
        signal = _make_signal(direction=SignalDirection.LONG)
        passed, _, _ = self.agent._evaluate(signal, portfolio, 0, [])
        assert not passed

    def test_accepts_when_drawdown_within_limit(self) -> None:
        portfolio = _make_portfolio(max_drawdown_pct=0.05)
        signal = _make_signal(direction=SignalDirection.LONG)
        passed, _, _ = self.agent._evaluate(signal, portfolio, 0, [])
        assert passed

    def test_exit_bypasses_drawdown_check(self) -> None:
        """EXIT (CLOSE) signals bypass the drawdown halt."""
        portfolio = _make_portfolio(max_drawdown_pct=0.99)
        signal = _make_signal(direction=SignalDirection.CLOSE)
        passed, _, _ = self.agent._evaluate(signal, portfolio, 0, [])
        assert passed


# ---------------------------------------------------------------------------
# Layer 2b — Concentration limit
# ---------------------------------------------------------------------------


class TestCheckMaxPositions:
    def setup_method(self) -> None:
        self.agent = _make_agent(settings=_make_settings(max_open_positions=3))

    def test_rejects_new_position_when_max_reached(self) -> None:
        signal = _make_signal(direction=SignalDirection.LONG)
        passed, reason, _ = self.agent._evaluate(
            signal, _make_portfolio(), open_count=3, approved_so_far=[]
        )
        assert not passed
        assert "open positions" in (reason or "").lower()

    def test_accepts_new_position_below_max(self) -> None:
        signal = _make_signal(direction=SignalDirection.LONG)
        passed, _, _ = self.agent._evaluate(
            signal, _make_portfolio(), open_count=2, approved_so_far=[]
        )
        assert passed

    def test_exit_bypasses_concentration_check(self) -> None:
        signal = _make_signal(direction=SignalDirection.CLOSE)
        passed, _, _ = self.agent._evaluate(
            signal, _make_portfolio(), open_count=100, approved_so_far=[]
        )
        assert passed

    def test_stage2_add_bypasses_concentration_check(self) -> None:
        """Stage 2 ADD signals (stage1_id set) are exempt from max-positions check."""
        signal = _make_signal(
            direction=SignalDirection.LONG, stage1_id="some-stage1-uuid"
        )
        passed, _, _ = self.agent._evaluate(
            signal, _make_portfolio(), open_count=3, approved_so_far=[]
        )
        assert passed

    def test_stage2_add_exempt_at_limit_via_run(self) -> None:
        """Integration: run() approves Stage 2 ADD signal even when at max positions."""
        agent = _make_agent(settings=_make_settings(max_open_positions=0))
        signal = _make_signal(
            passed_confidence_gate=True,
            direction=SignalDirection.LONG,
            stage1_id="some-stage1-uuid",
        )
        import asyncio

        state = {"trade_signals": [signal], "portfolio": _make_portfolio()}
        result = asyncio.get_event_loop().run_until_complete(agent.run(state))
        assert len(result["approved_signals"]) == 1
        assert len(result["rejected_signals"]) == 0


# ---------------------------------------------------------------------------
# Layer 3a — Pending order conflict
# ---------------------------------------------------------------------------


class TestPendingConflict:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    def test_rejects_duplicate_ticker_in_same_batch(self) -> None:
        approved_signal = _make_signal(ticker="AAPL")
        new_signal = _make_signal(ticker="AAPL", signal_id=str(uuid.uuid4()))
        passed, reason, _ = self.agent._evaluate(
            new_signal, _make_portfolio(), 0, [approved_signal]
        )
        assert not passed
        assert "pending order" in (reason or "").lower()

    def test_accepts_different_ticker(self) -> None:
        approved_signal = _make_signal(ticker="AAPL")
        new_signal = _make_signal(ticker="MSFT", signal_id=str(uuid.uuid4()))
        passed, _, _ = self.agent._evaluate(
            new_signal, _make_portfolio(), 0, [approved_signal]
        )
        assert passed

    def test_accepts_when_no_prior_approved(self) -> None:
        signal = _make_signal(ticker="AAPL")
        passed, _, _ = self.agent._evaluate(signal, _make_portfolio(), 0, [])
        assert passed


# ---------------------------------------------------------------------------
# Layer 3c — Direction conflict
# ---------------------------------------------------------------------------


class TestHasConflictingPosition:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    def test_detects_conflict_long_vs_short(self) -> None:
        portfolio = _make_portfolio(positions=[_make_position_obj("AAPL", qty=100)])
        signal = _make_signal(ticker="AAPL", direction=SignalDirection.SHORT)
        passed, reason, _ = self.agent._evaluate(signal, portfolio, 0, [])
        assert not passed
        assert "direction conflict" in (reason or "").lower()

    def test_detects_conflict_short_vs_long(self) -> None:
        portfolio = _make_portfolio(positions=[_make_position_obj("AAPL", qty=-100)])
        signal = _make_signal(ticker="AAPL", direction=SignalDirection.LONG)
        passed, reason, _ = self.agent._evaluate(signal, portfolio, 0, [])
        assert not passed
        assert "direction conflict" in (reason or "").lower()

    def test_no_conflict_same_direction(self) -> None:
        portfolio = _make_portfolio(positions=[_make_position_obj("AAPL", qty=100)])
        signal = _make_signal(ticker="AAPL", direction=SignalDirection.LONG)
        passed, _, _ = self.agent._evaluate(signal, portfolio, 0, [])
        assert passed

    def test_no_conflict_different_ticker(self) -> None:
        portfolio = _make_portfolio(positions=[_make_position_obj("AAPL", qty=100)])
        signal = _make_signal(ticker="MSFT", direction=SignalDirection.SHORT)
        passed, _, _ = self.agent._evaluate(signal, portfolio, 0, [])
        assert passed

    def test_exit_bypasses_direction_conflict(self) -> None:
        portfolio = _make_portfolio(positions=[_make_position_obj("AAPL", qty=100)])
        signal = _make_signal(ticker="AAPL", direction=SignalDirection.CLOSE)
        passed, _, _ = self.agent._evaluate(signal, portfolio, 0, [])
        assert passed


# ---------------------------------------------------------------------------
# run() integration tests
# ---------------------------------------------------------------------------


class TestRiskManagerRun:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    async def test_all_layers_pass_signal_approved(self) -> None:
        signal = _make_signal(passed_confidence_gate=True)
        state = {"trade_signals": [signal], "portfolio": _make_portfolio()}
        result = await self.agent.run(state)
        assert len(result["approved_signals"]) == 1
        assert len(result["rejected_signals"]) == 0
        assert result["system_halted"] is False

    async def test_gate_false_signal_rejected(self) -> None:
        signal = _make_signal(passed_confidence_gate=False)
        state = {"trade_signals": [signal], "portfolio": _make_portfolio()}
        result = await self.agent.run(state)
        assert len(result["approved_signals"]) == 0
        assert len(result["rejected_signals"]) == 1

    async def test_empty_signals_returns_empty(self) -> None:
        state = {"trade_signals": [], "portfolio": _make_portfolio()}
        result = await self.agent.run(state)
        assert result["approved_signals"] == []
        assert result["rejected_signals"] == []
        assert result["system_halted"] is False

    async def test_missing_portfolio_key_uses_default(self) -> None:
        signal = _make_signal(passed_confidence_gate=True)
        state = {"trade_signals": [signal]}
        result = await self.agent.run(state)
        # Should not crash; default portfolio has 0 drawdown and 0 positions.
        assert len(result["approved_signals"]) == 1

    async def test_risk_dry_run_approves_rejected_signal(self) -> None:
        agent = _make_agent(settings=_make_settings(risk_dry_run=True))
        signal = _make_signal(passed_confidence_gate=False)
        state = {"trade_signals": [signal], "portfolio": _make_portfolio()}
        result = await agent.run(state)
        assert len(result["approved_signals"]) == 1
        assert len(result["rejected_signals"]) == 0

    async def test_drawdown_halt_sets_system_halted(self) -> None:
        portfolio = _make_portfolio(max_drawdown_pct=0.99)
        signal = _make_signal(
            passed_confidence_gate=True, direction=SignalDirection.LONG
        )
        state = {"trade_signals": [signal], "portfolio": portfolio}
        result = await self.agent.run(state)
        assert result["system_halted"] is True
        assert len(result["rejected_signals"]) == 1

    async def test_drawdown_halt_publishes_event(self) -> None:
        portfolio = _make_portfolio(max_drawdown_pct=0.99)
        signal = _make_signal(
            passed_confidence_gate=True, direction=SignalDirection.LONG
        )
        state = {"trade_signals": [signal], "portfolio": portfolio}
        await self.agent.run(state)
        self.agent.event_bus.publish.assert_called_once()

    async def test_concentration_limit_rejects(self) -> None:
        agent = _make_agent(settings=_make_settings(max_open_positions=0))
        signal = _make_signal(
            passed_confidence_gate=True, direction=SignalDirection.LONG
        )
        state = {"trade_signals": [signal], "portfolio": _make_portfolio()}
        result = await agent.run(state)
        assert len(result["rejected_signals"]) == 1

    async def test_within_batch_dedup_rejects_second_signal(self) -> None:
        sig1 = _make_signal(ticker="AAPL", signal_id="s1", passed_confidence_gate=True)
        sig2 = _make_signal(ticker="AAPL", signal_id="s2", passed_confidence_gate=True)
        state = {"trade_signals": [sig1, sig2], "portfolio": _make_portfolio()}
        result = await self.agent.run(state)
        assert len(result["approved_signals"]) == 1
        assert len(result["rejected_signals"]) == 1
        assert result["approved_signals"][0].signal_id == "s1"

    async def test_direction_conflict_rejects(self) -> None:
        portfolio = _make_portfolio(positions=[_make_position_obj("AAPL", qty=100)])
        signal = _make_signal(
            ticker="AAPL",
            direction=SignalDirection.SHORT,
            passed_confidence_gate=True,
        )
        state = {"trade_signals": [signal], "portfolio": portfolio}
        result = await self.agent.run(state)
        assert len(result["rejected_signals"]) == 1

    async def test_mixed_signals_split_correctly(self) -> None:
        good = _make_signal(
            ticker="AAPL", signal_id="good", passed_confidence_gate=True
        )
        bad = _make_signal(ticker="MSFT", signal_id="bad", passed_confidence_gate=False)
        state = {"trade_signals": [good, bad], "portfolio": _make_portfolio()}
        result = await self.agent.run(state)
        assert len(result["approved_signals"]) == 1
        assert result["approved_signals"][0].signal_id == "good"
        assert len(result["rejected_signals"]) == 1
        assert result["rejected_signals"][0].signal_id == "bad"


# ---------------------------------------------------------------------------
# Layer 3b — Position size cap
# ---------------------------------------------------------------------------


class TestSizeCapLayer:
    def setup_method(self) -> None:
        # equity=100_000, max_position_pct=0.10 → cap = 10_000 per position
        self.agent = _make_agent(settings=_make_settings(max_position_pct=0.10))
        self.portfolio = _make_portfolio(equity=100_000.0)

    def test_qty_reduced_when_over_cap(self) -> None:
        """200 shares @ $100 = $20k > $10k cap → reduced to 100 shares."""
        signal = _make_signal(suggested_qty=200, entry_price=100.0)
        result = self.agent._apply_size_cap(signal, self.portfolio)
        assert result.suggested_qty == 100

    def test_qty_unchanged_when_within_cap(self) -> None:
        """50 shares @ $100 = $5k <= $10k cap → unchanged."""
        signal = _make_signal(suggested_qty=50, entry_price=100.0)
        result = self.agent._apply_size_cap(signal, self.portfolio)
        assert result.suggested_qty == 50

    def test_no_entry_price_skips_cap(self) -> None:
        """Market order (entry_price=None) → signal returned unchanged."""
        signal = _make_signal(suggested_qty=999, entry_price=None)
        result = self.agent._apply_size_cap(signal, self.portfolio)
        assert result.suggested_qty == 999

    def test_zero_equity_skips_cap(self) -> None:
        """Zero equity → no division; signal returned unchanged."""
        signal = _make_signal(suggested_qty=999, entry_price=10.0)
        result = self.agent._apply_size_cap(signal, _make_portfolio(equity=0.0))
        assert result.suggested_qty == 999

    def test_capped_qty_minimum_one(self) -> None:
        """Even if max_value < entry_price, qty floors at 1."""
        # equity=100, max_position_pct=0.10 → cap=10; entry_price=50 → 0.2 → floors to 1
        portfolio = _make_portfolio(equity=100.0)
        signal = _make_signal(suggested_qty=10, entry_price=50.0)
        result = self.agent._apply_size_cap(signal, portfolio)
        assert result.suggested_qty == 1

    def test_original_signal_not_mutated(self) -> None:
        """_apply_size_cap returns a model_copy; the original is unchanged."""
        signal = _make_signal(suggested_qty=200, entry_price=100.0)
        result = self.agent._apply_size_cap(signal, self.portfolio)
        assert result is not signal
        assert signal.suggested_qty == 200  # original untouched

    async def test_run_applies_cap_to_approved_signal(self) -> None:
        """Integration: run() shrinks an over-cap signal before adding to approved."""
        signal = _make_signal(
            passed_confidence_gate=True, suggested_qty=200, entry_price=100.0
        )
        state = {"trade_signals": [signal], "portfolio": self.portfolio}
        result = await self.agent.run(state)
        assert len(result["approved_signals"]) == 1
        assert result["approved_signals"][0].suggested_qty == 100

    async def test_run_does_not_cap_rejected_signal(self) -> None:
        """Rejected signals are not modified by the size cap."""
        signal = _make_signal(
            passed_confidence_gate=False, suggested_qty=200, entry_price=100.0
        )
        state = {"trade_signals": [signal], "portfolio": self.portfolio}
        result = await self.agent.run(state)
        assert len(result["rejected_signals"]) == 1
        assert result["rejected_signals"][0].suggested_qty == 200
