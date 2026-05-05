"""Unit tests for EntityResolutionService — all three layers.

Tests use:
  - A real ContractorLookup backed by the bundled contractor_tickers.csv
    for Layer 1 (exact and alias hits).
  - AsyncMock for the EdgarProvider (Layer 2).
  - AsyncMock for LLMClientFactory.quick (Layer 3).
  - MagicMock for the Redis client and SQLAlchemy session.

No real HTTP, no real LLM, no real Redis.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from news_trade.config import GovTradeSettings
from news_trade.models.contracts import ResolutionLayer
from news_trade.services.contractor_lookup import ContractorLookup
from news_trade.services.entity_resolver import EntityResolutionService
from news_trade.services.llm_client import LLMResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs: object) -> GovTradeSettings:
    defaults: dict[str, object] = dict(entity_resolution_min_confidence=0.7)
    return GovTradeSettings(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_service(
    *,
    edgar: object = None,
    llm_factory: object = None,
    redis_client: object = None,
    session: object = None,
    settings: GovTradeSettings | None = None,
) -> EntityResolutionService:
    return EntityResolutionService(
        lookup=ContractorLookup(),
        edgar=edgar,  # type: ignore[arg-type]
        llm=llm_factory,  # type: ignore[arg-type]
        redis_client=redis_client,  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
        settings=settings or _make_settings(),
    )


def _llm_response(
    ticker: str, confidence: float, reasoning: str = "test"
) -> LLMResponse:
    payload = json.dumps(
        {"ticker": ticker, "exchange": "NYSE", "confidence": confidence,
         "reasoning": reasoning}
    )
    return LLMResponse(content=payload, model_id="test-model", provider="anthropic")


# ---------------------------------------------------------------------------
# Layer 1 — static lookup
# ---------------------------------------------------------------------------


class TestLayer1Static:
    async def test_exact_canonical_name_resolves(self) -> None:
        svc = _make_service()
        result = await svc.resolve("LOCKHEED MARTIN CORPORATION")

        assert result.ticker == "LMT"
        assert result.layer == ResolutionLayer.STATIC
        assert result.confidence == 1.0

    async def test_alias_resolves_to_same_ticker(self) -> None:
        svc = _make_service()
        result = await svc.resolve("BOEING DEFENSE SPACE & SECURITY")

        assert result.ticker == "BA"
        assert result.layer == ResolutionLayer.STATIC
        assert result.confidence == 1.0

    async def test_layer1_hit_does_not_call_edgar(self) -> None:
        edgar = AsyncMock()
        svc = _make_service(edgar=edgar)

        await svc.resolve("PALANTIR TECHNOLOGIES INC - FEDERAL")

        edgar.lookup.assert_not_called()

    async def test_layer1_hit_does_not_call_llm(self) -> None:
        llm_factory = MagicMock()
        svc = _make_service(llm_factory=llm_factory)

        await svc.resolve("BOOZ ALLEN HAMILTON HOLDING CORPORATION")

        llm_factory.quick.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Layer 2 — EDGAR
# ---------------------------------------------------------------------------


class TestLayer2Edgar:
    async def test_layer1_miss_falls_through_to_edgar(self) -> None:
        edgar = AsyncMock()
        edgar.lookup.return_value = ("EXMPL", "NASDAQ")
        svc = _make_service(edgar=edgar)

        result = await svc.resolve("SOME UNKNOWN MID-CAP CONTRACTOR INC")

        assert result.ticker == "EXMPL"
        assert result.layer == ResolutionLayer.EDGAR
        assert result.confidence == 0.9
        edgar.lookup.assert_called_once_with("SOME UNKNOWN MID-CAP CONTRACTOR INC")

    async def test_edgar_none_falls_through_to_layer3(self) -> None:
        edgar = AsyncMock()
        edgar.lookup.return_value = None

        llm_factory = MagicMock()
        llm_factory.quick.invoke = AsyncMock(
            return_value=_llm_response("EXMPL", 0.85)
        )

        svc = _make_service(edgar=edgar, llm_factory=llm_factory)
        result = await svc.resolve("OBSCURE CONTRACTOR LLC")

        assert result.layer == ResolutionLayer.LLM
        llm_factory.quick.invoke.assert_called_once()

    async def test_edgar_exception_falls_through_gracefully(self) -> None:
        edgar = AsyncMock()
        edgar.lookup.side_effect = ConnectionError("EDGAR down")

        llm_factory = MagicMock()
        llm_factory.quick.invoke = AsyncMock(
            return_value=_llm_response("EXMPL", 0.85)
        )

        svc = _make_service(edgar=edgar, llm_factory=llm_factory)
        result = await svc.resolve("SOME COMPANY INC")

        assert result.ticker == "EXMPL"
        assert result.layer == ResolutionLayer.LLM


# ---------------------------------------------------------------------------
# Layer 3 — LLM fallback
# ---------------------------------------------------------------------------


class TestLayer3LLM:
    async def test_llm_hit_above_threshold_resolves(self) -> None:
        llm_factory = MagicMock()
        llm_factory.quick.invoke = AsyncMock(
            return_value=_llm_response("KTOS", 0.80, "Kratos Defense subsidiary")
        )

        svc = _make_service(llm_factory=llm_factory)
        # Use a name not present in the static table or any alias
        result = await svc.resolve(
            "GREYWOLF AUTONOMOUS SYSTEMS INC",
            description="UAV development contract",
            awarding_agency="DEPT OF DEFENSE",
        )

        assert result.ticker == "KTOS"
        assert result.layer == ResolutionLayer.LLM
        assert result.confidence == 0.80
        assert "Kratos" in (result.reasoning or "")

    async def test_llm_confidence_below_threshold_returns_none(self) -> None:
        llm_factory = MagicMock()
        llm_factory.quick.invoke = AsyncMock(
            return_value=_llm_response("MAYBE", 0.40)
        )

        svc = _make_service(
            llm_factory=llm_factory,
            settings=_make_settings(entity_resolution_min_confidence=0.7),
        )
        result = await svc.resolve("VAGUE ENTERPRISE SOLUTIONS LLC")

        assert result.ticker is None
        assert result.layer == ResolutionLayer.NONE

    async def test_llm_empty_ticker_returns_none(self) -> None:
        llm_factory = MagicMock()
        llm_factory.quick.invoke = AsyncMock(
            return_value=_llm_response("", 0.95, "company is private")
        )

        svc = _make_service(llm_factory=llm_factory)
        result = await svc.resolve("PRIVATE HOLDINGS GROUP LLC")

        assert result.ticker is None
        assert result.layer == ResolutionLayer.NONE

    async def test_llm_exception_returns_none(self) -> None:
        llm_factory = MagicMock()
        llm_factory.quick.invoke = AsyncMock(side_effect=RuntimeError("API error"))

        svc = _make_service(llm_factory=llm_factory)
        result = await svc.resolve("BROKEN COMPANY INC")

        assert result.ticker is None
        assert result.layer == ResolutionLayer.NONE


# ---------------------------------------------------------------------------
# All layers fail
# ---------------------------------------------------------------------------


class TestAllLayersFail:
    async def test_no_providers_returns_none_resolution(self) -> None:
        svc = _make_service()
        result = await svc.resolve("COMPLETELY UNKNOWN PRIVATE FIRM LLC")

        assert result.ticker is None
        assert result.layer == ResolutionLayer.NONE
        assert result.confidence == 0.0

    async def test_none_resolution_has_correct_recipient_name(self) -> None:
        svc = _make_service()
        name = "XYZ UNKNOWN CORP"
        result = await svc.resolve(name)

        assert result.recipient_name == name


# ---------------------------------------------------------------------------
# Redis negative-result cache
# ---------------------------------------------------------------------------


class TestRedisMissCache:
    async def test_cached_miss_skips_edgar(self) -> None:
        redis_client = AsyncMock()
        redis_client.exists = AsyncMock(return_value=1)

        edgar = AsyncMock()
        svc = _make_service(edgar=edgar, redis_client=redis_client)

        result = await svc.resolve("SOME UNRESOLVABLE COMPANY INC")

        assert result.ticker is None
        assert result.layer == ResolutionLayer.NONE
        edgar.lookup.assert_not_called()

    async def test_miss_is_cached_after_all_layers_fail(self) -> None:
        redis_client = AsyncMock()
        redis_client.exists = AsyncMock(return_value=0)
        redis_client.set = AsyncMock()

        svc = _make_service(redis_client=redis_client)
        await svc.resolve("COMPLETELY UNKNOWN PRIVATE FIRM LLC")

        redis_client.set.assert_called_once()
        call_kwargs = redis_client.set.call_args
        assert call_kwargs.kwargs.get("ex") == 60 * 60 * 24 * 7

    async def test_layer1_hit_does_not_cache_miss(self) -> None:
        redis_client = AsyncMock()
        redis_client.exists = AsyncMock(return_value=0)
        redis_client.set = AsyncMock()

        svc = _make_service(redis_client=redis_client)
        await svc.resolve("LOCKHEED MARTIN CORPORATION")

        redis_client.set.assert_not_called()

    async def test_redis_down_does_not_block_resolution(self) -> None:
        redis_client = AsyncMock()
        redis_client.exists = AsyncMock(side_effect=ConnectionError("Redis down"))

        svc = _make_service(redis_client=redis_client)
        result = await svc.resolve("LOCKHEED MARTIN CORPORATION")

        assert result.ticker == "LMT"
        assert result.layer == ResolutionLayer.STATIC


# ---------------------------------------------------------------------------
# DB logging
# ---------------------------------------------------------------------------


class TestDBLogging:
    async def test_successful_resolution_is_logged(self) -> None:
        session = MagicMock()
        svc = _make_service(session=session)

        await svc.resolve("LOCKHEED MARTIN CORPORATION", award_id="AWARD-001")

        session.add.assert_called_once()
        session.commit.assert_called_once()
        row = session.add.call_args[0][0]
        assert row.award_id == "AWARD-001"
        assert row.ticker == "LMT"
        assert row.layer == "static"

    async def test_failed_resolution_is_logged_with_none_ticker(self) -> None:
        session = MagicMock()
        svc = _make_service(session=session)

        await svc.resolve("UNKNOWN PRIVATE FIRM LLC")

        session.add.assert_called_once()
        row = session.add.call_args[0][0]
        assert row.ticker is None
        assert row.layer == "none"

    async def test_session_none_skips_logging_silently(self) -> None:
        svc = _make_service(session=None)
        result = await svc.resolve("LOCKHEED MARTIN CORPORATION")

        assert result.ticker == "LMT"


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    async def test_unexpected_exception_returns_none_resolution(self) -> None:
        lookup = MagicMock()
        lookup.lookup.side_effect = RuntimeError("disk full")

        svc = EntityResolutionService(
            lookup=lookup,
            edgar=None,
            llm=None,
            redis_client=None,
            session=None,
            settings=_make_settings(),
        )
        result = await svc.resolve("ANY COMPANY")

        assert result.ticker is None
        assert result.layer == ResolutionLayer.NONE
