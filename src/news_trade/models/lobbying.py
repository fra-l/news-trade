"""Lobbying disclosure models for the Senate LDA enrichment layer.

``EnrichmentResult`` is defined here and re-exported from
``models/contracts.py`` so existing code that imports it from there
continues to work without changes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class LobbyingTrend(StrEnum):
    """Direction of lobbying spend on a specific agency over recent quarters."""

    INCREASING = "increasing"
    FLAT = "flat"
    DECREASING = "decreasing"
    NEW = "new"
    STOPPED = "stopped"
    NO_DATA = "no_data"


class EnrichmentResult(BaseModel):
    """Lobbying enrichment output — confidence multiplier for a ticker/agency pair.

    Applied by ``LobbyingEnrichmentService`` after the base materiality score
    is computed. ``multiplier=1.0`` (neutral) when no data is found or
    enrichment is disabled. The service never raises — any failure returns
    the neutral default so the base signal is unaffected.
    """

    model_config = ConfigDict(frozen=True)

    multiplier: Annotated[float, Field(ge=0.5, le=2.0)] = Field(
        default=1.0,
        description=(
            "Confidence multiplier applied to the base materiality score "
            "(1.0 = neutral)"
        ),
    )
    rationale: str = Field(
        default="",
        description="Human-readable explanation for the multiplier chosen",
    )
    data_found: bool = Field(
        default=False,
        description=(
            "True when lobbying filings were located for this ticker/agency pair"
        ),
    )
    quarters_lookback: int = Field(
        default=0,
        description="Number of prior quarters of lobbying data analysed",
    )
    spend_delta_pct: float | None = Field(
        default=None,
        description=(
            "Percentage change in lobbying spend directed at this agency; "
            "None when there is insufficient history to compute a delta"
        ),
    )


class LobbyingFiling(BaseModel):
    """A single quarterly LDA lobbying disclosure for one company.

    Per-agency spend is derived by proportional allocation:
    ``agency_spend[agency] = total_spend / len(lobbied_agencies)``.
    This approximation avoids LLM parsing of activity descriptions while
    capturing the agency-targeting signal.
    """

    model_config = ConfigDict(frozen=True)

    filing_uuid: str = Field(description="Unique LDA filing identifier")
    ticker: str = Field(description="Resolved ticker for the lobbying client")
    lda_client_name: str = Field(
        description="Client name exactly as it appears in the LDA filing"
    )
    year: int = Field(description="Calendar year of the filing period")
    period: str = Field(
        description="Filing period: 'Q1', 'Q2', 'Q3', 'Q4', or 'annual'"
    )
    total_spend: float = Field(
        ge=0.0,
        description="Total lobbying income or expenses for this period in USD",
    )
    agency_spend: dict[str, float] = Field(
        default_factory=dict,
        description="Normalised agency name → proportional spend allocation",
    )
    lobbied_agencies: list[str] = Field(
        default_factory=list,
        description="List of normalised agency names targeted in this filing",
    )
    raw_description: str = Field(
        default="",
        description=(
            "Concatenated activity descriptions (used for LLM parsing if needed)"
        ),
    )


class LobbyingSignal(BaseModel):
    """Derived signal for a specific ticker/agency pair across multiple quarters.

    Produced by ``LobbyingEnrichmentService`` from a set of ``LobbyingFiling``
    objects. Summarises the spend trend that drives the multiplier decision.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    agency: str = Field(description="USASpending agency name this signal applies to")
    quarters_analyzed: int = Field(description="Number of quarters of data used")
    spend_delta_pct: float | None = Field(
        default=None,
        description=(
            "% change from prior-period mean to recent-period mean; "
            "None when insufficient data"
        ),
    )
    trend: LobbyingTrend = Field(
        description="Directional classification of spend on this agency",
    )
    spend_by_quarter: list[float] = Field(
        default_factory=list,
        description="Agency spend per quarter, oldest first",
    )
