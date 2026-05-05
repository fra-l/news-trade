"""EntityResolverAgent — maps USASpending recipient names to stock tickers."""

from __future__ import annotations

import asyncio
from typing import Any

from news_trade.agents.base import BaseAgent
from news_trade.config import GovTradeSettings, Settings
from news_trade.models.contracts import ContractAwardEvent, EntityResolution
from news_trade.services.entity_resolver import EntityResolutionService
from news_trade.services.event_bus import EventBus


class EntityResolverAgent(BaseAgent):
    """Resolve each contract award's recipient name to a stock ticker.

    Runs all resolutions concurrently (bounded by a semaphore), then filters
    to awards whose confidence meets the configured minimum threshold.
    Awards that cannot be resolved are logged and dropped — never traded.

    State keys read:
        ``contract_awards``   — list of ``ContractAwardEvent`` objects

    State keys written:
        ``resolutions``       — list of ``EntityResolution`` objects (passed only)
        ``errors``            — resolution errors (operator.add reducer)
    """

    _MAX_CONCURRENT = 10

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        gov_settings: GovTradeSettings,
        resolver: EntityResolutionService,
    ) -> None:
        super().__init__(settings, event_bus)
        self._gov = gov_settings
        self._resolver = resolver
        self._semaphore = asyncio.Semaphore(self._MAX_CONCURRENT)

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        awards: list[ContractAwardEvent] = state.get("contract_awards") or []

        if not awards:
            self.logger.info("EntityResolver: no awards to resolve")
            return {"resolutions": []}

        self.logger.info("EntityResolver: resolving %d awards", len(awards))

        async def _resolve(award: ContractAwardEvent) -> EntityResolution:
            async with self._semaphore:
                return await self._resolver.resolve(
                    recipient_name=award.recipient_name,
                    award_id=award.award_id,
                    description=award.description,
                    awarding_agency=award.awarding_agency,
                )

        outcomes: list[EntityResolution | BaseException] = list(
            await asyncio.gather(*[_resolve(a) for a in awards], return_exceptions=True)
        )

        resolutions: list[EntityResolution] = []
        errors: list[str] = []
        min_conf = self._gov.entity_resolution_min_confidence

        for award, outcome in zip(awards, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                msg = (
                    f"EntityResolver: resolution raised for "
                    f"{award.recipient_name}: {outcome}"
                )
                self.logger.error(msg)
                errors.append(msg)
                continue

            if outcome.ticker is None or outcome.confidence < min_conf:
                self.logger.debug(
                    "EntityResolver: discarded  recipient=%s  ticker=%s  conf=%.2f",
                    award.recipient_name[:60],
                    outcome.ticker,
                    outcome.confidence,
                )
                continue

            self.logger.info(
                "EntityResolver: resolved  %-60s → %-6s  layer=%-6s  conf=%.2f",
                award.recipient_name[:60],
                outcome.ticker,
                outcome.layer.value,
                outcome.confidence,
            )
            resolutions.append(outcome)

        self.logger.info(
            "EntityResolver: %d/%d resolved above confidence threshold %.2f",
            len(resolutions),
            len(awards),
            min_conf,
        )
        result: dict[str, Any] = {"resolutions": resolutions}
        if errors:
            result["errors"] = errors
        return result
