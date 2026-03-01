"""Portfolio and position models."""

from datetime import datetime

from pydantic import BaseModel, Field


class Position(BaseModel):
    """A single open position."""

    ticker: str
    qty: int = Field(description="Signed quantity: positive = long, negative = short")
    avg_entry_price: float
    current_price: float = Field(default=0.0)
    unrealized_pnl: float = Field(default=0.0)
    market_value: float = Field(default=0.0)


class PortfolioState(BaseModel):
    """Snapshot of the portfolio used by the RiskManagerAgent.

    Provides position-level and account-level data needed to enforce
    risk limits before order execution.
    """

    equity: float = Field(description="Total account equity")
    cash: float = Field(description="Available cash")
    buying_power: float = Field(default=0.0)
    positions: list[Position] = Field(default_factory=list)
    daily_pnl: float = Field(default=0.0)
    max_drawdown_pct: float = Field(
        default=0.0,
        description="Peak-to-trough drawdown as a fraction of peak equity",
    )
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def position_count(self) -> int:
        return len(self.positions)

    def get_position(self, ticker: str) -> Position | None:
        """Return the position for a ticker, or None."""
        for pos in self.positions:
            if pos.ticker == ticker:
                return pos
        return None
