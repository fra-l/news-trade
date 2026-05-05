"""Government contract award domain models.

These are the shared type language for the gov-trade pipeline. Every
component — providers, services, agents, graph state — references these
frozen Pydantic objects.

``EnrichmentResult`` is defined in ``models/lobbying.py`` and re-exported
here so existing imports continue to work without changes.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from news_trade.models.lobbying import EnrichmentResult as EnrichmentResult  # re-export


class AwardType(StrEnum):
    """USASpending.gov contract award type codes."""

    BPA_CALL = "A"
    PURCHASE_ORDER = "B"
    DELIVERY_ORDER = "C"
    DEFINITIVE_CONTRACT = "D"


class MaterialityDirection(StrEnum):
    """Overall signal direction produced by the materiality scorer."""

    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"


class NoveltyClass(StrEnum):
    """How novel an award is for a given recipient/agency relationship."""

    NEW_CUSTOMER = "new_customer"
    EXPANSION = "expansion"
    RENEWAL = "renewal"
    UNKNOWN = "unknown"


class ResolutionLayer(StrEnum):
    """Which layer of EntityResolutionService produced the result."""

    STATIC = "static"
    EDGAR = "edgar"
    LLM = "llm"
    NONE = "none"


class ContractAwardEvent(BaseModel):
    """Raw federal contract award record ingested from USASpending.gov.

    Fields typed as ``Optional`` are genuinely absent in a subset of
    USASpending records — do not treat ``None`` as an error. Award type
    codes: A = BPA Call, B = Purchase Order, C = Delivery Order,
    D = Definitive Contract.
    """

    model_config = ConfigDict(frozen=True)

    award_id: str = Field(description="USASpending internal award identifier")
    recipient_name: str = Field(
        description=(
            "Legal entity name of the contract recipient as it appears in USASpending"
        )
    )
    recipient_uei: str | None = Field(
        default=None,
        description="Unique Entity Identifier (replaced DUNS in 2022)",
    )
    recipient_parent_name: str | None = Field(
        default=None,
        description="Parent company legal name when the recipient is a subsidiary",
    )
    amount_usd: float = Field(
        ge=0.0,
        description="Total obligated value of the award in US dollars",
    )
    awarding_agency: str = Field(
        description="Top-level awarding agency name (e.g. 'DEPT OF DEFENSE')"
    )
    awarding_sub_agency: str | None = Field(
        default=None,
        description="Sub-agency or contracting office within the top-level agency",
    )
    award_type: str = Field(
        description=(
            "Award type code: A = BPA Call, B = Purchase Order, "
            "C = Delivery Order, D = Definitive Contract"
        ),
    )
    description: str = Field(
        default="",
        description="Free-text description of the work, product, or service awarded",
    )
    naics_code: str | None = Field(
        default=None,
        description="NAICS industry classification code",
    )
    naics_description: str | None = Field(
        default=None,
        description="Human-readable NAICS industry description",
    )
    period_start: date | None = Field(
        default=None,
        description="Period of performance start date",
    )
    period_end: date | None = Field(
        default=None,
        description=(
            "Period of performance end date; "
            "multi-year end dates signal recurring revenue"
        ),
    )
    sign_date: date = Field(
        description="Date the contract was signed and obligated"
    )
    last_modified_date: date | None = Field(
        default=None,
        description="Date the USASpending record was last modified by the agency",
    )
    place_of_performance_state: str | None = Field(
        default=None,
        description="Two-letter US state code where work is primarily performed",
    )
    fetched_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp when this record was retrieved from the USASpending API",
    )


class EntityResolution(BaseModel):
    """Result of mapping a USASpending recipient name to a public stock ticker.

    Includes which resolution layer succeeded and the confidence level so
    that accuracy can be analysed and calibrated over time. Awards where
    ``ticker`` is ``None`` or ``confidence`` is below the configured minimum
    must be discarded — never trade on a low-confidence resolution.
    """

    model_config = ConfigDict(frozen=True)

    recipient_name: str = Field(
        description="Raw recipient name exactly as returned by USASpending"
    )
    ticker: str | None = Field(
        default=None,
        description=(
            "Resolved stock ticker symbol; None when all resolution layers failed"
        ),
    )
    exchange: str | None = Field(
        default=None,
        description=(
            "Primary listing exchange (e.g. 'NYSE', 'NASDAQ'); None when unresolved"
        ),
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description=(
            "Resolution confidence in [0.0, 1.0]. "
            "Static layer always returns 1.0; EDGAR layer ~0.9; LLM layer is variable."
        )
    )
    layer: ResolutionLayer = Field(
        description="Resolution layer that produced this result",
    )
    reasoning: str | None = Field(
        default=None,
        description="LLM-generated explanation; None for static and EDGAR layers",
    )
    resolved_at: datetime = Field(default_factory=datetime.utcnow)


class MaterialityResult(BaseModel):
    """Output of the MaterialityScorerAgent for a single resolved contract award.

    Analogous to ``SentimentResult`` in the news pipeline. Combines a base
    score, an enrichment-adjusted score, a directional label, a novelty
    classification, and human-readable reasoning for downstream signal
    generation.
    """

    model_config = ConfigDict(frozen=True)

    award_id: str = Field(
        description="References the originating ContractAwardEvent"
    )
    ticker: str = Field(
        description="Resolved ticker this materiality result applies to"
    )
    score: Annotated[float, Field(ge=0.0, le=10.0)] = Field(
        description="Base materiality score on a 0-10 scale before lobbying enrichment"
    )
    adjusted_score: Annotated[float, Field(ge=0.0)] = Field(
        description=(
            "Score after applying the lobbying enrichment multiplier "
            "(adjusted_score = score x enrichment.multiplier). "
            "May exceed 10.0 when the multiplier is above 1.0."
        )
    )
    direction: MaterialityDirection = Field(
        description="Overall signal direction derived from the award analysis"
    )
    novelty: NoveltyClass = Field(
        description="How novel this award is for the recipient/agency relationship"
    )
    reasoning: str = Field(
        default="",
        description="Explanation of the score and direction from the scorer",
    )
    award_pct_of_market_cap: float | None = Field(
        default=None,
        description=(
            "Award amount as a percentage of the recipient's market cap; "
            "None when market cap data is unavailable"
        ),
    )
    model_id: str = Field(
        default="",
        description=(
            "Model identifier when the LLM scorer was used; "
            "empty for the heuristic scorer"
        ),
    )
    provider: str = Field(
        default="heuristic",
        description="Scorer implementation: 'llm' or 'heuristic'",
    )
    enrichment: EnrichmentResult | None = Field(
        default=None,
        description=(
            "Lobbying enrichment result; None when enrichment is disabled "
            "or data was unavailable for this ticker/agency pair"
        ),
    )
    scored_at: datetime = Field(default_factory=datetime.utcnow)
