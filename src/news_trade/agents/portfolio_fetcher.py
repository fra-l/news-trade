"""PortfolioFetcherAgent — fetches live account and position data from Alpaca.

Runs as the first node in the LangGraph pipeline every cycle so that
downstream agents (especially RiskManagerAgent) always work with real
portfolio data rather than a zero-equity default.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from news_trade.agents.base import BaseAgent
from news_trade.models.portfolio import PortfolioState, Position

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient

    from news_trade.config import Settings
    from news_trade.services.event_bus import EventBus


class PortfolioFetcherAgent(BaseAgent):
    """Fetches live portfolio state from Alpaca at the start of each pipeline cycle.

    Responsibilities:
        - Call TradingClient.get_account() for equity, cash, buying_power.
        - Call TradingClient.get_all_positions() for open positions.
        - Compute today's drawdown from account.last_equity (previous close).
        - Return a populated PortfolioState so RiskManagerAgent checks are live.

    Graceful degradation: if Alpaca is unreachable (or alpaca_client is None),
    logs a WARNING and returns an empty PortfolioState — the pipeline continues
    with risk checks silently disabled, identical to the pre-fix behaviour.
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        alpaca_client: TradingClient | None = None,
    ) -> None:
        super().__init__(settings, event_bus)
        self._alpaca = alpaca_client

    async def run(self, state: dict) -> dict:  # type: ignore[type-arg]
        """Fetch live portfolio state and write it into the pipeline state.

        Returns:
            ``{"portfolio": PortfolioState}`` — always present, never raises.
        """
        if self._alpaca is None:
            self.logger.warning(
                "PortfolioFetcher: no Alpaca client configured — "
                "risk checks will use zero-equity defaults"
            )
            return {"portfolio": PortfolioState(equity=0.0, cash=0.0)}

        try:
            account, alpaca_positions = await asyncio.gather(
                asyncio.to_thread(self._alpaca.get_account),
                asyncio.to_thread(self._alpaca.get_all_positions),
            )
        except Exception as exc:
            self.logger.warning(
                "PortfolioFetcher: failed to fetch account data from Alpaca: %s — "
                "risk checks will use zero-equity defaults",
                exc,
            )
            return {
                "portfolio": PortfolioState(equity=0.0, cash=0.0),
                "errors": [f"PortfolioFetcher: {exc}"],
            }

        equity = float(getattr(account, "equity", 0) or 0)
        last_equity = float(getattr(account, "last_equity", 0) or 0)
        cash = float(getattr(account, "cash", 0) or 0)
        buying_power = float(getattr(account, "buying_power", 0) or 0)

        daily_pnl = equity - last_equity
        if last_equity > 0:
            max_drawdown_pct = max(0.0, -daily_pnl / last_equity)
        else:
            max_drawdown_pct = 0.0

        positions = [_map_position(pos) for pos in (alpaca_positions or [])]

        portfolio = PortfolioState(
            equity=equity,
            cash=cash,
            buying_power=buying_power,
            positions=positions,
            daily_pnl=daily_pnl,
            max_drawdown_pct=max_drawdown_pct,
            timestamp=datetime.utcnow(),
        )

        self.logger.info(
            "PortfolioFetcher: equity=%.2f  cash=%.2f  positions=%d  "
            "daily_pnl=%.2f  drawdown=%.4f",
            equity,
            cash,
            len(positions),
            daily_pnl,
            max_drawdown_pct,
        )

        return {"portfolio": portfolio}


def _map_position(pos: object) -> Position:
    """Map an alpaca-py Position object to our internal Position model.

    All numeric fields in alpaca-py are Decimal strings; cast with float().
    ``current_price`` may be None intraday when the market is closed.
    """
    return Position(
        ticker=str(getattr(pos, "symbol", "")),
        qty=int(float(getattr(pos, "qty", 0))),
        avg_entry_price=float(getattr(pos, "avg_entry_price", 0)),
        current_price=float(getattr(pos, "current_price", None) or 0),
        unrealized_pnl=float(getattr(pos, "unrealized_pl", 0)),
        market_value=float(getattr(pos, "market_value", 0)),
    )
