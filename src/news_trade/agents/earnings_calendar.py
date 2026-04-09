"""EarningsCalendarAgent - scans the earnings calendar and emits EARN_PRE events.

This agent runs outside the main LangGraph pipeline on a daily cron (07:00 ET Mon-Fri).
It synthesises NewsEvent(event_type=EARN_PRE) objects for every watchlist ticker that is
2-5 days from its report date, deduplicates via SQLite, publishes to Redis, and returns
the synthesised events so callers can inspect or chain them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from news_trade.agents.base import BaseAgent
from news_trade.config import Settings
from news_trade.models.calendar import EarningsCalendarEntry
from news_trade.models.events import EventType, NewsEvent
from news_trade.models.surprise import EstimatesData
from news_trade.providers.base import CalendarProvider, EstimatesProvider
from news_trade.services.event_bus import EventBus
from news_trade.services.tables import NewsEventRow

_SCAN_DAYS_AHEAD = 5


class EarningsCalendarAgent(BaseAgent):
    """Scans the earnings calendar and emits synthetic EARN_PRE NewsEvents.

    Responsibilities:
        - Query the primary CalendarProvider for the next 5 days.
        - Fall back to the secondary provider if the primary returns nothing.
        - Filter to entries whose ``is_actionable`` flag is True (2-5 days ahead).
        - Deduplicate against NewsEventRow (same dedup store as NewsIngestorAgent).
        - Publish each new event to the event bus.
        - Persist each new event to SQLite.
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        primary: CalendarProvider,
        fallback: CalendarProvider,
        engine: Engine,
        estimates_provider: EstimatesProvider | None = None,
        tickers: list[str] | None = None,
    ) -> None:
        super().__init__(settings, event_bus)
        self._primary = primary
        self._fallback = fallback
        self._engine = engine
        self._estimates_provider = estimates_provider
        self._tickers = tickers or []

    async def run(self, state: dict) -> dict:  # type: ignore[type-arg]
        """Scan the earnings calendar and return synthesised EARN_PRE events.

        Returns:
            ``{"news_events": [NewsEvent, ...], "errors": [...]}``
        """
        today = date.today()
        to_date = today + timedelta(days=_SCAN_DAYS_AHEAD)
        watchlist: list[str] = self._tickers

        # --- Fetch from primary, fall back if empty ---
        entries = await self._fetch_with_fallback(watchlist, today, to_date)

        actionable = [e for e in entries if e.is_actionable]
        self.logger.debug(
            "%d actionable calendar entries in window %s - %s",
            len(actionable), today, to_date,
        )

        published: list[NewsEvent] = []
        estimates: dict[str, EstimatesData] = {}
        errors: list[str] = list(state.get("errors") or [])

        # Build estimates from all actionable entries (regardless of dedup status)
        # so SentimentAnalystAgent always has estimates context when available.
        for entry in actionable:
            est = _build_estimates(entry)
            if est is None:
                continue
            if self._estimates_provider is not None:
                try:
                    rate = await self._estimates_provider.get_historical_beat_rate(
                        entry.ticker
                    )
                    if rate is not None:
                        est = est.model_copy(update={"historical_beat_rate": rate})
                except Exception as exc:
                    self.logger.warning(
                        "EstimatesProvider failed for %s: %s", entry.ticker, exc
                    )
            estimates[entry.ticker] = est

        with Session(self._engine) as session:
            for entry in actionable:
                event_id = _make_event_id(entry)
                if self._is_duplicate(event_id, session):
                    self.logger.debug("Calendar event already ingested: %s", event_id)
                    continue
                event = _synthesise_event(entry)
                self._persist(event, session)
                published.append(event)
            session.commit()

        for event in published:
            try:
                await self.event_bus.publish("news_events", event)
            except Exception as exc:
                self.logger.warning(
                    "Failed to publish calendar event %s: %s", event.event_id, exc
                )

        self.logger.info(
            "EarningsCalendarAgent: published %d EARN_PRE events", len(published)
        )
        return {"news_events": published, "estimates": estimates, "errors": errors}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_with_fallback(
        self,
        tickers: list[str],
        from_date: date,
        to_date: date,
    ) -> list[EarningsCalendarEntry]:
        """Try primary provider; use fallback if primary returns empty or raises."""
        try:
            entries = await self._primary.get_upcoming_earnings(
                tickers, from_date, to_date
            )
        except Exception as exc:
            self.logger.warning(
                "Primary calendar provider (%s) failed: %s - trying fallback",
                self._primary.name, exc,
            )
            entries = []

        if not entries:
            self.logger.info(
                "Primary provider (%s) returned no entries; falling back to %s",
                self._primary.name, self._fallback.name,
            )
            try:
                entries = await self._fallback.get_upcoming_earnings(
                    tickers, from_date, to_date
                )
            except Exception as exc:
                self.logger.warning(
                    "Fallback calendar provider (%s) also failed: %s",
                    self._fallback.name, exc,
                )
                entries = []

        return entries

    def _is_duplicate(self, event_id: str, session: Session) -> bool:
        """Check whether this event_id is already persisted in NewsEventRow."""
        return (
            session.execute(
                select(NewsEventRow.id)
                .where(NewsEventRow.event_id == event_id)
                .limit(1)
            ).first()
            is not None
        )

    def _persist(self, event: NewsEvent, session: Session) -> None:
        """Insert a synthesised NewsEvent into the database (does not commit)."""
        row = NewsEventRow(
            event_id=event.event_id,
            headline=event.headline,
            summary=event.summary,
            source=event.source,
            url=event.url,
            event_type=event.event_type,
            published_at=event.published_at,
        )
        row.tickers = event.tickers
        session.add(row)


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _make_event_id(entry: EarningsCalendarEntry) -> str:
    """Build a stable, dedup-friendly event_id for a calendar entry."""
    return f"calendar_earn_pre_{entry.ticker}_{entry.report_date}"


def _build_estimates(entry: EarningsCalendarEntry) -> EstimatesData | None:
    """Build a minimal EstimatesData from a calendar entry.

    Returns None when no EPS estimate is available (e.g. yfinance fallback).
    Revenue fields default to 0.0 and analyst count to 0 because the calendar
    endpoint does not provide them; they render as placeholder values in the
    EstimatesRenderer narrative so the LLM knows they are absent.
    """
    if entry.eps_estimate is None:
        return None
    eps = entry.eps_estimate
    return EstimatesData(
        ticker=entry.ticker,
        fiscal_period=entry.fiscal_quarter,
        report_date=entry.report_date,
        eps_estimate=eps,
        eps_low=eps,
        eps_high=eps,
        revenue_estimate=0.0,
        revenue_low=0.0,
        revenue_high=0.0,
        num_analysts=0,
    )


def _synthesise_event(entry: EarningsCalendarEntry) -> NewsEvent:
    """Convert an EarningsCalendarEntry into a synthetic EARN_PRE NewsEvent."""
    return NewsEvent(
        event_id=_make_event_id(entry),
        headline=(
            f"{entry.ticker} scheduled to report {entry.fiscal_quarter}"
            f" on {entry.report_date} ({entry.timing})"
        ),
        summary=(
            f"eps_estimate={entry.eps_estimate}"
            f" days_until_report={entry.days_until_report}"
        ),
        source="earnings_calendar",
        tickers=[entry.ticker],
        event_type=EventType.EARN_PRE,
        published_at=datetime.utcnow(),
    )
