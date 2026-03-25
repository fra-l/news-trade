"""Stage 1 pre-earnings position models for the two-stage trade management system."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field


class Stage1Status(StrEnum):
    """Lifecycle states for a Stage 1 pre-earnings position."""

    OPEN = "open"
    CONFIRMED = "confirmed"  # earnings beat aligned with our long direction
    REVERSED = "reversed"    # earnings result opposite to our position
    EXITED = "exited"        # closed flat (EARN_MIXED or manual)
    EXPIRED = "expired"      # report date passed with no EARN_* event detected


class OpenStage1Position(BaseModel):
    """A live pre-earnings (Stage 1) position opened by SignalGeneratorAgent.

    Persisted to SQLite via OpenStage1PositionRow so it survives across
    pipeline runs. Stage 2 logic loads this record when the announcement
    arrives to decide whether to confirm, reverse, or exit the position.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="UUID string — primary key in stage1_positions table")
    ticker: str = Field(description="Equity ticker symbol")
    direction: str = Field(description='"long" or "short"')
    size_pct: Annotated[float, Field(ge=0.25, le=0.40)] = Field(
        description="Position size as fraction of portfolio (0.25-0.40)"
    )
    entry_price: Annotated[float, Field(gt=0.0)] = Field(
        description="Fill price at entry"
    )
    opened_at: datetime = Field(description="UTC timestamp when position was opened")
    expected_report_date: date = Field(
        description="Expected earnings report date (ET calendar)"
    )
    fiscal_quarter: str = Field(
        description='Fiscal quarter string, e.g. "Q2 2026"'
    )
    historical_beat_rate: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description="Beat rate used to size the position (from FMP or observed history)"
    )
    status: Stage1Status = Field(
        default=Stage1Status.OPEN,
        description="Current lifecycle state",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_to_report(self) -> int:
        """Calendar days until the expected report date (negative if past)."""
        return (self.expected_report_date - date.today()).days
