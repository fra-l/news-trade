"""Rule-based materiality scorer — no network calls, no LLM.

Scoring is driven by two inputs:
  1. Award value as a percentage of market cap (primary signal)
  2. Novelty classification derived from award type code and description keywords

Thresholds are calibrated against the CONTEXT.md materiality table:

  award/mktcap  < 0.5%   → base score 1.0  (negligible)
  0.5% - 2%              -> base score 2.5  (low)
  2%  - 5%               -> base score 4.0  (medium-low)
  5%  - 10%              -> base score 5.5  (medium)
  10% - 25%              -> base score 7.5  (high)
  ≥ 25%                  → base score 9.5  (very high)

When market cap is unavailable, absolute award value is used as a proxy:

  < $25M                 → base score 2.0
  $25M - $100M           -> base score 3.5
  $100M - $500M          -> base score 5.5
  ≥ $500M                → base score 7.0

Novelty adjustments (applied after base score):
  NEW_CUSTOMER   +1.5   (new agency relationship — more signal value)
  EXPANSION       0.0   (no adjustment)
  RENEWAL        -1.0   (recompete — market may already expect it)
  UNKNOWN         0.0   (insufficient information)

Final score is clamped to [0.0, 10.0]. Direction is BULLISH when score ≥ 3.0,
NEUTRAL otherwise. Contract awards are always positive for the recipient so
BEARISH is never returned by this provider.
"""

from __future__ import annotations

import logging
from datetime import datetime

from news_trade.models.contracts import (
    ContractAwardEvent,
    MaterialityDirection,
    MaterialityResult,
    NoveltyClass,
)

_logger = logging.getLogger(__name__)

_BULLISH_THRESHOLD = 3.0

_NOVELTY_ADJUSTMENT: dict[NoveltyClass, float] = {
    NoveltyClass.NEW_CUSTOMER: 1.5,
    NoveltyClass.EXPANSION: 0.0,
    NoveltyClass.RENEWAL: -1.0,
    NoveltyClass.UNKNOWN: 0.0,
}

_RENEWAL_KEYWORDS = frozenset({
    "recompete", "re-compete", "option period", "option exercise",
    "continuation", "follow-on", "follow on", "renewal",
})
_EXPANSION_KEYWORDS = frozenset({
    "additional", "increase", "modification", "supplemental",
    "exercise of option",
})


class HeuristicMaterialityProvider:
    """Score contract award materiality using rule-based heuristics.

    No network calls, no LLM. Safe to use as a fallback when the LLM
    provider's budget is exhausted or as the primary scorer in dry-run mode.
    """

    @property
    def name(self) -> str:
        return "heuristic"

    async def score(
        self,
        award: ContractAwardEvent,
        ticker: str,
        market_cap_usd: float | None = None,
    ) -> MaterialityResult:
        novelty = _classify_novelty(award)
        base = _base_score(award.amount_usd, market_cap_usd)
        raw = base + _NOVELTY_ADJUSTMENT[novelty]
        final = max(0.0, min(10.0, raw))

        pct: float | None = None
        if market_cap_usd and market_cap_usd > 0:
            pct = round(award.amount_usd / market_cap_usd * 100, 4)

        direction = (
            MaterialityDirection.BULLISH
            if final >= _BULLISH_THRESHOLD
            else MaterialityDirection.NEUTRAL
        )

        reasoning = _build_reasoning(award, ticker, novelty, base, final, pct)

        return MaterialityResult(
            award_id=award.award_id,
            ticker=ticker,
            score=round(final, 4),
            adjusted_score=round(final, 4),
            direction=direction,
            novelty=novelty,
            reasoning=reasoning,
            award_pct_of_market_cap=pct,
            provider="heuristic",
            enrichment=None,
            scored_at=datetime.utcnow(),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_score(amount_usd: float, market_cap_usd: float | None) -> float:
    if market_cap_usd and market_cap_usd > 0:
        ratio = amount_usd / market_cap_usd
        return _ratio_to_score(ratio)
    return _absolute_to_score(amount_usd)


def _ratio_to_score(ratio: float) -> float:
    if ratio < 0.005:
        return 1.0
    if ratio < 0.02:
        return 2.5
    if ratio < 0.05:
        return 4.0
    if ratio < 0.10:
        return 5.5
    if ratio < 0.25:
        return 7.5
    return 9.5


def _absolute_to_score(amount: float) -> float:
    if amount < 25_000_000:
        return 2.0
    if amount < 100_000_000:
        return 3.5
    if amount < 500_000_000:
        return 5.5
    return 7.0


def _classify_novelty(award: ContractAwardEvent) -> NoveltyClass:
    desc = (award.description or "").lower()

    if any(kw in desc for kw in _RENEWAL_KEYWORDS):
        return NoveltyClass.RENEWAL
    if any(kw in desc for kw in _EXPANSION_KEYWORDS):
        return NoveltyClass.EXPANSION
    # Definitive Contract with no renewal/expansion keywords → new relationship
    if award.award_type == "D":
        return NoveltyClass.NEW_CUSTOMER
    # Delivery Order without keywords → probably an expansion on an existing vehicle
    if award.award_type == "C":
        return NoveltyClass.EXPANSION
    return NoveltyClass.UNKNOWN


def _build_reasoning(
    award: ContractAwardEvent,
    ticker: str,
    novelty: NoveltyClass,
    base: float,
    final: float,
    pct: float | None,
) -> str:
    pct_str = f"{pct:.1f}% of market cap" if pct is not None else "market cap unknown"
    return (
        f"${award.amount_usd:,.0f} award from {award.awarding_agency} "
        f"({pct_str}); novelty={novelty.value}; "
        f"base={base:.1f} → final={final:.1f}"
    )
