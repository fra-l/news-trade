"""MaterialityScorerAgent — scores contract awards and applies lobbying enrichment."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from news_trade.agents.base import BaseAgent
from news_trade.config import GovTradeSettings, Settings
from news_trade.models.contracts import (
    ContractAwardEvent,
    EntityResolution,
    MaterialityResult,
)
from news_trade.providers.base import MaterialityProvider
from news_trade.services.event_bus import EventBus
from news_trade.services.lobbying_enrichment import LobbyingEnrichmentService

_logger = logging.getLogger(__name__)

# Minimum adjusted score to pass to SignalGeneratorAgent
_MIN_SCORE = 3.0


class MaterialityScorerAgent(BaseAgent):
    """Score the materiality of each resolved contract award.

    For each award → resolution pair:
      1. Fetch market cap via yfinance (best-effort; None falls back to
         absolute-value heuristic in the provider).
      2. Call the materiality provider → ``MaterialityResult`` with
         ``score == adjusted_score`` (no enrichment applied yet).
      3. Call the lobbying enrichment service → ``EnrichmentResult``.
      4. Apply the multiplier:
         ``adjusted_score = score x multiplier`` (clamped to 0).
         Attach ``enrichment`` to the result via ``model_copy()``.
      5. Drop awards below ``_MIN_SCORE`` — too immaterial to trade.

    State keys read:
        ``contract_awards``       — list of ``ContractAwardEvent``
        ``resolutions``           — list of ``EntityResolution``

    State keys written:
        ``materiality_results``   — list of enriched ``MaterialityResult``
        ``errors``                — scoring errors (operator.add reducer)
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        gov_settings: GovTradeSettings,
        provider: MaterialityProvider,
        enrichment: LobbyingEnrichmentService,
    ) -> None:
        super().__init__(settings, event_bus)
        self._gov = gov_settings
        self._provider = provider
        self._enrichment = enrichment

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        awards: list[ContractAwardEvent] = state.get("contract_awards") or []
        resolutions: list[EntityResolution] = state.get("resolutions") or []

        if not resolutions:
            self.logger.info("MaterialityScorer: no resolved awards to score")
            return {"materiality_results": []}

        # Build award_id → ContractAwardEvent lookup
        award_map: dict[str, ContractAwardEvent] = {a.award_id: a for a in awards}

        scored: list[MaterialityResult] = []
        errors: list[str] = []

        for resolution in resolutions:
            award = award_map.get(resolution.recipient_name) or _find_by_ticker(
                awards, resolution
            )
            if award is None:
                self.logger.warning(
                    "MaterialityScorer: no matching award for resolution %s",
                    resolution.recipient_name,
                )
                continue

            try:
                result = await self._score_one(award, resolution)
            except Exception as exc:
                msg = (
                    f"MaterialityScorer: scoring failed for "
                    f"{resolution.ticker}: {exc}"
                )
                self.logger.error(msg)
                errors.append(msg)
                continue

            if result.adjusted_score < _MIN_SCORE:
                self.logger.debug(
                    "MaterialityScorer: dropped  ticker=%s  "
                    "adjusted_score=%.2f  (below %.1f)",
                    resolution.ticker,
                    result.adjusted_score,
                    _MIN_SCORE,
                )
                continue

            self.logger.info(
                "MaterialityScorer: scored  ticker=%-6s  score=%.1f  adjusted=%.1f"
                "  novelty=%-12s  direction=%-8s  multiplier=%.2f",
                result.ticker,
                result.score,
                result.adjusted_score,
                result.novelty.value,
                result.direction.value,
                result.enrichment.multiplier if result.enrichment else 1.0,
            )
            scored.append(result)

        self.logger.info(
            "MaterialityScorer: %d/%d awards passed materiality threshold %.1f",
            len(scored),
            len(resolutions),
            _MIN_SCORE,
        )
        out: dict[str, Any] = {"materiality_results": scored}
        if errors:
            out["errors"] = errors
        return out

    async def _score_one(
        self,
        award: ContractAwardEvent,
        resolution: EntityResolution,
    ) -> MaterialityResult:
        ticker = resolution.ticker or ""

        # Best-effort market cap fetch (never blocks the pipeline)
        market_cap = await asyncio.to_thread(_fetch_market_cap, ticker)

        # Base materiality score (score == adjusted_score at this point)
        base = await self._provider.score(award, ticker, market_cap_usd=market_cap)

        # Lobbying enrichment multiplier
        enrichment = await self._enrichment.get_multiplier(
            ticker=ticker,
            awarding_agency=award.awarding_agency,
            award_id=award.award_id,
        )

        adjusted = max(0.0, round(base.score * enrichment.multiplier, 4))
        return base.model_copy(
            update={"adjusted_score": adjusted, "enrichment": enrichment}
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_market_cap(ticker: str) -> float | None:
    """Return market cap in USD via yfinance, or None on any failure."""
    try:
        import yfinance as yf  # type: ignore[import-untyped]

        info = yf.Ticker(ticker).info
        return float(info.get("marketCap") or 0) or None
    except Exception:
        return None


def _find_by_ticker(
    awards: list[ContractAwardEvent],
    resolution: EntityResolution,
) -> ContractAwardEvent | None:
    """Fallback: find the award whose recipient_name matches the resolution."""
    for award in awards:
        if award.recipient_name == resolution.recipient_name:
            return award
    return None
