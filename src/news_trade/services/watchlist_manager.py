"""WatchlistManager — runtime watchlist backed by SQLite.

Operators run ``select-watchlist`` to scan the next 30 days of earnings and
pick tickers interactively.  The selection is persisted to
``WatchlistSelectionRow``.  On every pipeline cycle the three watchlist-reading
agents call ``get_active_watchlist()``, which returns the saved selection when
one exists, or falls back to ``settings.watchlist`` (the static ``.env`` list).

No restart is required after a selection is saved.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from news_trade.config import Settings
from news_trade.models.calendar import EarningsCalendarEntry
from news_trade.providers.base import CalendarProvider
from news_trade.providers.calendar.fmp import FMPBroadScanError
from news_trade.services.tables import WatchlistSelectionRow

logger = logging.getLogger(__name__)


class WatchlistManager:
    """Manages the active watchlist, with optional DB-persisted override.

    Args:
        settings:  Application configuration.  ``settings.watchlist`` is the
                   static fallback when no DB selection exists.
        session:   Sync SQLAlchemy ``Session`` for reading/writing
                   ``WatchlistSelectionRow`` rows.
        primary:   Primary ``CalendarProvider`` for ``scan_candidates()``.
        fallback:  Fallback provider used when primary returns empty or raises.
    """

    def __init__(
        self,
        settings: Settings,
        session: Session,
        primary: CalendarProvider,
        fallback: CalendarProvider,
    ) -> None:
        self._settings = settings
        self._session = session
        self._primary = primary
        self._fallback = fallback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan_candidates(
        self, from_date: date, to_date: date
    ) -> list[EarningsCalendarEntry]:
        """Return earnings entries where ``is_candidate=True`` in the window.

        Queries primary provider first; falls back to fallback provider if
        primary returns empty or raises.

        Args:
            from_date: Start of scan window (inclusive).
            to_date:   End of scan window (inclusive).

        Returns:
            Entries with ``1 <= days_until_report <= 31``, sorted by
            ``report_date`` ascending.
        """
        # Try a broad market scan first (empty tickers = no filter on FMP).
        # FMP free-tier keys reject this with 403; catch FMPBroadScanError
        # and fall back to per-ticker queries using the static watchlist.
        try:
            entries = await self._fetch_with_fallback([], from_date, to_date)
        except FMPBroadScanError as exc:
            logger.warning(
                "%s — falling back to static watchlist: %s",
                exc, list(self._settings.watchlist),
            )
            entries = await self._fetch_with_fallback(
                list(self._settings.watchlist), from_date, to_date
            )
        candidates = [e for e in entries if e.is_candidate]
        candidates.sort(key=lambda e: e.report_date)
        return candidates

    def load_selected(self) -> list[str]:
        """Return tickers from the most recent saved selection, or ``[]``."""
        row = self._session.execute(
            select(WatchlistSelectionRow)
            .order_by(WatchlistSelectionRow.saved_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return []
        return json.loads(row.tickers_json)  # type: ignore[no-any-return]

    def save_selection(self, tickers: list[str]) -> None:
        """Persist a new watchlist selection snapshot to SQLite.

        Appends a new row — does not overwrite the previous selection so the
        audit trail is preserved.

        Args:
            tickers: Ordered list of ticker symbols chosen by the operator.
        """
        row = WatchlistSelectionRow(
            tickers_json=json.dumps(tickers),
            saved_at=datetime.utcnow(),
        )
        self._session.add(row)
        self._session.commit()
        logger.info("Watchlist selection saved: %s", tickers)

    def get_active_watchlist(self) -> list[str]:
        """Return the active watchlist for this pipeline cycle.

        Priority:
          1. Most recent saved selection from SQLite (if non-empty).
          2. ``settings.watchlist`` — the static ``.env`` fallback.
        """
        selected = self.load_selected()
        if selected:
            return selected
        return list(self._settings.watchlist)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_with_fallback(
        self,
        tickers: list[str],
        from_date: date,
        to_date: date,
    ) -> list[EarningsCalendarEntry]:
        """Try primary provider; use fallback if primary is empty or raises."""
        try:
            entries = await self._primary.get_upcoming_earnings(
                tickers, from_date, to_date
            )
        except FMPBroadScanError:
            raise  # let scan_candidates handle the free-tier 403 fallback
        except Exception as exc:
            logger.warning(
                "Primary calendar provider (%s) failed: %s — trying fallback",
                self._primary.name,
                exc,
            )
            entries = []

        if not entries:
            logger.info(
                "Primary provider (%s) returned no entries; falling back to %s",
                self._primary.name,
                self._fallback.name,
            )
            try:
                entries = await self._fallback.get_upcoming_earnings(
                    tickers, from_date, to_date
                )
            except Exception as exc:
                logger.warning(
                    "Fallback calendar provider (%s) also failed: %s",
                    self._fallback.name,
                    exc,
                )
                entries = []

        return entries
