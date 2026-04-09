"""EarningsTickerNode — synthesises ephemeral EARN_PRE events for active tickers.

Runs every pipeline cycle as the third parallel branch from START.
Queries the news_events DB table for EARN_PRE rows written by EarningsCalendarAgent
(cron, 07:00 ET) and re-synthesises ephemeral NewsEvent objects so the analysis
pipeline always processes active earnings tickers — even on cycles with no new news.

Events produced here are ephemeral: they exist only in PipelineState for the current
cycle and are NOT re-persisted (EarningsCalendarAgent handles durable storage).

Event IDs use the prefix ``ticker_earn_pre_*`` to distinguish them from cron-generated
``calendar_earn_pre_*`` events.  The SignalGeneratorAgent._handle_earn_pre() guard
(checks for an existing OPEN Stage1 position) prevents duplicate positions from opening.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from news_trade.agents.base import BaseAgent
from news_trade.models import NewsEvent
from news_trade.models.events import EventType
from news_trade.services.database import build_engine
from news_trade.services.tables import NewsEventRow

if TYPE_CHECKING:
    from news_trade.config import Settings
    from news_trade.services.event_bus import EventBus
    from news_trade.services.stage1_repository import Stage1Repository

# Only synthesise events for tickers reporting within this many calendar days
_HORIZON_DAYS = 7

# event_id prefix used by EarningsCalendarAgent cron — we query rows with this prefix
_CRON_PREFIX = "calendar_earn_pre_"

# event_id prefix for events synthesised by this node
_NODE_PREFIX = "ticker_earn_pre_"


class EarningsTickerNode(BaseAgent):
    """Synthesises ephemeral EARN_PRE NewsEvent objects for all active earnings tickers.

    Runs every pipeline cycle as the third parallel branch from START.
    Does NOT write to the DB; events are ephemeral and exist only in the
    current pipeline state.  EarningsCalendarAgent (07:00 ET cron) handles
    durable DB persistence — this node is the pipeline-cycle counterpart that
    closes the structural gap between the cron and the main pipeline.

    Logic:
        1. Query ``news_events`` for rows with ``event_id LIKE 'calendar_earn_pre_%'``.
        2. Parse ``report_date`` from the event_id suffix (``rsplit("_", 1)[-1]``).
        3. Filter to tickers with ``1 <= days_until_report <= _HORIZON_DAYS``.
        4. Cross-reference with the session tickers; skip tickers not on it.
        5. Synthesise one ephemeral NewsEvent(event_type=EARN_PRE) per ticker.
        6. Return ``{"news_events": [...], "active_tickers": [...]}``.
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        tickers: list[str],
        stage1_repo: Stage1Repository,
    ) -> None:
        super().__init__(settings, event_bus)
        self._tickers = tickers
        self._stage1_repo = stage1_repo
        self._engine = build_engine(settings)

    async def run(self, state: dict) -> dict:  # type: ignore[type-arg]
        """Synthesise ephemeral EARN_PRE events for active earnings tickers.

        Returns:
            ``{"news_events": [NewsEvent, ...], "active_tickers": ["AAPL", ...]}``
        """
        try:
            events, tickers = self._gather_active_events()
        except Exception as exc:
            self.logger.error("EarningsTickerNode failed: %s", exc)
            return {"news_events": [], "active_tickers": [], "errors": [str(exc)]}

        if events:
            self.logger.info(
                "EarningsTickerNode: synthesised %d EARN_PRE event(s) for tickers=%s",
                len(events),
                tickers,
            )
        else:
            self.logger.debug(
                "EarningsTickerNode: no active earnings tickers in horizon"
            )

        return {"news_events": events, "active_tickers": tickers}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _gather_active_events(self) -> tuple[list[NewsEvent], list[str]]:
        """Query DB for cron-written EARN_PRE rows and build ephemeral events.

        Returns:
            A (events, tickers) tuple. Both lists are empty when there is
            nothing to do.
        """
        today = date.today()
        active_tickers = set(self._tickers)
        events: list[NewsEvent] = []
        tickers: list[str] = []

        with Session(self._engine) as session:
            rows = session.execute(
                select(NewsEventRow)
                .where(NewsEventRow.event_id.like(f"{_CRON_PREFIX}%"))
                .where(NewsEventRow.event_type == EventType.EARN_PRE.value)
            ).scalars().all()

        # Deduplicate by ticker — keep the row with the most recent published_at
        best: dict[str, NewsEventRow] = {}
        for row in rows:
            # event_id format: "calendar_earn_pre_{TICKER}_{YYYY-MM-DD}"
            # Use rsplit("_", 1) to safely handle tickers that may contain underscores
            # (though none currently do — this is defensive).
            try:
                report_date_str = row.event_id.rsplit("_", 1)[-1]
                report_date = date.fromisoformat(report_date_str)
            except ValueError:
                self.logger.debug(
                    "EarningsTickerNode: cannot parse date from event_id=%s — skipping",
                    row.event_id,
                )
                continue

            days_until = (report_date - today).days
            if not (1 <= days_until <= _HORIZON_DAYS):
                continue

            # Extract ticker: everything between prefix and the trailing _YYYY-MM-DD
            ticker = row.event_id[len(_CRON_PREFIX):-(len(report_date_str) + 1)]
            if not ticker:
                continue

            if ticker not in active_tickers:
                continue

            # Keep the most recent row per ticker (in case the cron ran multiple times)
            if ticker not in best or row.published_at > best[ticker].published_at:
                best[ticker] = row

        for ticker, row in best.items():
            report_date_str = row.event_id.rsplit("_", 1)[-1]
            report_date = date.fromisoformat(report_date_str)
            days_until = (report_date - today).days
            event = self._synthesise(ticker, report_date, days_until, row)
            events.append(event)
            tickers.append(ticker)

        return events, tickers

    def _synthesise(
        self,
        ticker: str,
        report_date: date,
        days_until: int,
        source_row: NewsEventRow,
    ) -> NewsEvent:
        """Build an ephemeral EARN_PRE NewsEvent (not persisted)."""
        return NewsEvent(
            event_id=f"{_NODE_PREFIX}{ticker}_{report_date}",
            headline=f"{ticker} earnings in {days_until}d (report date: {report_date})",
            summary=source_row.summary or f"days_until_report={days_until}",
            source="earnings_ticker_node",
            tickers=[ticker],
            event_type=EventType.EARN_PRE,
            published_at=datetime.now(UTC),
        )
