"""ExpiryScanner — marks stale Stage 1 positions as EXPIRED.

Runs on a daily cron at 07:15 ET Mon-Fri, alongside EarningsCalendarAgent.
It queries Stage1Repository for all OPEN positions whose expected_report_date
has passed and closes them as EXPIRED so the concentration check in
RiskManagerAgent does not over-count stale rows.

No network calls. No LLM calls. Pure DB read + write.
"""

from __future__ import annotations

from news_trade.agents.base import BaseAgent
from news_trade.models.positions import Stage1Status
from news_trade.services.stage1_repository import Stage1Repository

# EventBus.publish requires a BaseModel; STAGE1_EXPIRED has no downstream consumer
# yet, so we skip the publish and rely solely on the WARNING log for the audit trail.


class ExpiryScanner(BaseAgent):
    """Closes Stage 1 positions that passed their expected report date with no event.

    Responsibilities:
        - Load all OPEN positions whose expected_report_date < today.
        - Mark each one EXPIRED via Stage1Repository.
        - Publish a lightweight STAGE1_EXPIRED notification to the event bus.
        - Log a WARNING per expired position for the audit trail.
    """

    def __init__(self, settings, event_bus, stage1_repo: Stage1Repository) -> None:  # type: ignore[override]
        super().__init__(settings, event_bus)
        self._stage1_repo = stage1_repo

    async def run(self, state: dict) -> dict:  # type: ignore[type-arg]
        """Scan for expired Stage 1 positions and close them.

        Returns:
            ``{"errors": []}`` — does not mutate pipeline state keys.
        """
        expired = self._stage1_repo.load_expired()

        if not expired:
            self.logger.debug("ExpiryScanner: no expired Stage 1 positions found")
            return {"errors": []}

        for pos in expired:
            self._stage1_repo.update_status(pos.id, Stage1Status.EXPIRED)
            self.logger.warning(
                "EARN_PRE expired without announcement: %s %s (id=%s, expected=%s)",
                pos.ticker,
                pos.fiscal_quarter,
                pos.id,
                pos.expected_report_date,
            )

        self.logger.info(
            "ExpiryScanner: marked %d position(s) as EXPIRED", len(expired)
        )
        return {"errors": []}
