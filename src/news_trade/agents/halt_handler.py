"""HaltHandlerAgent — emergency cleanup when system drawdown halt is triggered.

Runs as a dedicated LangGraph node after RiskManagerAgent sets system_halted=True.
Cancels all pending Alpaca orders, closes all open positions, and marks every
OPEN Stage 1 position as EXPIRED so the concentration check stays accurate in
subsequent pipeline cycles.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from alpaca.trading.client import TradingClient

from news_trade.agents.base import BaseAgent
from news_trade.models.positions import Stage1Status
from news_trade.services.stage1_repository import Stage1Repository

if TYPE_CHECKING:
    from news_trade.config import Settings
    from news_trade.services.event_bus import EventBus


class HaltHandlerAgent(BaseAgent):
    """Emergency cleanup agent that runs when a drawdown halt is triggered.

    Responsibilities:
        - Cancel all pending Alpaca orders via ``TradingClient.cancel_orders()``.
        - Close all open Alpaca positions via ``TradingClient.close_all_positions()``.
        - Mark every OPEN Stage 1 position as EXPIRED so it does not inflate the
          concentration check in ``RiskManagerAgent`` on subsequent cycles.

    Each step is wrapped in a ``try/except`` so a failure in one step (e.g. Alpaca
    unreachable) does not prevent the remaining steps from running. Errors are
    accumulated and returned; nothing is re-raised.

    Both ``alpaca_client`` and ``stage1_repo`` are ``None``-safe — passing ``None``
    skips the corresponding step without error. This mirrors the pattern used by
    ``ExecutionAgent`` and simplifies test construction.
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        alpaca_client: TradingClient | None = None,
        stage1_repo: Stage1Repository | None = None,
    ) -> None:
        super().__init__(settings, event_bus)
        self._alpaca = alpaca_client
        self._stage1_repo = stage1_repo

    # ------------------------------------------------------------------
    # LangGraph node
    # ------------------------------------------------------------------

    async def run(self, state: dict) -> dict:  # type: ignore[type-arg]
        """Execute halt cleanup sequence.

        ``system_halted=True`` is already set in state by ``RiskManagerAgent``;
        this node does not mutate it.

        Returns:
            ``{"errors": [...]}`` — empty list on full success.
        """
        from news_trade.models.portfolio import PortfolioState

        portfolio: PortfolioState | None = state.get("portfolio")
        drawdown = (
            f"{portfolio.max_drawdown_pct:.1%}" if portfolio is not None else "unknown"
        )
        self.logger.critical(
            "SYSTEM HALT triggered — drawdown=%s; cancelling orders and closing"
            " positions",
            drawdown,
        )

        errors: list[str] = list(state.get("errors", []))

        errors.extend(await self._cancel_all_orders())
        errors.extend(await self._close_all_positions())
        errors.extend(self._expire_open_stage1_positions())

        return {"errors": errors}

    # ------------------------------------------------------------------
    # Cleanup steps
    # ------------------------------------------------------------------

    async def _cancel_all_orders(self) -> list[str]:
        """Cancel all pending Alpaca orders in a single API call."""
        if self._alpaca is None:
            self.logger.debug(
                "HaltHandler: no alpaca_client — skipping order cancellation"
            )
            return []
        try:
            await asyncio.to_thread(self._alpaca.cancel_orders)
            self.logger.warning("HaltHandler: all pending orders cancelled")
        except Exception as exc:
            self.logger.error("HaltHandler: cancel_orders failed: %s", exc)
            return [f"halt_cancel_orders:{exc}"]
        return []

    async def _close_all_positions(self) -> list[str]:
        """Close all open Alpaca positions and cancel any associated orders."""
        if self._alpaca is None:
            self.logger.debug("HaltHandler: no alpaca_client — skipping position close")
            return []
        try:
            await asyncio.to_thread(
                self._alpaca.close_all_positions,
                cancel_orders=True,
            )
            self.logger.warning("HaltHandler: all open positions closed")
        except Exception as exc:
            self.logger.error("HaltHandler: close_all_positions failed: %s", exc)
            return [f"halt_close_positions:{exc}"]
        return []

    def _expire_open_stage1_positions(self) -> list[str]:
        """Mark all OPEN Stage 1 positions as EXPIRED."""
        if self._stage1_repo is None:
            self.logger.debug("HaltHandler: no stage1_repo — skipping Stage1 expiry")
            return []
        errors: list[str] = []
        try:
            open_positions = self._stage1_repo.load_all_open()
            for pos in open_positions:
                self._stage1_repo.update_status(pos.id, Stage1Status.EXPIRED)
                self.logger.warning(
                    "HaltHandler: expired Stage1 position %s %s (id=%s)",
                    pos.ticker,
                    pos.fiscal_quarter,
                    pos.id,
                )
            if open_positions:
                self.logger.info(
                    "HaltHandler: marked %d Stage1 position(s) as EXPIRED",
                    len(open_positions),
                )
        except Exception as exc:
            self.logger.error("HaltHandler: Stage1 expiry failed: %s", exc)
            errors.append(f"halt_expire_stage1:{exc}")
        return errors
