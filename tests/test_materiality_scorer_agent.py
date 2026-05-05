"""Unit tests for MaterialityScorerAgent.

Verifies that the agent:
  - Scores each resolved award via the injected MaterialityProvider
  - Applies the lobbying enrichment multiplier (adjusted_score = score x multiplier)
  - Filters out results below _MIN_SCORE (3.0)
  - Returns empty immediately when no resolutions are in state
  - Handles per-award scoring exceptions gracefully
  - Proceeds with None market cap when yfinance is unavailable

The materiality provider, enrichment service, and yfinance are all mocked.
No real HTTP, no real LLM, no real database.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gov_trade.agents.materiality_scorer import _MIN_SCORE, MaterialityScorerAgent
from news_trade.config import GovTradeSettings, Settings
from news_trade.models.contracts import (
    ContractAwardEvent,
    EntityResolution,
    MaterialityDirection,
    MaterialityResult,
    NoveltyClass,
    ResolutionLayer,
)
from news_trade.models.lobbying import EnrichmentResult, LobbyingTrend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def _make_gov_settings(**kwargs: object) -> GovTradeSettings:
    return GovTradeSettings(**kwargs)  # type: ignore[arg-type]


def _make_award(**kwargs: object) -> ContractAwardEvent:
    defaults: dict[str, object] = dict(
        award_id="AWARD-001",
        recipient_name="LOCKHEED MARTIN CORPORATION",
        amount_usd=50_000_000.0,
        awarding_agency="DEPT OF DEFENSE",
        award_type="D",
        sign_date=datetime.utcnow().date(),
    )
    return ContractAwardEvent(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_resolution(**kwargs: object) -> EntityResolution:
    defaults: dict[str, object] = dict(
        recipient_name="LOCKHEED MARTIN CORPORATION",
        ticker="LMT",
        exchange="NYSE",
        confidence=1.0,
        layer=ResolutionLayer.STATIC,
    )
    return EntityResolution(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_materiality_result(**kwargs: object) -> MaterialityResult:
    defaults: dict[str, object] = dict(
        award_id="AWARD-001",
        ticker="LMT",
        score=6.0,
        adjusted_score=6.0,
        direction=MaterialityDirection.BULLISH,
        novelty=NoveltyClass.RENEWAL,
    )
    return MaterialityResult(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_enrichment(**kwargs: object) -> EnrichmentResult:
    defaults: dict[str, object] = dict(
        multiplier=1.0,
        trend=LobbyingTrend.FLAT,
        rationale="flat spend",
        ticker="LMT",
        awarding_agency="DEPT OF DEFENSE",
    )
    return EnrichmentResult(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_agent(
    *,
    gov_settings: GovTradeSettings | None = None,
    provider: object | None = None,
    enrichment: object | None = None,
) -> MaterialityScorerAgent:
    mock_bus = MagicMock()

    mock_provider = provider or AsyncMock()
    if provider is None:
        mock_provider.score = AsyncMock(return_value=_make_materiality_result())

    mock_enrichment = enrichment or AsyncMock()
    if enrichment is None:
        mock_enrichment.get_multiplier = AsyncMock(
            return_value=_make_enrichment(multiplier=1.0)
        )

    return MaterialityScorerAgent(
        settings=_make_settings(),
        event_bus=mock_bus,  # type: ignore[arg-type]
        gov_settings=gov_settings or _make_gov_settings(),
        provider=mock_provider,  # type: ignore[arg-type]
        enrichment=mock_enrichment,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Empty input fast-path
# ---------------------------------------------------------------------------


class TestEmptyInput:
    async def test_no_resolutions_returns_empty_results(self) -> None:
        provider = AsyncMock()
        provider.score = AsyncMock()
        agent = _make_agent(provider=provider)

        result = await agent.run({"contract_awards": [], "resolutions": []})

        assert result["materiality_results"] == []
        provider.score.assert_not_called()

    async def test_none_resolutions_treated_as_empty(self) -> None:
        provider = AsyncMock()
        provider.score = AsyncMock()
        agent = _make_agent(provider=provider)

        result = await agent.run({"contract_awards": [], "resolutions": None})

        assert result["materiality_results"] == []
        provider.score.assert_not_called()


# ---------------------------------------------------------------------------
# Materiality threshold filtering
# ---------------------------------------------------------------------------


class TestThresholdFiltering:
    async def test_award_above_min_score_is_included(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        base_result = _make_materiality_result(score=6.0, adjusted_score=6.0)

        provider = AsyncMock()
        provider.score = AsyncMock(return_value=base_result)
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(
            return_value=_make_enrichment(multiplier=1.0)
        )

        _patch = patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap", return_value=None
        )
        with _patch:
            agent = _make_agent(provider=provider, enrichment=enrichment)
            result = await agent.run(
                {"contract_awards": [award], "resolutions": [resolution]}
            )

        assert len(result["materiality_results"]) == 1

    async def test_award_below_min_score_is_dropped(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        base_result = _make_materiality_result(score=2.0, adjusted_score=2.0)

        provider = AsyncMock()
        provider.score = AsyncMock(return_value=base_result)
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(
            return_value=_make_enrichment(multiplier=1.0)
        )

        _patch = patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap", return_value=None
        )
        with _patch:
            agent = _make_agent(provider=provider, enrichment=enrichment)
            result = await agent.run(
                {"contract_awards": [award], "resolutions": [resolution]}
            )

        assert result["materiality_results"] == []

    async def test_award_at_exact_min_score_boundary_is_included(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        base_result = _make_materiality_result(
            score=_MIN_SCORE, adjusted_score=_MIN_SCORE
        )

        provider = AsyncMock()
        provider.score = AsyncMock(return_value=base_result)
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(
            return_value=_make_enrichment(multiplier=1.0)
        )

        _patch = patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap", return_value=None
        )
        with _patch:
            agent = _make_agent(provider=provider, enrichment=enrichment)
            result = await agent.run(
                {"contract_awards": [award], "resolutions": [resolution]}
            )

        assert len(result["materiality_results"]) == 1


# ---------------------------------------------------------------------------
# Lobbying enrichment multiplier
# ---------------------------------------------------------------------------


class TestEnrichmentMultiplier:
    async def test_multiplier_applied_to_base_score(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        base_result = _make_materiality_result(score=4.0, adjusted_score=4.0)

        provider = AsyncMock()
        provider.score = AsyncMock(return_value=base_result)
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(
            return_value=_make_enrichment(multiplier=1.5)
        )

        _patch = patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap", return_value=None
        )
        with _patch:
            agent = _make_agent(provider=provider, enrichment=enrichment)
            result = await agent.run(
                {"contract_awards": [award], "resolutions": [resolution]}
            )

        assert len(result["materiality_results"]) == 1
        assert result["materiality_results"][0].adjusted_score == pytest.approx(6.0)

    async def test_multiplier_below_threshold_drops_award(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        # base score 4.0 x 0.5 multiplier = 2.0 -> below _MIN_SCORE 3.0
        base_result = _make_materiality_result(score=4.0, adjusted_score=4.0)

        provider = AsyncMock()
        provider.score = AsyncMock(return_value=base_result)
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(
            return_value=_make_enrichment(multiplier=0.5)
        )

        _patch = patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap", return_value=None
        )
        with _patch:
            agent = _make_agent(provider=provider, enrichment=enrichment)
            result = await agent.run(
                {"contract_awards": [award], "resolutions": [resolution]}
            )

        assert result["materiality_results"] == []

    async def test_enrichment_result_attached_to_materiality_result(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        base_result = _make_materiality_result(score=5.0, adjusted_score=5.0)
        enrichment_obj = _make_enrichment(
            multiplier=1.3, trend=LobbyingTrend.INCREASING
        )

        provider = AsyncMock()
        provider.score = AsyncMock(return_value=base_result)
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(return_value=enrichment_obj)

        _patch = patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap", return_value=None
        )
        with _patch:
            agent = _make_agent(provider=provider, enrichment=enrichment)
            result = await agent.run(
                {"contract_awards": [award], "resolutions": [resolution]}
            )

        assert result["materiality_results"][0].enrichment == enrichment_obj

    async def test_neutral_multiplier_leaves_score_unchanged(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        base_result = _make_materiality_result(score=5.0, adjusted_score=5.0)

        provider = AsyncMock()
        provider.score = AsyncMock(return_value=base_result)
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(
            return_value=_make_enrichment(multiplier=1.0)
        )

        _patch = patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap", return_value=None
        )
        with _patch:
            agent = _make_agent(provider=provider, enrichment=enrichment)
            result = await agent.run(
                {"contract_awards": [award], "resolutions": [resolution]}
            )

        assert result["materiality_results"][0].adjusted_score == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Market cap fetch resilience
# ---------------------------------------------------------------------------


class TestMarketCapFetch:
    async def test_none_market_cap_does_not_abort_scoring(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        base_result = _make_materiality_result(score=5.0, adjusted_score=5.0)

        provider = AsyncMock()
        provider.score = AsyncMock(return_value=base_result)
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(return_value=_make_enrichment())

        with patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap", return_value=None
        ):
            agent = _make_agent(provider=provider, enrichment=enrichment)
            result = await agent.run(
                {"contract_awards": [award], "resolutions": [resolution]}
            )

        assert len(result["materiality_results"]) == 1

    async def test_market_cap_passed_to_provider(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        base_result = _make_materiality_result(score=5.0, adjusted_score=5.0)

        captured: dict[str, object] = {}

        async def _score(
            award: ContractAwardEvent, ticker: str, market_cap_usd: float | None = None
        ) -> MaterialityResult:
            captured["market_cap_usd"] = market_cap_usd
            return base_result

        provider = AsyncMock()
        provider.score = _score
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(return_value=_make_enrichment())

        with patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap",
            return_value=2_000_000_000.0,
        ):
            agent = _make_agent(provider=provider, enrichment=enrichment)
            await agent.run({"contract_awards": [award], "resolutions": [resolution]})

        assert captured["market_cap_usd"] == pytest.approx(2_000_000_000.0)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_provider_exception_logged_not_raised(self) -> None:
        award = _make_award()
        resolution = _make_resolution()

        provider = AsyncMock()
        provider.score = AsyncMock(side_effect=RuntimeError("LLM timeout"))
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(return_value=_make_enrichment())

        _patch = patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap", return_value=None
        )
        with _patch:
            agent = _make_agent(provider=provider, enrichment=enrichment)
            result = await agent.run(
                {"contract_awards": [award], "resolutions": [resolution]}
            )

        assert result["materiality_results"] == []
        assert "errors" in result
        assert len(result["errors"]) == 1
        assert "LMT" in result["errors"][0]

    async def test_partial_failure_keeps_successful_results(self) -> None:
        award_ok = _make_award(award_id="OK", recipient_name="GOOD CO")
        award_bad = _make_award(award_id="BAD", recipient_name="BAD CO")
        resolution_ok = _make_resolution(recipient_name="GOOD CO", ticker="GOOD")
        resolution_bad = _make_resolution(recipient_name="BAD CO", ticker="BAD")

        good_result = _make_materiality_result(
            award_id="OK", ticker="GOOD", score=5.0, adjusted_score=5.0
        )

        call_count = 0

        async def _score(
            award: ContractAwardEvent, ticker: str, market_cap_usd: float | None = None
        ) -> MaterialityResult:
            nonlocal call_count
            call_count += 1
            if ticker == "BAD":
                raise ValueError("bad data")
            return good_result

        provider = AsyncMock()
        provider.score = _score
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(return_value=_make_enrichment())

        _patch = patch(
            "gov_trade.agents.materiality_scorer._fetch_market_cap", return_value=None
        )
        with _patch:
            agent = _make_agent(provider=provider, enrichment=enrichment)
            result = await agent.run(
                {
                    "contract_awards": [award_ok, award_bad],
                    "resolutions": [resolution_ok, resolution_bad],
                }
            )

        assert len(result["materiality_results"]) == 1
        assert result["materiality_results"][0].ticker == "GOOD"
        assert len(result["errors"]) == 1

    async def test_unmatched_resolution_is_skipped_silently(self) -> None:
        # resolution has no matching award in contract_awards
        resolution = _make_resolution(recipient_name="ORPHAN CO", ticker="ORP")

        provider = AsyncMock()
        provider.score = AsyncMock()
        enrichment = AsyncMock()
        enrichment.get_multiplier = AsyncMock(return_value=_make_enrichment())

        agent = _make_agent(provider=provider, enrichment=enrichment)
        result = await agent.run(
            {"contract_awards": [], "resolutions": [resolution]}
        )

        assert result["materiality_results"] == []
        provider.score.assert_not_called()
