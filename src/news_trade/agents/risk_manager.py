"""RiskManagerAgent — validates signals against portfolio risk limits."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from news_trade.agents.base import BaseAgent
from news_trade.models.portfolio import PortfolioState
from news_trade.models.risk import RiskValidation
from news_trade.models.signals import SignalDirection, TradeSignal


class _SystemHaltedEvent(BaseModel):
    """Published to Redis when the drawdown halt is triggered."""

    event: str = "SYSTEM_HALTED"
    reason: str

if TYPE_CHECKING:
    from news_trade.config import Settings
    from news_trade.services.event_bus import EventBus
    from news_trade.services.stage1_repository import Stage1Repository


class RiskManagerAgent(BaseAgent):
    """Gate-keeper that approves or rejects trade signals based on risk rules.

    Five-layer fail-fast checks (executed in order per signal):

    1. Confidence gate  — reject if ``passed_confidence_gate`` is False.
    2a. Drawdown halt   — reject (non-EXIT) if portfolio drawdown >= threshold;
                          sets ``system_halted=True`` in pipeline state.
    2b. Concentration   — reject (non-EXIT, non-ADD) if open position count >= limit;
                          Stage 2 ADD signals (stage1_id set) are exempt.
    3a. Pending conflict — reject if ticker already approved in this batch.
    3b. Size cap        — soft: log warning if position value exceeds
                          ``max_position_pct``; no rejection (model uses
                          ``suggested_qty`` not ``size_pct``).
    3c. Direction conflict — reject if existing position has opposite direction.

    When ``settings.risk_dry_run`` is True all checks still run and are logged, but
    every signal is moved to ``approved_signals`` regardless of outcome.
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        stage1_repo: Stage1Repository,
    ) -> None:
        super().__init__(settings, event_bus)
        self._stage1_repo = stage1_repo

    # ------------------------------------------------------------------
    # LangGraph node
    # ------------------------------------------------------------------

    async def run(self, state: dict) -> dict:  # type: ignore[type-arg]
        """Validate trade signals against current portfolio state.

        Returns:
            ``{"approved_signals": [...], "rejected_signals": [...],
            "system_halted": bool}``
        """
        signals: list[TradeSignal] = state.get("trade_signals", [])
        portfolio: PortfolioState = state.get(
            "portfolio", PortfolioState(equity=0.0, cash=0.0)
        )

        approved: list[TradeSignal] = []
        rejected: list[TradeSignal] = []
        system_halted: bool = False

        # Precompute open-position count once (Stage1 positions + Alpaca positions).
        stage1_open_count = len(self._stage1_repo.load_all_open())
        open_count = stage1_open_count + portfolio.position_count

        for signal in signals:
            passed, reason, validation = self._evaluate(
                signal, portfolio, open_count, approved
            )

            if not passed:
                self.logger.warning(
                    "Signal %s for %s REJECTED: %s",
                    signal.signal_id,
                    signal.ticker,
                    reason,
                )
                if self.settings.risk_dry_run:
                    self.logger.warning(
                        "risk_dry_run=True — approving %s despite rejection: %s",
                        signal.signal_id,
                        reason,
                    )
                    approved.append(signal)
                else:
                    rejected.append(signal)

                # Drawdown halt must be propagated even in dry-run.
                if validation is not None and "drawdown_halt" in validation.checks_run:
                    system_halted = True
                    await self.event_bus.publish(
                        "system_events",
                        _SystemHaltedEvent(reason=reason or "drawdown limit breached"),
                    )
            else:
                # L3b — apply size cap (modify, not reject) on approved signals.
                signal = self._apply_size_cap(signal, portfolio)
                self.logger.info(
                    "Signal %s for %s APPROVED (size=%s)",
                    signal.signal_id,
                    signal.ticker,
                    signal.suggested_qty,
                )
                approved.append(signal)

        return {
            "approved_signals": approved,
            "rejected_signals": rejected,
            "system_halted": system_halted,
        }

    # ------------------------------------------------------------------
    # Core evaluation — returns (passed, reason, RiskValidation)
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        signal: TradeSignal,
        portfolio: PortfolioState,
        open_count: int,
        approved_so_far: list[TradeSignal],
    ) -> tuple[bool, str | None, RiskValidation | None]:
        """Run all five layers in order; return on first failure (fail-fast)."""
        checks: list[str] = []

        # L1 — confidence gate
        gate_passed, gate_reason = self._check_confidence_gate(signal)
        checks.append("confidence_gate")
        if not gate_passed:
            return (
                False,
                gate_reason,
                RiskValidation(
                    approved=False,
                    rejection_reason=gate_reason,
                    original_size=float(signal.suggested_qty),
                    approved_size=None,
                    checks_run=checks,
                ),
            )

        # L2a — drawdown halt (EXIT signals bypass)
        checks.append("drawdown_halt")
        if (
            not self._check_drawdown(portfolio)
            and signal.direction != SignalDirection.CLOSE
        ):
            reason = (
                f"drawdown {portfolio.max_drawdown_pct:.1%} >= "
                f"limit {self.settings.max_drawdown_pct:.1%}"
            )
            return (
                False,
                reason,
                RiskValidation(
                    approved=False,
                    rejection_reason=reason,
                    original_size=float(signal.suggested_qty),
                    approved_size=None,
                    checks_run=checks,
                ),
            )

        # L2b — concentration limit (EXIT signals bypass)
        checks.append("concentration")
        if not self._check_concentration(signal, open_count):
            limit = self.settings.max_open_positions
            reason = f"open positions {open_count} >= limit {limit}"
            return (
                False,
                reason,
                RiskValidation(
                    approved=False,
                    rejection_reason=reason,
                    original_size=float(signal.suggested_qty),
                    approved_size=None,
                    checks_run=checks,
                ),
            )

        # L3a — pending order conflict (within this batch)
        checks.append("pending_conflict")
        if self._check_pending_conflict(signal, approved_so_far):
            reason = f"ticker {signal.ticker} already has a pending order in this batch"
            return (
                False,
                reason,
                RiskValidation(
                    approved=False,
                    rejection_reason=reason,
                    original_size=float(signal.suggested_qty),
                    approved_size=None,
                    checks_run=checks,
                ),
            )

        # L3b — size cap (modify, not reject); actual reduction applied in run()
        checks.append("size_cap")

        # L3c — direction conflict
        checks.append("direction_conflict")
        dir_ok, dir_reason = self._check_direction_conflict(signal, portfolio)
        if not dir_ok:
            return (
                False,
                dir_reason,
                RiskValidation(
                    approved=False,
                    rejection_reason=dir_reason,
                    original_size=float(signal.suggested_qty),
                    approved_size=None,
                    checks_run=checks,
                ),
            )

        validation = RiskValidation(
            approved=True,
            original_size=float(signal.suggested_qty),
            approved_size=float(signal.suggested_qty),
            checks_run=checks,
        )
        return True, None, validation

    # ------------------------------------------------------------------
    # Individual check helpers
    # ------------------------------------------------------------------

    def _check_confidence_gate(self, signal: TradeSignal) -> tuple[bool, str | None]:
        """Layer 1 — reject if ConfidenceScorer did not set passed_confidence_gate."""
        if not signal.passed_confidence_gate:
            reason = signal.rejection_reason or "confidence gate not passed"
            return False, reason
        return True, None

    def _check_drawdown(self, portfolio: PortfolioState) -> bool:
        """Layer 2a — return True if drawdown is within the hard limit."""
        return portfolio.max_drawdown_pct < self.settings.max_drawdown_pct

    def _check_concentration(self, signal: TradeSignal, open_count: int) -> bool:
        """Layer 2b — return True if there is room for another position.

        EXIT signals always pass.  Stage 2 ADD signals (``stage1_id`` set) also
        pass — they extend an existing EARN_PRE position rather than opening a
        new one, so they must not count against the concentration limit.
        """
        if signal.direction == SignalDirection.CLOSE:
            return True
        if signal.stage1_id is not None:
            return True
        return open_count < self.settings.max_open_positions

    def _check_pending_conflict(
        self, signal: TradeSignal, approved: list[TradeSignal]
    ) -> bool:
        """Layer 3a — True if ticker already has a pending order in this batch."""
        return signal.ticker in {s.ticker for s in approved}

    def _apply_size_cap(
        self, signal: TradeSignal, portfolio: PortfolioState
    ) -> TradeSignal:
        """Layer 3b — reduce ``suggested_qty`` if position value exceeds
        ``max_position_pct``.

        Returns the same signal unchanged when:
        - ``entry_price`` is None (market order; no price to compute against), or
        - ``portfolio.equity`` is zero or negative, or
        - the position value is already within the cap.

        When the cap is breached the signal is returned as a ``model_copy`` with a
        reduced ``suggested_qty = max(1, floor(max_value / entry_price))``.  This is
        a *modify, not reject* layer — the signal is still approved.
        """
        if signal.entry_price is None or portfolio.equity <= 0:
            return signal
        position_value = signal.suggested_qty * signal.entry_price
        max_value = portfolio.equity * self.settings.max_position_pct
        if position_value <= max_value:
            return signal
        capped_qty = max(1, int(max_value / signal.entry_price))
        self.logger.warning(
            "Signal %s size reduced %d → %d shares: position value %.2f exceeds "
            "max_position_pct cap %.2f (equity=%.2f, max_position_pct=%.2f)",
            signal.signal_id,
            signal.suggested_qty,
            capped_qty,
            position_value,
            max_value,
            portfolio.equity,
            self.settings.max_position_pct,
        )
        return signal.model_copy(update={"suggested_qty": capped_qty})

    def _check_direction_conflict(
        self, signal: TradeSignal, portfolio: PortfolioState
    ) -> tuple[bool, str | None]:
        """Layer 3c — return True if no opposing position exists for this ticker.

        EXIT signals always pass — they are meant to close existing positions.
        """
        if signal.direction == SignalDirection.CLOSE:
            return True, None
        existing = portfolio.get_position(signal.ticker)
        if existing is None:
            return True, None
        # qty > 0 means long; qty < 0 means short
        long_conflict = existing.qty > 0 and signal.direction == SignalDirection.SHORT
        short_conflict = existing.qty < 0 and signal.direction == SignalDirection.LONG
        if long_conflict or short_conflict:
            side = "long" if existing.qty > 0 else "short"
            reason = (
                f"direction conflict: existing {side} position "
                f"vs {signal.direction} signal for {signal.ticker}"
            )
            return False, reason
        return True, None
