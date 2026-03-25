"""Historical earnings outcome model for Pattern D reflection loop.

Stage1Repository returns a HistoricalOutcomes instance when queried for a
ticker's past EARN_PRE resolution history.  Consumers (EarningsCalendarAgent)
use this to decide whether to rely on own-system observed beat rates or fall
back to FMP historical data.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class HistoricalOutcomes(BaseModel):
    """Aggregated earnings outcomes observed by this system for a single ticker.

    When ``source`` is ``"observed"`` and ``sample_size >= 4``, callers should
    use ``beat_rate`` in preference to FMP historical data.  When ``source``
    is ``"fmp"`` (insufficient observed sample), ``beat_rate`` is ``None`` and
    the caller is responsible for fetching the FMP fallback.
    """

    model_config = ConfigDict(frozen=True)

    source: Literal["observed", "fmp"] = Field(
        description=(
            '"observed" = own-system data with sample_size >= 4; '
            '"fmp" = insufficient sample, caller must do FMP lookup'
        )
    )
    beat_rate: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        description=(
            "Fraction of CONFIRMED outcomes among CONFIRMED + REVERSED; "
            "None when source=fmp"
        )
    )
    sample_size: Annotated[int, Field(ge=0)] = Field(
        default=0,
        description=(
            "Count of CONFIRMED + REVERSED outcomes (EXPIRED excluded from denominator)"
        ),
    )
    mean_eps_surprise: float | None = Field(
        default=None,
        description="Mean eps_surprise_pct across recorded outcomes (None if all null)",
    )
    mean_price_move_1d: float | None = Field(
        default=None,
        description="Mean price_move_1d across recorded outcomes (None if all null)",
    )
