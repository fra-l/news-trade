"""ContractPollerAgent — fetches federal contract awards via an injected provider."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from news_trade.agents.base import BaseAgent
from news_trade.config import GovTradeSettings, Settings
from news_trade.models.contracts import ContractAwardEvent
from news_trade.providers.base import ContractProvider
from news_trade.services.event_bus import EventBus


class ContractPollerAgent(BaseAgent):
    """Fetch new federal contract awards and emit them into pipeline state.

    Reads ``last_poll_govtrade`` from state to determine the polling window.
    When absent (first cycle), defaults to one poll interval ago so the
    first run always captures recent awards.

    State keys written:
        ``contract_awards``      — list of new ``ContractAwardEvent`` objects
        ``last_poll_govtrade``   — datetime of this poll's upper bound
        ``errors``               — any fetch errors (operator.add reducer)
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        gov_settings: GovTradeSettings,
        provider: ContractProvider,
    ) -> None:
        super().__init__(settings, event_bus)
        self._gov = gov_settings
        self._provider = provider

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        now = datetime.utcnow()
        since: datetime = state.get("last_poll_govtrade") or (
            now - timedelta(minutes=self._gov.usaspending_poll_interval_minutes)
        )

        self.logger.info(
            "ContractPoller: polling %s → %s  provider=%s",
            since.strftime("%Y-%m-%d %H:%M"),
            now.strftime("%Y-%m-%d %H:%M"),
            self._provider.name,
        )

        try:
            awards: list[ContractAwardEvent] = await self._provider.fetch(
                since=since, until=now
            )
        except Exception as exc:
            self.logger.error("ContractPoller: fetch failed: %s", exc)
            return {
                "contract_awards": [],
                "last_poll_govtrade": now,
                "errors": [f"ContractPoller: {exc}"],
            }

        for award in awards:
            self.logger.info(
                "ContractPoller: award  id=%-30s  amount=$%,.0f  "
                "agency=%s  recipient=%s",
                award.award_id,
                award.amount_usd,
                award.awarding_agency,
                award.recipient_name[:60],
            )

        self.logger.info(
            "ContractPoller: fetched %d new awards via %s",
            len(awards),
            self._provider.name,
        )
        return {"contract_awards": awards, "last_poll_govtrade": now}
