"""Unit tests for EntityResolverAgent.

Verifies that the agent:
  - Resolves each award's recipient name via the injected EntityResolutionService
  - Filters out resolutions below entity_resolution_min_confidence
  - Runs concurrent resolutions (smoke-tested via multiple awards)
  - Handles per-resolution exceptions gracefully (error key, skips award)
  - Returns empty resolutions immediately when no awards are in state

All EntityResolutionService calls are mocked — no real HTTP, LLM, Redis, or DB.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from gov_trade.agents.entity_resolver_agent import EntityResolverAgent
from news_trade.config import GovTradeSettings, Settings
from news_trade.models.contracts import (
    ContractAwardEvent,
    EntityResolution,
    ResolutionLayer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def _make_gov_settings(**kwargs: object) -> GovTradeSettings:
    defaults: dict[str, object] = dict(entity_resolution_min_confidence=0.7)
    return GovTradeSettings(**(defaults | kwargs))  # type: ignore[arg-type]


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


def _make_agent(
    *,
    gov_settings: GovTradeSettings | None = None,
    resolver: object | None = None,
) -> EntityResolverAgent:
    mock_bus = MagicMock()
    mock_resolver = resolver or AsyncMock()
    if resolver is None:
        mock_resolver.resolve = AsyncMock(return_value=_make_resolution())
    return EntityResolverAgent(
        settings=_make_settings(),
        event_bus=mock_bus,  # type: ignore[arg-type]
        gov_settings=gov_settings or _make_gov_settings(),
        resolver=mock_resolver,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Empty input fast-path
# ---------------------------------------------------------------------------


class TestEmptyInput:
    async def test_no_awards_returns_empty_resolutions(self) -> None:
        resolver = AsyncMock()
        resolver.resolve = AsyncMock()
        agent = _make_agent(resolver=resolver)

        result = await agent.run({})

        assert result["resolutions"] == []
        resolver.resolve.assert_not_called()

    async def test_none_contract_awards_treated_as_empty(self) -> None:
        resolver = AsyncMock()
        resolver.resolve = AsyncMock()
        agent = _make_agent(resolver=resolver)

        result = await agent.run({"contract_awards": None})

        assert result["resolutions"] == []
        resolver.resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Successful resolution
# ---------------------------------------------------------------------------


class TestSuccessfulResolution:
    async def test_single_award_above_threshold_is_included(self) -> None:
        award = _make_award()
        resolution = _make_resolution(confidence=0.95)

        resolver = AsyncMock()
        resolver.resolve = AsyncMock(return_value=resolution)
        agent = _make_agent(resolver=resolver)

        result = await agent.run({"contract_awards": [award]})

        assert len(result["resolutions"]) == 1
        assert result["resolutions"][0].ticker == "LMT"

    async def test_resolver_called_with_correct_award_fields(self) -> None:
        award = _make_award(
            recipient_name="BOEING COMPANY",
            award_id="AWARD-XYZ",
            description="Aircraft maintenance services",
            awarding_agency="DEPT OF DEFENSE",
        )
        resolution = _make_resolution(recipient_name="BOEING COMPANY", ticker="BA")

        resolver = AsyncMock()
        resolver.resolve = AsyncMock(return_value=resolution)
        agent = _make_agent(resolver=resolver)

        await agent.run({"contract_awards": [award]})

        resolver.resolve.assert_called_once_with(
            recipient_name="BOEING COMPANY",
            award_id="AWARD-XYZ",
            description="Aircraft maintenance services",
            awarding_agency="DEPT OF DEFENSE",
        )

    async def test_multiple_awards_resolved_concurrently(self) -> None:
        awards = [
            _make_award(award_id=f"AWARD-{i:03d}", recipient_name=f"COMPANY-{i}")
            for i in range(4)
        ]
        resolutions = [
            _make_resolution(
                recipient_name=f"COMPANY-{i}",
                ticker=f"C{i:03d}",
                confidence=0.9,
            )
            for i in range(4)
        ]

        call_count = 0

        async def _resolve(**kwargs: object) -> EntityResolution:
            nonlocal call_count
            idx = call_count
            call_count += 1
            return resolutions[idx]

        resolver = AsyncMock()
        resolver.resolve = _resolve
        agent = _make_agent(resolver=resolver)

        result = await agent.run({"contract_awards": awards})

        assert len(result["resolutions"]) == 4


# ---------------------------------------------------------------------------
# Confidence threshold filtering
# ---------------------------------------------------------------------------


class TestConfidenceFiltering:
    async def test_resolution_below_threshold_is_dropped(self) -> None:
        award = _make_award()
        resolution = _make_resolution(confidence=0.5)  # below default 0.7

        resolver = AsyncMock()
        resolver.resolve = AsyncMock(return_value=resolution)
        gov = _make_gov_settings(entity_resolution_min_confidence=0.7)
        agent = _make_agent(gov_settings=gov, resolver=resolver)

        result = await agent.run({"contract_awards": [award]})

        assert result["resolutions"] == []

    async def test_resolution_at_exact_threshold_is_included(self) -> None:
        award = _make_award()
        resolution = _make_resolution(confidence=0.7)

        resolver = AsyncMock()
        resolver.resolve = AsyncMock(return_value=resolution)
        gov = _make_gov_settings(entity_resolution_min_confidence=0.7)
        agent = _make_agent(gov_settings=gov, resolver=resolver)

        result = await agent.run({"contract_awards": [award]})

        assert len(result["resolutions"]) == 1

    async def test_unresolved_ticker_none_is_dropped(self) -> None:
        award = _make_award()
        resolution = _make_resolution(
            ticker=None, confidence=0.0, layer=ResolutionLayer.NONE
        )

        resolver = AsyncMock()
        resolver.resolve = AsyncMock(return_value=resolution)
        agent = _make_agent(resolver=resolver)

        result = await agent.run({"contract_awards": [award]})

        assert result["resolutions"] == []

    async def test_mixed_confidence_only_high_included(self) -> None:
        awards = [
            _make_award(award_id="A1", recipient_name="HIGH CO"),
            _make_award(award_id="A2", recipient_name="LOW CO"),
        ]
        resolutions = [
            _make_resolution(recipient_name="HIGH CO", ticker="HIGH", confidence=0.95),
            _make_resolution(recipient_name="LOW CO", ticker="LOW", confidence=0.4),
        ]

        call_index = 0

        async def _resolve(**kwargs: object) -> EntityResolution:
            nonlocal call_index
            r = resolutions[call_index]
            call_index += 1
            return r

        resolver = AsyncMock()
        resolver.resolve = _resolve
        gov = _make_gov_settings(entity_resolution_min_confidence=0.7)
        agent = _make_agent(gov_settings=gov, resolver=resolver)

        result = await agent.run({"contract_awards": awards})

        assert len(result["resolutions"]) == 1
        assert result["resolutions"][0].ticker == "HIGH"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_resolution_exception_is_logged_not_raised(self) -> None:
        award = _make_award()

        resolver = AsyncMock()
        resolver.resolve = AsyncMock(side_effect=RuntimeError("EDGAR timeout"))
        agent = _make_agent(resolver=resolver)

        result = await agent.run({"contract_awards": [award]})

        assert result["resolutions"] == []
        assert "errors" in result
        assert len(result["errors"]) == 1

    async def test_partial_failure_keeps_successful_resolutions(self) -> None:
        awards = [
            _make_award(award_id="A1", recipient_name="GOOD CO"),
            _make_award(award_id="A2", recipient_name="BAD CO"),
        ]
        good_resolution = _make_resolution(recipient_name="GOOD CO", ticker="GOOD")

        call_count = 0

        async def _resolve(
            *, recipient_name: str, **kwargs: object
        ) -> EntityResolution:
            nonlocal call_count
            call_count += 1
            if recipient_name == "BAD CO":
                raise ConnectionError("timeout")
            return good_resolution

        resolver = AsyncMock()
        resolver.resolve = _resolve
        agent = _make_agent(resolver=resolver)

        result = await agent.run({"contract_awards": awards})

        assert len(result["resolutions"]) == 1
        assert result["resolutions"][0].ticker == "GOOD"
        assert len(result["errors"]) == 1

    async def test_all_failures_returns_empty_with_errors(self) -> None:
        awards = [_make_award(award_id=f"A{i}") for i in range(3)]

        resolver = AsyncMock()
        resolver.resolve = AsyncMock(side_effect=RuntimeError("total failure"))
        agent = _make_agent(resolver=resolver)

        result = await agent.run({"contract_awards": awards})

        assert result["resolutions"] == []
        assert len(result["errors"]) == 3
