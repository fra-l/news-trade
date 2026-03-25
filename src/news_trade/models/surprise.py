"""Earnings surprise and estimates models.

Three layers:
- ``EstimatesData``    — pre-announcement consensus from FMP (no actuals yet)
- ``MetricSurprise``   — post-announcement single-metric surprise (EPS or revenue)
- ``EarningsSurprise`` — post-announcement composite with signal strength
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field


class SurpriseDirection(StrEnum):
    """Beat/miss/in-line classification for a single earnings metric."""

    BEAT = "beat"
    MISS = "miss"
    IN_LINE = "in_line"


class SignalStrength(StrEnum):
    """Tier classification for a post-announcement signal."""

    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NONE = "none"


# ---------------------------------------------------------------------------
# Pre-announcement consensus data
# ---------------------------------------------------------------------------


class EstimatesData(BaseModel):
    """Pre-announcement consensus estimates fetched from FMP (or equivalent).

    This is the raw input to ``EstimatesRenderer``. All fields come directly
    from the data provider; ``estimate_dispersion`` is the only computed field.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    fiscal_period: str = Field(description="e.g. 'Q1 2026'")
    report_date: date

    # EPS estimates
    eps_estimate: float = Field(description="Consensus mean EPS estimate")
    eps_low: float = Field(description="Lowest individual analyst EPS estimate")
    eps_high: float = Field(description="Highest individual analyst EPS estimate")
    eps_trailing_mean: float | None = Field(
        default=None,
        description="Mean EPS over prior 4 quarters; None if insufficient history",
    )

    # Revenue estimates (raw dollars)
    revenue_estimate: Annotated[float, Field(ge=0.0)] = Field(
        description="Consensus mean revenue estimate in dollars"
    )
    revenue_low: Annotated[float, Field(ge=0.0)]
    revenue_high: Annotated[float, Field(ge=0.0)]

    # Historical context (stored as fractions, e.g. 0.75 = 75%, 0.05 = 5% beat)
    historical_beat_rate: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description="Fraction of last 8 quarters where EPS beat consensus",
    )
    mean_eps_surprise: float | None = Field(
        default=None,
        description="Mean historical EPS surprise as a fraction (e.g. 0.05 = 5% beat)",
    )

    # Coverage
    num_analysts: Annotated[int, Field(ge=0)] = Field(
        description="Number of analysts contributing to the consensus"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def estimate_dispersion(self) -> float:
        """Normalised spread of analyst estimates.

        Formula: (eps_high - eps_low) / (4 * |eps_estimate|)
        Returns 0.0 when eps_estimate is zero to avoid division by zero.
        Lower value = higher analyst consensus.
        """
        if self.eps_estimate == 0.0:
            return 0.0
        return (self.eps_high - self.eps_low) / (4.0 * abs(self.eps_estimate))


# ---------------------------------------------------------------------------
# Post-announcement metric surprise
# ---------------------------------------------------------------------------


class MetricSurprise(BaseModel):
    """Surprise metrics for a single metric (EPS or revenue) after announcement.

    All derived fields (``pct_surprise``, ``sigma_surprise``, etc.) are computed
    deterministically from the four raw inputs.
    """

    model_config = ConfigDict(frozen=True)

    actual: float
    consensus: float
    estimate_high: float
    estimate_low: float
    analyst_count: Annotated[int, Field(ge=0)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pct_surprise(self) -> float:
        """((actual - consensus) / |consensus|) * 100.

        Returns 0.0 if consensus is zero.
        """
        if self.consensus == 0.0:
            return 0.0
        return ((self.actual - self.consensus) / abs(self.consensus)) * 100.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def estimate_std(self) -> float:
        """Proxy standard deviation: (high - low) / 4 (inter-quartile approximation)."""
        return (self.estimate_high - self.estimate_low) / 4.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sigma_surprise(self) -> float:
        """(actual - consensus) / estimate_std. Returns 0.0 if estimate_std is zero."""
        if self.estimate_std == 0.0:
            return 0.0
        return (self.actual - self.consensus) / self.estimate_std

    @computed_field  # type: ignore[prop-decorator]
    @property
    def direction(self) -> SurpriseDirection:
        """BEAT if pct_surprise > 2.0, MISS if < -2.0, else IN_LINE."""
        if self.pct_surprise > 2.0:
            return SurpriseDirection.BEAT
        if self.pct_surprise < -2.0:
            return SurpriseDirection.MISS
        return SurpriseDirection.IN_LINE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confidence(self) -> float:
        """(sigma_score * 0.7) + (coverage_score * 0.3), clamped to [0.0, 1.0]."""
        sigma_score = min(abs(self.sigma_surprise) / 3.0, 1.0)
        coverage_score = min(self.analyst_count / 10.0, 1.0)
        return min(sigma_score * 0.7 + coverage_score * 0.3, 1.0)


# ---------------------------------------------------------------------------
# Post-announcement composite
# ---------------------------------------------------------------------------


class EarningsSurprise(BaseModel):
    """Post-announcement composite earnings surprise.

    Built after actual results are published. Used by ``ConfidenceScorer``
    for EARN_BEAT and EARN_MISS events.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    report_date: date
    fiscal_quarter: str
    eps: MetricSurprise
    revenue: MetricSurprise
    guidance_sentiment: Annotated[float, Field(ge=-1.0, le=1.0)] | None = Field(
        default=None,
        description="Guidance sentiment from SentimentAnalystAgent; None if absent",
    )
    guidance_direction: SurpriseDirection | None = Field(default=None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def composite_surprise(self) -> float:
        """(eps.pct * 0.6) + (rev.pct * 0.4) + (guidance_sentiment * 20 if present)."""
        base = (self.eps.pct_surprise * 0.6) + (self.revenue.pct_surprise * 0.4)
        if self.guidance_sentiment is not None:
            base += self.guidance_sentiment * 20.0
        return base

    @computed_field  # type: ignore[prop-decorator]
    @property
    def composite_confidence(self) -> float:
        """Mean of eps.confidence and revenue.confidence."""
        return (self.eps.confidence + self.revenue.confidence) / 2.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def signal_strength(self) -> SignalStrength:
        """Tier classification based on composite_surprise and composite_confidence.

        STRONG:   composite_surprise > 10 AND composite_confidence > 0.7
        MODERATE: composite_surprise > 5  AND composite_confidence > 0.5
        WEAK:     composite_surprise > 2  (any confidence)
        NONE:     composite_surprise <= 2
        """
        cs = self.composite_surprise
        cc = self.composite_confidence
        if cs > 10.0 and cc > 0.7:
            return SignalStrength.STRONG
        if cs > 5.0 and cc > 0.5:
            return SignalStrength.MODERATE
        if cs > 2.0:
            return SignalStrength.WEAK
        return SignalStrength.NONE
