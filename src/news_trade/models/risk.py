"""Risk validation model produced by RiskManagerAgent."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RiskValidation(BaseModel):
    """Result of the RiskManagerAgent's five-layer signal validation.

    Produced for each TradeSignal processed by RiskManagerAgent.
    Carries the approval decision, the layer that triggered a rejection,
    and an audit trail of all checks that were evaluated.
    """

    model_config = ConfigDict(frozen=True)

    approved: bool
    rejection_reason: str | None = Field(
        default=None,
        description="Human-readable reason for rejection; None when approved",
    )
    original_size: float = Field(
        description="signal.suggested_qty at the time of evaluation"
    )
    approved_size: float | None = Field(
        default=None,
        description="Final approved size; may be reduced by the size-cap layer",
    )
    checks_run: list[str] = Field(
        default_factory=list,
        description="Ordered names of checks that were evaluated (audit trail)",
    )
    checked_at: datetime = Field(default_factory=datetime.utcnow)
