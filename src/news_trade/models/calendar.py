"""Earnings calendar models."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field


class ReportTiming(StrEnum):
    """When during the trading day the earnings report is released."""

    PRE_MARKET = "pre_market"
    POST_MARKET = "post_market"
    UNKNOWN = "unknown"


class EarningsCalendarEntry(BaseModel):
    """A single upcoming earnings report from a calendar provider.

    Produced by ``EarningsCalendarAgent`` and used to synthesise
    ``NewsEvent(event_type=EARN_PRE)`` objects published to Redis.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str = Field(description="Stock ticker symbol")
    report_date: date = Field(description="Expected earnings report date")
    fiscal_quarter: str = Field(description="Fiscal quarter label, e.g. 'Q1 2026'")
    fiscal_year: int = Field(description="Fiscal year of the report")
    timing: ReportTiming = Field(
        default=ReportTiming.UNKNOWN,
        description="Pre-market, post-market, or unknown report timing",
    )
    eps_estimate: float | None = Field(
        default=None,
        description="Consensus EPS estimate for the quarter",
    )
    fetched_at: datetime = Field(
        description="UTC timestamp when this entry was fetched",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_until_report(self) -> int:
        """Calendar days from today until the report date."""
        return (self.report_date - date.today()).days

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_actionable(self) -> bool:
        """True if the report is 2-5 days away (the EARN_PRE signal window).

        Beyond 5 days the signal decays too early; under 2 days implied
        volatility is already elevated and the edge disappears.
        """
        return 2 <= self.days_until_report <= 5
