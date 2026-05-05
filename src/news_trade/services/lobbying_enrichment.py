"""Lobbying enrichment service — confidence multiplier for contract award signals.

Asks: did this company recently increase lobbying spend on the awarding agency?

  spend increased > 3x  -> multiplier 1.5
  spend increased 1.5-3x -> multiplier 1.3
  spend increased < 1.5x or new -> multiplier 1.15
  no data found         → multiplier 1.0  (neutral — base signal unchanged)
  spend flat            → multiplier 0.95
  spend decreased       → multiplier 0.8
  company stopped       → multiplier 0.7

The service is stateless and injectable. It never raises — any failure
returns a neutral ``EnrichmentResult(multiplier=1.0)`` so the base signal
is unaffected.

LOBBYING_ENRICHMENT_ENABLED=false makes the service return neutral
multipliers immediately without any API calls.

Design decisions (per TASKS.md):
  - Per-agency spend: proportional allocation from total filing spend.
    Raw LDA data gives agency targets, not exact dollar splits per agency.
  - Company name: resolved via ContractorLookup.lda_client_name column.
    Falls back to ticker string if not found in lookup table.
  - EnrichmentResult: nested field on MaterialityResult (confirmed).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from statistics import mean

from sqlalchemy.orm import Session

from news_trade.config import GovTradeSettings
from news_trade.models.lobbying import (
    EnrichmentResult,
    LobbyingFiling,
    LobbyingSignal,
    LobbyingTrend,
)
from news_trade.providers.lobbying.lda_provider import (
    LobbyingDataProvider,
    available_quarters,
)
from news_trade.services.contractor_lookup import ContractorLookup
from news_trade.services.tables import EnrichmentLogRow

_logger = logging.getLogger(__name__)

# Multiplier bands (from LOBBY_ENRICHMENT.md)
_MULTIPLIER_STOPPED = 0.7
_MULTIPLIER_DECREASING = 0.8
_MULTIPLIER_FLAT = 0.95
_MULTIPLIER_NEUTRAL = 1.0
_MULTIPLIER_NEW_OR_SMALL_INCREASE = 1.15
_MULTIPLIER_MEDIUM_INCREASE = 1.3
_MULTIPLIER_LARGE_INCREASE = 1.5

_LARGE_INCREASE_RATIO = 3.0
_MEDIUM_INCREASE_RATIO = 1.5
_FLAT_BAND = 0.10  # ±10% is considered flat


class LobbyingEnrichmentService:
    """Apply a lobbying-spend confidence multiplier to a contract award signal.

    Args:
        provider: Lobbying data provider (``LdaProvider`` or ``MockLobbyingProvider``).
        lookup: Contractor lookup table for resolving ticker → LDA client name.
        agency_mapping: Dict mapping USASpending agency names to lists of LDA
                        government-entity names (from ``agency_lda_mapping.json``).
        settings: Gov-trade settings (enabled flag, lookback quarters, min spend).
        session: SQLAlchemy session for logging enrichment calls; ``None`` skips
            logging.
    """

    def __init__(
        self,
        provider: LobbyingDataProvider,
        lookup: ContractorLookup,
        agency_mapping: dict[str, list[str]],
        settings: GovTradeSettings,
        session: Session | None = None,
    ) -> None:
        self._provider = provider
        self._lookup = lookup
        self._agency_mapping = agency_mapping
        self._enabled = settings.lobbying_enrichment_enabled
        self._lookback = settings.lobbying_lookback_quarters
        self._min_spend = settings.lobbying_min_spend_threshold_usd
        self._session = session

    async def get_multiplier(
        self,
        ticker: str,
        awarding_agency: str,
        award_id: str = "",
        as_of: date | None = None,
    ) -> EnrichmentResult:
        """Return a confidence multiplier for *ticker* / *awarding_agency*.

        Args:
            ticker: Resolved stock ticker of the contract recipient.
            awarding_agency: Top-level agency name as returned by USASpending.
            award_id: Award ID for audit logging.
            as_of: Reference date for quarter freshness (defaults to today).

        Returns:
            ``EnrichmentResult`` — never raises.
        """
        if not self._enabled:
            return EnrichmentResult(multiplier=_MULTIPLIER_NEUTRAL, data_found=False)

        try:
            result = await self._compute(ticker, awarding_agency, as_of or date.today())
        except Exception:
            _logger.warning(
                "Enrichment failed for %s / %s — returning neutral",
                ticker, awarding_agency, exc_info=True,
            )
            result = EnrichmentResult(multiplier=_MULTIPLIER_NEUTRAL, data_found=False)

        self._log(award_id, ticker, awarding_agency, result)
        return result

    async def _compute(
        self,
        ticker: str,
        awarding_agency: str,
        as_of: date,
    ) -> EnrichmentResult:
        # Resolve LDA client name
        lookup_result = self._lookup.lookup(ticker)
        client_name = (
            lookup_result.lda_client_name
            if lookup_result and lookup_result.lda_client_name
            else ticker
        )

        # Determine available quarters
        quarters = available_quarters(self._lookback, as_of)
        if not quarters:
            return EnrichmentResult(multiplier=_MULTIPLIER_NEUTRAL, data_found=False)

        # Fetch filings
        filings = await self._provider.get_filings(client_name, quarters)

        # Filter by minimum spend threshold
        filings = [f for f in filings if f.total_spend >= self._min_spend]

        if not filings:
            return EnrichmentResult(
                multiplier=_MULTIPLIER_NEUTRAL,
                data_found=False,
                rationale="No lobbying filings found above spend threshold",
                quarters_lookback=len(quarters),
            )

        # Map awarding agency to LDA entity names
        lda_agency_names = self._resolve_agency(awarding_agency)

        # Compute spend per quarter directed at this agency
        signal = _compute_signal(
            ticker, awarding_agency, filings, lda_agency_names, quarters
        )

        multiplier = _signal_to_multiplier(signal)
        return EnrichmentResult(
            multiplier=multiplier,
            rationale=_build_rationale(signal, multiplier),
            data_found=True,
            quarters_lookback=signal.quarters_analyzed,
            spend_delta_pct=signal.spend_delta_pct,
        )

    def _resolve_agency(self, usaspending_agency: str) -> list[str]:
        """Return LDA entity names for the given USASpending agency name."""
        upper = usaspending_agency.upper()
        for key, lda_names in self._agency_mapping.items():
            if key.upper() == upper or upper in key.upper():
                return lda_names
        # No mapping found — return the original name for best-effort matching
        return [usaspending_agency]

    def _log(
        self,
        award_id: str,
        ticker: str,
        agency: str,
        result: EnrichmentResult,
    ) -> None:
        if self._session is None:
            return
        try:
            row = EnrichmentLogRow(
                contract_award_id=award_id,
                ticker=ticker,
                agency=agency,
                multiplier_applied=result.multiplier,
                data_found=int(result.data_found),
                spend_delta_pct=result.spend_delta_pct,
                rationale=result.rationale,
            )
            self._session.add(row)
            self._session.commit()
        except Exception:
            _logger.debug("Failed to log enrichment for %s", ticker, exc_info=True)
            self._session.rollback()


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------


def _compute_signal(
    ticker: str,
    agency: str,
    filings: list[LobbyingFiling],
    lda_agency_names: list[str],
    quarters: list[tuple[int, str]],
) -> LobbyingSignal:
    """Build a ``LobbyingSignal`` from filings for the given agency targets."""
    lda_names_upper = {n.upper() for n in lda_agency_names}

    # Map quarter key → agency spend
    spend_map: dict[tuple[int, str], float] = {}
    for filing in filings:
        q_key = (filing.year, filing.period)
        agency_total = sum(
            spend
            for agency_name, spend in filing.agency_spend.items()
            if agency_name.upper() in lda_names_upper
            or any(lda.upper() in agency_name.upper() for lda in lda_names_upper)
        )
        if q_key in spend_map:
            spend_map[q_key] += agency_total
        else:
            spend_map[q_key] = agency_total

    # Build ordered spend series (oldest → newest)
    spend_by_quarter = [spend_map.get(q, 0.0) for q in reversed(quarters)]

    # Split into recent (last 2) and prior
    recent = spend_by_quarter[-2:] if len(spend_by_quarter) >= 2 else spend_by_quarter
    prior = spend_by_quarter[:-2] if len(spend_by_quarter) > 2 else []

    recent_mean = mean(recent) if recent else 0.0
    prior_mean = mean(prior) if prior else 0.0

    trend, delta_pct = _classify_trend(recent_mean, prior_mean)

    return LobbyingSignal(
        ticker=ticker,
        agency=agency,
        quarters_analyzed=len(quarters),
        spend_delta_pct=delta_pct,
        trend=trend,
        spend_by_quarter=spend_by_quarter,
    )


def _classify_trend(
    recent_mean: float, prior_mean: float
) -> tuple[LobbyingTrend, float | None]:
    if recent_mean == 0 and prior_mean > 0:
        return LobbyingTrend.STOPPED, -100.0
    if recent_mean > 0 and prior_mean == 0:
        return LobbyingTrend.NEW, None
    if recent_mean == 0 and prior_mean == 0:
        return LobbyingTrend.NO_DATA, None

    ratio = recent_mean / prior_mean
    delta = (recent_mean - prior_mean) / prior_mean * 100.0

    if ratio > 1 + _FLAT_BAND:
        return LobbyingTrend.INCREASING, round(delta, 2)
    if ratio < 1 - _FLAT_BAND:
        return LobbyingTrend.DECREASING, round(delta, 2)
    return LobbyingTrend.FLAT, round(delta, 2)


def _signal_to_multiplier(signal: LobbyingSignal) -> float:
    match signal.trend:
        case LobbyingTrend.STOPPED:
            return _MULTIPLIER_STOPPED
        case LobbyingTrend.DECREASING:
            return _MULTIPLIER_DECREASING
        case LobbyingTrend.FLAT:
            return _MULTIPLIER_FLAT
        case LobbyingTrend.NO_DATA:
            return _MULTIPLIER_NEUTRAL
        case LobbyingTrend.NEW:
            return _MULTIPLIER_NEW_OR_SMALL_INCREASE
        case LobbyingTrend.INCREASING:
            recent = (
                signal.spend_by_quarter[-2:]
                if len(signal.spend_by_quarter) >= 2
                else signal.spend_by_quarter
            )
            prior = (
                signal.spend_by_quarter[:-2]
                if len(signal.spend_by_quarter) > 2
                else []
            )
            if not prior or mean(prior) == 0:
                return _MULTIPLIER_NEW_OR_SMALL_INCREASE
            ratio = mean(recent) / mean(prior)
            if ratio > _LARGE_INCREASE_RATIO:
                return _MULTIPLIER_LARGE_INCREASE
            if ratio >= _MEDIUM_INCREASE_RATIO:
                return _MULTIPLIER_MEDIUM_INCREASE
            return _MULTIPLIER_NEW_OR_SMALL_INCREASE
        case _:
            return _MULTIPLIER_NEUTRAL


def _build_rationale(signal: LobbyingSignal, multiplier: float) -> str:
    delta_str = (
        f"{signal.spend_delta_pct:+.1f}%"
        if signal.spend_delta_pct is not None
        else "N/A"
    )
    return (
        f"trend={signal.trend.value}, delta={delta_str}, "
        f"quarters={signal.quarters_analyzed} → multiplier {multiplier:.2f}"
    )


# ---------------------------------------------------------------------------
# Mapping loader
# ---------------------------------------------------------------------------


def load_agency_mapping(path: str) -> dict[str, list[str]]:
    """Load the USASpending → LDA agency name mapping from a JSON file."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)  # type: ignore[no-any-return]
