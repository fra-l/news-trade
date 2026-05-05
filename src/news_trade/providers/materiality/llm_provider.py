"""LLM-based materiality scorer.

Uses ``LLMClientFactory.quick`` (cheaper tier) to assess materiality — the
prompt is structured and deterministic enough that the deep model is not
required for this task.

Daily budget cap is enforced using the same pattern as
``ClaudeSentimentProvider``: a per-instance running total is reset at
midnight UTC and checked before each call.  When the budget is exhausted
the provider falls back to ``HeuristicMaterialityProvider`` rather than
returning neutral, since the heuristic produces a better signal than zero.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from pydantic import BaseModel, Field

from news_trade.models.contracts import (
    ContractAwardEvent,
    MaterialityDirection,
    MaterialityResult,
    NoveltyClass,
)
from news_trade.providers.materiality.heuristic_provider import (
    HeuristicMaterialityProvider,
)
from news_trade.services.llm_client import LLMClientFactory

_logger = logging.getLogger(__name__)

# Approximate cost for Haiku (quick model): $0.25/1M input, $1.25/1M output
_INPUT_COST_PER_TOKEN = 0.25 / 1_000_000
_OUTPUT_COST_PER_TOKEN = 1.25 / 1_000_000

_SYSTEM = """\
You are a financial analyst specialising in US federal government contracting.
Assess the materiality of a contract award to a publicly traded company.

Consider:
- Award size relative to the company's market cap
- Whether this is a new customer relationship, an expansion, or a renewal
- The strategic importance of the awarding agency for this company's sector
- Whether the contract description suggests recurring revenue or one-time work

Do NOT invent revenue figures. If market cap is not provided, base your
assessment on the absolute award size and sector context only.
"""


class _LLMMaterialitySchema(BaseModel):
    score: float = Field(
        ge=0.0, le=10.0,
        description="Materiality score 0 (negligible) - 10 (transformative)",
    )
    direction: str = Field(
        description="Signal direction: 'bullish', 'neutral', or 'bearish'",
    )
    novelty: str = Field(
        description=(
            "Novelty classification: 'new_customer', 'expansion', "
            "'renewal', or 'unknown'"
        ),
    )
    award_pct_of_market_cap: float | None = Field(
        default=None,
        description="Award as % of market cap if you can estimate it; null otherwise",
    )
    reasoning: str = Field(
        description="One or two sentences explaining the score and direction",
    )


class LLMMaterialityProvider:
    """Score contract award materiality via an LLM call.

    Falls back to ``HeuristicMaterialityProvider`` when the daily budget
    is exhausted so scoring never silently degrades to zero.

    Args:
        llm: Factory vending ``quick`` and ``deep`` clients.
        daily_budget: Maximum USD spend per calendar day (UTC). Shared
            across all ``score()`` calls on this instance.
    """

    def __init__(
        self,
        llm: LLMClientFactory,
        daily_budget: float = 2.00,
    ) -> None:
        self._llm = llm
        self._daily_budget = daily_budget
        self._fallback = HeuristicMaterialityProvider()
        self._budget_date: date | None = None
        self._spent_today: float = 0.0

    @property
    def name(self) -> str:
        return "llm"

    # ── Budget helpers (same pattern as ClaudeSentimentProvider) ──────────

    def _reset_budget_if_new_day(self) -> None:
        today = datetime.now(UTC).date()
        if self._budget_date != today:
            self._budget_date = today
            self._spent_today = 0.0

    def _budget_exhausted(self) -> bool:
        self._reset_budget_if_new_day()
        return self._spent_today >= self._daily_budget

    def _record_usage(self, input_tokens: int, output_tokens: int) -> None:
        self._spent_today += (
            input_tokens * _INPUT_COST_PER_TOKEN
            + output_tokens * _OUTPUT_COST_PER_TOKEN
        )

    # ── Scoring ───────────────────────────────────────────────────────────

    async def score(
        self,
        award: ContractAwardEvent,
        ticker: str,
        market_cap_usd: float | None = None,
    ) -> MaterialityResult:
        """Score *award* for *ticker*; falls back to heuristic on budget exhaustion."""
        if self._budget_exhausted():
            _logger.warning(
                "LLM materiality budget $%.2f exhausted — falling back to heuristic "
                "for award %s",
                self._daily_budget,
                award.award_id,
            )
            return await self._fallback.score(award, ticker, market_cap_usd)

        try:
            return await self._llm_score(award, ticker, market_cap_usd)
        except Exception:
            _logger.warning(
                "LLM materiality call failed for award %s — falling back to heuristic",
                award.award_id,
                exc_info=True,
            )
            return await self._fallback.score(award, ticker, market_cap_usd)

    async def _llm_score(
        self,
        award: ContractAwardEvent,
        ticker: str,
        market_cap_usd: float | None,
    ) -> MaterialityResult:
        mktcap_str = (
            f"${market_cap_usd:,.0f}" if market_cap_usd else "not available"
        )
        prompt = (
            f"Ticker: {ticker}\n"
            f"Market cap: {mktcap_str}\n"
            f"Award value: ${award.amount_usd:,.0f}\n"
            f"Awarding agency: {award.awarding_agency}"
            + (f" / {award.awarding_sub_agency}" if award.awarding_sub_agency else "")
            + f"\nAward type: {award.award_type}\n"
            f"NAICS: {award.naics_description or 'not provided'}\n"
            f"Description: {(award.description or 'not provided')[:400]}"
        )

        response = await self._llm.quick.invoke(
            prompt,
            system=_SYSTEM,
            response_schema=_LLMMaterialitySchema,
        )
        self._record_usage(response.input_tokens, response.output_tokens)

        parsed = _LLMMaterialitySchema.model_validate_json(response.content)

        direction = _parse_direction(parsed.direction)
        novelty = _parse_novelty(parsed.novelty)

        return MaterialityResult(
            award_id=award.award_id,
            ticker=ticker,
            score=round(parsed.score, 4),
            adjusted_score=round(parsed.score, 4),
            direction=direction,
            novelty=novelty,
            reasoning=parsed.reasoning,
            award_pct_of_market_cap=parsed.award_pct_of_market_cap,
            model_id=response.model_id,
            provider="llm",
            enrichment=None,
            scored_at=datetime.utcnow(),
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_direction(raw: str) -> MaterialityDirection:
    try:
        return MaterialityDirection(raw.lower().strip())
    except ValueError:
        _logger.debug("Unknown direction '%s' — defaulting to neutral", raw)
        return MaterialityDirection.NEUTRAL


def _parse_novelty(raw: str) -> NoveltyClass:
    try:
        return NoveltyClass(raw.lower().strip())
    except ValueError:
        _logger.debug("Unknown novelty '%s' — defaulting to unknown", raw)
        return NoveltyClass.UNKNOWN
