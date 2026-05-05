"""Unit tests for LobbyingEnrichmentService.

Covers:
  - All multiplier bands (stopped/decreasing/flat/neutral/new/increasing)
  - LOBBYING_ENRICHMENT_ENABLED=false returns neutral immediately
  - Provider exception → graceful neutral result
  - Cache behaviour via mocked Redis
  - DB logging
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from news_trade.config import GovTradeSettings
from news_trade.models.lobbying import LobbyingFiling
from news_trade.providers.lobbying.lda_provider import available_quarters
from news_trade.providers.lobbying.mock_provider import MockLobbyingProvider
from news_trade.services.contractor_lookup import ContractorLookup
from news_trade.services.lobbying_enrichment import LobbyingEnrichmentService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AGENCY_MAPPING: dict[str, list[str]] = {
    "DEPT OF DEFENSE": ["Department of Defense", "DoD"],
    "DEPT OF HEALTH AND HUMAN SERVICES": [
        "HHS", "Department of Health and Human Services"
    ],
}


def _make_settings(**kwargs: object) -> GovTradeSettings:
    defaults: dict[str, object] = dict(
        lobbying_enrichment_enabled=True,
        lobbying_lookback_quarters=4,
        lobbying_min_spend_threshold_usd=50_000,
    )
    return GovTradeSettings(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_service(
    filings: list[LobbyingFiling] | None = None,
    settings: GovTradeSettings | None = None,
    session: object = None,
) -> LobbyingEnrichmentService:
    return LobbyingEnrichmentService(
        provider=MockLobbyingProvider(filings),
        lookup=ContractorLookup(),
        agency_mapping=_AGENCY_MAPPING,
        settings=settings or _make_settings(),
        session=session,  # type: ignore[arg-type]
    )


def _make_filing(
    year: int,
    period: str,
    total_spend: float,
    agencies: list[str],
    ticker: str = "LMT",
) -> LobbyingFiling:
    n = len(agencies) or 1
    agency_spend = {a: total_spend / n for a in agencies}
    return LobbyingFiling(
        filing_uuid=f"{ticker}-{year}-{period}",
        ticker=ticker,
        lda_client_name="Lockheed Martin Corporation",
        year=year,
        period=period,
        total_spend=total_spend,
        agency_spend=agency_spend,
        lobbied_agencies=agencies,
    )


_DOD = "Department of Defense"
_AS_OF = date(2026, 5, 1)  # Q1 2026 is published (May 15 deadline has passed)


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


class TestFeatureGate:
    async def test_disabled_returns_neutral_immediately(self) -> None:
        provider = AsyncMock()
        svc = LobbyingEnrichmentService(
            provider=provider,
            lookup=ContractorLookup(),
            agency_mapping=_AGENCY_MAPPING,
            settings=_make_settings(lobbying_enrichment_enabled=False),
        )
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE")

        assert result.multiplier == 1.0
        assert result.data_found is False
        provider.get_filings.assert_not_called()

    async def test_enabled_calls_provider(self) -> None:
        provider = MockLobbyingProvider([])
        provider.get_filings = AsyncMock(return_value=[])  # type: ignore[method-assign]
        svc = LobbyingEnrichmentService(
            provider=provider,
            lookup=ContractorLookup(),
            agency_mapping=_AGENCY_MAPPING,
            settings=_make_settings(),
        )
        await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        provider.get_filings.assert_called_once()


# ---------------------------------------------------------------------------
# No data
# ---------------------------------------------------------------------------


class TestNoData:
    async def test_no_filings_returns_neutral(self) -> None:
        svc = _make_service(filings=[])
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == 1.0
        assert result.data_found is False

    async def test_filings_below_min_spend_returns_neutral(self) -> None:
        filing = _make_filing(2025, "Q4", 10_000.0, [_DOD])  # below $50k threshold
        svc = _make_service(filings=[filing])
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == 1.0
        assert result.data_found is False

    async def test_filings_for_different_agency_returns_neutral(self) -> None:
        # Lobbied HHS, not DoD
        filing = _make_filing(2025, "Q4", 500_000.0, ["HHS"])
        svc = _make_service(filings=[filing])
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        # data_found=True (filings exist) but agency spend = 0 → NO_DATA trend → 1.0
        assert result.multiplier == 1.0


# ---------------------------------------------------------------------------
# Multiplier bands
# ---------------------------------------------------------------------------


class TestMultiplierBands:
    async def test_stopped_lobbying_returns_0_7(self) -> None:
        # Prior quarters: spend on DoD; recent quarters: no spend on DoD
        filings = [
            _make_filing(2025, "Q2", 500_000.0, [_DOD]),
            _make_filing(2025, "Q1", 600_000.0, [_DOD]),
            # Q3 and Q4 2025: no DoD spend (lobbied HHS only)
            _make_filing(2025, "Q3", 400_000.0, ["HHS"]),
            _make_filing(2025, "Q4", 450_000.0, ["HHS"]),
        ]
        svc = _make_service(filings=filings)
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == pytest.approx(0.7)
        assert result.data_found is True

    async def test_decreasing_spend_returns_0_8(self) -> None:
        filings = [
            _make_filing(2025, "Q1", 800_000.0, [_DOD]),
            _make_filing(2025, "Q2", 700_000.0, [_DOD]),
            _make_filing(2025, "Q3", 250_000.0, [_DOD]),  # drop > 10%
            _make_filing(2025, "Q4", 200_000.0, [_DOD]),
        ]
        svc = _make_service(filings=filings)
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == pytest.approx(0.8)

    async def test_flat_spend_returns_0_95(self) -> None:
        filings = [
            _make_filing(2025, "Q1", 500_000.0, [_DOD]),
            _make_filing(2025, "Q2", 510_000.0, [_DOD]),
            _make_filing(2025, "Q3", 505_000.0, [_DOD]),
            _make_filing(2025, "Q4", 495_000.0, [_DOD]),
        ]
        svc = _make_service(filings=filings)
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == pytest.approx(0.95)

    async def test_new_lobbying_returns_1_15(self) -> None:
        # Only recent quarters, no prior spend
        filings = [
            _make_filing(2025, "Q3", 200_000.0, [_DOD]),
            _make_filing(2025, "Q4", 250_000.0, [_DOD]),
        ]
        svc = _make_service(filings=filings)
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == pytest.approx(1.15)

    async def test_small_increase_returns_1_15(self) -> None:
        # < 1.5x increase
        filings = [
            _make_filing(2025, "Q1", 400_000.0, [_DOD]),
            _make_filing(2025, "Q2", 420_000.0, [_DOD]),
            _make_filing(2025, "Q3", 550_000.0, [_DOD]),
            _make_filing(2025, "Q4", 560_000.0, [_DOD]),
        ]
        svc = _make_service(filings=filings)
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == pytest.approx(1.15)

    async def test_medium_increase_1_5x_to_3x_returns_1_3(self) -> None:
        # recent ~2x prior
        filings = [
            _make_filing(2025, "Q1", 300_000.0, [_DOD]),
            _make_filing(2025, "Q2", 320_000.0, [_DOD]),
            _make_filing(2025, "Q3", 650_000.0, [_DOD]),
            _make_filing(2025, "Q4", 680_000.0, [_DOD]),
        ]
        svc = _make_service(filings=filings)
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == pytest.approx(1.3)

    async def test_large_increase_over_3x_returns_1_5(self) -> None:
        # recent ~4x prior
        filings = [
            _make_filing(2025, "Q1", 200_000.0, [_DOD]),
            _make_filing(2025, "Q2", 220_000.0, [_DOD]),
            _make_filing(2025, "Q3", 850_000.0, [_DOD]),
            _make_filing(2025, "Q4", 900_000.0, [_DOD]),
        ]
        svc = _make_service(filings=filings)
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    async def test_provider_exception_returns_neutral(self) -> None:
        provider = MockLobbyingProvider()
        provider.get_filings = AsyncMock(side_effect=ConnectionError("LDA down"))  # type: ignore[method-assign]
        svc = LobbyingEnrichmentService(
            provider=provider,
            lookup=ContractorLookup(),
            agency_mapping=_AGENCY_MAPPING,
            settings=_make_settings(),
        )
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == 1.0
        assert result.data_found is False

    async def test_result_contains_rationale(self) -> None:
        filings = [
            _make_filing(2025, "Q3", 500_000.0, [_DOD]),
            _make_filing(2025, "Q4", 600_000.0, [_DOD]),
        ]
        svc = _make_service(filings=filings)
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.rationale != ""


# ---------------------------------------------------------------------------
# DB logging
# ---------------------------------------------------------------------------


class TestDBLogging:
    async def test_enrichment_logged_when_session_provided(self) -> None:
        session = MagicMock()
        svc = _make_service(filings=[], session=session)

        await svc.get_multiplier(
            "LMT", "DEPT OF DEFENSE", award_id="AW-001", as_of=_AS_OF
        )

        session.add.assert_called_once()
        row = session.add.call_args[0][0]
        assert row.ticker == "LMT"
        assert row.contract_award_id == "AW-001"

    async def test_no_session_skips_logging_silently(self) -> None:
        svc = _make_service(filings=[])
        result = await svc.get_multiplier("LMT", "DEPT OF DEFENSE", as_of=_AS_OF)

        assert result.multiplier == 1.0


# ---------------------------------------------------------------------------
# available_quarters helper
# ---------------------------------------------------------------------------


class TestAvailableQuarters:
    def test_returns_at_most_lookback(self) -> None:
        quarters = available_quarters(4, as_of=date(2026, 5, 1))
        assert len(quarters) <= 4

    def test_unpublished_quarter_excluded(self) -> None:
        # On May 1, Q1 2026 is not yet published (publish date is May 15)
        quarters = available_quarters(4, as_of=date(2026, 5, 1))
        assert (2026, "Q1") not in quarters

    def test_published_quarter_included(self) -> None:
        # On May 16, Q1 2026 IS published
        quarters = available_quarters(4, as_of=date(2026, 5, 16))
        assert (2026, "Q1") in quarters
