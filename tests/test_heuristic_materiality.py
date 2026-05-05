"""Unit tests for HeuristicMaterialityProvider.

Covers the four checkpoint scenarios from TASKS.md plus edge cases for
the novelty classifier and the absolute-value fallback path.
"""

from __future__ import annotations

from datetime import date

import pytest

from news_trade.models.contracts import (
    ContractAwardEvent,
    MaterialityDirection,
    NoveltyClass,
)
from news_trade.providers.materiality.heuristic_provider import (
    HeuristicMaterialityProvider,
    _classify_novelty,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_award(**kwargs: object) -> ContractAwardEvent:
    defaults: dict[str, object] = dict(
        award_id="AWARD-001",
        recipient_name="ACME DEFENSE INC",
        amount_usd=10_000_000.0,
        awarding_agency="DEPT OF DEFENSE",
        award_type="D",
        sign_date=date(2026, 1, 15),
        description="",
    )
    return ContractAwardEvent(**(defaults | kwargs))  # type: ignore[arg-type]


_PROVIDER = HeuristicMaterialityProvider()


# ---------------------------------------------------------------------------
# Checkpoint 1: award < 1% of market cap → low materiality
# ---------------------------------------------------------------------------


class TestSmallAwardLargeCap:
    async def test_score_below_threshold(self) -> None:
        # $5M award, $2B market cap = 0.25% → negligible
        award = _make_award(amount_usd=5_000_000.0)
        result = await _PROVIDER.score(award, "ACME", market_cap_usd=2_000_000_000.0)

        assert result.score < 3.0

    async def test_direction_is_neutral(self) -> None:
        award = _make_award(amount_usd=5_000_000.0)
        result = await _PROVIDER.score(award, "ACME", market_cap_usd=2_000_000_000.0)

        assert result.direction == MaterialityDirection.NEUTRAL

    async def test_pct_of_market_cap_computed(self) -> None:
        award = _make_award(amount_usd=5_000_000.0)
        result = await _PROVIDER.score(award, "ACME", market_cap_usd=2_000_000_000.0)

        assert result.award_pct_of_market_cap is not None
        assert result.award_pct_of_market_cap == pytest.approx(0.25, rel=0.01)


# ---------------------------------------------------------------------------
# Checkpoint 2: award > 20% of market cap → high materiality
# ---------------------------------------------------------------------------


class TestLargeAwardSmallCap:
    async def test_score_above_seven(self) -> None:
        # $50M award, $200M market cap = 25% → very high
        award = _make_award(amount_usd=50_000_000.0)
        result = await _PROVIDER.score(award, "SMCO", market_cap_usd=200_000_000.0)

        assert result.score >= 7.0

    async def test_direction_is_bullish(self) -> None:
        award = _make_award(amount_usd=50_000_000.0)
        result = await _PROVIDER.score(award, "SMCO", market_cap_usd=200_000_000.0)

        assert result.direction == MaterialityDirection.BULLISH

    async def test_score_capped_at_ten(self) -> None:
        # Extremely large award relative to market cap
        award = _make_award(amount_usd=500_000_000.0)
        result = await _PROVIDER.score(award, "TINY", market_cap_usd=50_000_000.0)

        assert result.score <= 10.0


# ---------------------------------------------------------------------------
# Checkpoint 3: new agency relationship → novelty bonus
# ---------------------------------------------------------------------------


class TestNewCustomerNovelty:
    async def test_definitive_contract_no_keywords_is_new_customer(self) -> None:
        award = _make_award(award_type="D", description="Software development services")
        result = await _PROVIDER.score(award, "TECH", market_cap_usd=500_000_000.0)

        assert result.novelty == NoveltyClass.NEW_CUSTOMER

    async def test_new_customer_score_higher_than_renewal_same_amount(self) -> None:
        new_award = _make_award(
            award_type="D",
            description="Software development services",
            amount_usd=20_000_000.0,
        )
        renewal_award = _make_award(
            award_type="D",
            description="recompete for software development services",
            amount_usd=20_000_000.0,
        )
        new_result = await _PROVIDER.score(
            new_award, "TECH", market_cap_usd=500_000_000.0
        )
        renewal_result = await _PROVIDER.score(
            renewal_award, "TECH", market_cap_usd=500_000_000.0
        )

        assert new_result.score > renewal_result.score


# ---------------------------------------------------------------------------
# Checkpoint 4: renewal with same agency → novelty penalty
# ---------------------------------------------------------------------------


class TestRenewalPenalty:
    async def test_recompete_keyword_classified_as_renewal(self) -> None:
        award = _make_award(description="recompete for IT support services")
        result = await _PROVIDER.score(award, "SAIC", market_cap_usd=8_000_000_000.0)

        assert result.novelty == NoveltyClass.RENEWAL

    async def test_follow_on_classified_as_renewal(self) -> None:
        award = _make_award(description="follow-on contract for logistics support")
        result = await _PROVIDER.score(award, "KBR", market_cap_usd=3_000_000_000.0)

        assert result.novelty == NoveltyClass.RENEWAL

    async def test_renewal_score_lower_than_base(self) -> None:
        base_award = _make_award(
            award_type="D",
            description="new contract for cybersecurity services",
            amount_usd=50_000_000.0,
        )
        renewal_award = _make_award(
            award_type="D",
            description="recompete for cybersecurity services",
            amount_usd=50_000_000.0,
        )
        base_result = await _PROVIDER.score(
            base_award, "CRWD", market_cap_usd=2_000_000_000.0
        )
        renewal_result = await _PROVIDER.score(
            renewal_award, "CRWD", market_cap_usd=2_000_000_000.0
        )

        assert renewal_result.score < base_result.score


# ---------------------------------------------------------------------------
# Novelty classifier edge cases
# ---------------------------------------------------------------------------


class TestNoveltyClassifier:
    def test_delivery_order_without_keywords_is_expansion(self) -> None:
        award = _make_award(
            award_type="C", description="delivery of hardware components"
        )
        assert _classify_novelty(award) == NoveltyClass.EXPANSION

    def test_option_period_keyword_is_renewal(self) -> None:
        award = _make_award(description="exercise of option period 2")
        assert _classify_novelty(award) == NoveltyClass.RENEWAL

    def test_modification_keyword_is_expansion(self) -> None:
        award = _make_award(description="modification to add additional scope")
        assert _classify_novelty(award) == NoveltyClass.EXPANSION

    def test_unknown_type_no_keywords_is_unknown(self) -> None:
        award = _make_award(award_type="B", description="purchase of spare parts")
        assert _classify_novelty(award) == NoveltyClass.UNKNOWN


# ---------------------------------------------------------------------------
# Absolute-value fallback (no market cap)
# ---------------------------------------------------------------------------


class TestNoMarketCap:
    async def test_large_absolute_award_is_bullish(self) -> None:
        award = _make_award(amount_usd=200_000_000.0)
        result = await _PROVIDER.score(award, "LDOS", market_cap_usd=None)

        assert result.direction == MaterialityDirection.BULLISH
        assert result.award_pct_of_market_cap is None

    async def test_small_absolute_award_is_neutral(self) -> None:
        # award_type="B" → novelty=UNKNOWN (0.0 adj); absolute score=2.0 → NEUTRAL
        award = _make_award(amount_usd=6_000_000.0, award_type="B")
        result = await _PROVIDER.score(award, "TINY", market_cap_usd=None)

        assert result.direction == MaterialityDirection.NEUTRAL


# ---------------------------------------------------------------------------
# Result structure
# ---------------------------------------------------------------------------


class TestResultStructure:
    async def test_award_id_preserved(self) -> None:
        award = _make_award(award_id="CONT_AWD_XYZ_123")
        result = await _PROVIDER.score(award, "LMT", market_cap_usd=100_000_000_000.0)

        assert result.award_id == "CONT_AWD_XYZ_123"

    async def test_ticker_preserved(self) -> None:
        award = _make_award()
        result = await _PROVIDER.score(award, "NOC", market_cap_usd=50_000_000_000.0)

        assert result.ticker == "NOC"

    async def test_provider_label(self) -> None:
        award = _make_award()
        result = await _PROVIDER.score(award, "GD")

        assert result.provider == "heuristic"

    async def test_score_equals_adjusted_score_before_enrichment(self) -> None:
        award = _make_award(amount_usd=100_000_000.0)
        result = await _PROVIDER.score(award, "BAH", market_cap_usd=1_000_000_000.0)

        assert result.score == result.adjusted_score

    async def test_enrichment_is_none_before_agent_applies_it(self) -> None:
        award = _make_award()
        result = await _PROVIDER.score(award, "CACI")

        assert result.enrichment is None
