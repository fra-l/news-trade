"""Earnings calendar models."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field


class ReportTiming(StrEnum):
    """When during the trading day a company reports earnings."""

    PRE_MARKET = "pre_market"
    POST_MARKET = "post_market"
    UNKNOWN = "unknown"


class EarningsCalendarEntry(BaseModel):
    """A single upcoming earnings report entry from the calendar provider."""

    model_config = ConfigDict(frozen=True)

    ticker: str = Field(description="Equity ticker symbol")
    report_date: date = Field(description="Expected earnings report date (ET)")
    fiscal_quarter: str = Field(description='Fiscal quarter string, e.g. "Q2 2026"')
    fiscal_year: int = Field(description="Fiscal year, e.g. 2026")
    timing: ReportTiming = Field(
        default=ReportTiming.UNKNOWN,
        description="Pre-market, post-market, or unknown timing",
    )
    eps_estimate: float | None = Field(
        default=None,
        description="Consensus EPS estimate for the quarter",
    )
    fetched_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="UTC timestamp when this entry was fetched",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_until_report(self) -> int:
        """Calendar days until the report date (negative if past)."""
        return (self.report_date - date.today()).days

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_actionable(self) -> bool:
        """True if the report is 2-5 days away (the EARN_PRE entry window).

        Beyond 5 days: signal decays before announcement.
        Under 2 days: implied volatility already elevated — adverse entry price.
        """
        return 2 <= self.days_until_report <= 5
