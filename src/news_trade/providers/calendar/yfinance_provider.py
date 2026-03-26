"""yfinance earnings calendar provider - zero-API-key fallback.

Uses the unofficial yfinance Ticker.calendar property. Sync calls are
wrapped in asyncio.to_thread to avoid blocking the event loop.

Limitations vs FMP:
  - No report timing (always UNKNOWN)
  - No EPS estimate
  - May return None or incomplete data for some tickers
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from news_trade.models.calendar import EarningsCalendarEntry, ReportTiming

logger = logging.getLogger(__name__)


class YFinanceCalendarProvider:
    """Fetches upcoming earnings from yfinance (unofficial, no API key required)."""

    @property
    def name(self) -> str:
        return "yfinance_calendar"

    async def get_upcoming_earnings(
        self,
        tickers: list[str],
        from_date: date,
        to_date: date,
    ) -> list[EarningsCalendarEntry]:
        """Return earnings entries for each ticker falling within the date window."""
        entries: list[EarningsCalendarEntry] = []
        for ticker in tickers:
            entry = await self._fetch_one(ticker, from_date, to_date)
            if entry is not None:
                entries.append(entry)
        return entries

    async def _fetch_one(
        self,
        ticker: str,
        from_date: date,
        to_date: date,
    ) -> EarningsCalendarEntry | None:
        """Fetch the next earnings date for a single ticker via yfinance."""
        try:
            calendar = await asyncio.to_thread(self._get_calendar, ticker)
        except Exception as exc:
            logger.debug("yfinance calendar fetch failed for %s: %s", ticker, exc)
            return None

        if calendar is None:
            return None

        report_date = self._extract_date(calendar)
        if report_date is None:
            return None

        if not (from_date <= report_date <= to_date):
            return None

        return EarningsCalendarEntry(
            ticker=ticker.upper(),
            report_date=report_date,
            fiscal_quarter=f"Q? {report_date.year}",
            fiscal_year=report_date.year,
            timing=ReportTiming.UNKNOWN,
            eps_estimate=None,
            fetched_at=datetime.utcnow(),
        )

    def _get_calendar(self, ticker: str) -> object:
        """Synchronous yfinance call - run via asyncio.to_thread."""
        import yfinance as yf  # type: ignore[import-not-found]  # lazy import
        return yf.Ticker(ticker).calendar

    def _extract_date(self, calendar: object) -> date | None:
        """Extract the earnings date from the yfinance calendar object."""
        # yfinance returns a dict like {"Earnings Date": [Timestamp, ...]}
        # or a DataFrame depending on version - handle both.
        try:
            if hasattr(calendar, "get"):
                # dict-like
                raw = calendar.get("Earnings Date")
                if raw is None:
                    return None
                # May be a list of Timestamps
                if hasattr(raw, "__iter__") and not isinstance(raw, str):
                    items = list(raw)
                    if not items:
                        return None
                    raw = items[0]
                return _to_date(raw)
            # DataFrame
            if hasattr(calendar, "loc"):
                try:
                    val = calendar.loc["Earnings Date"]
                    if hasattr(val, "iloc"):
                        val = val.iloc[0]
                    return _to_date(val)
                except (KeyError, IndexError):
                    return None
        except Exception as exc:
            logger.debug("Could not extract date from yfinance calendar: %s", exc)
        return None


def _to_date(value: object) -> date | None:
    """Convert a pandas Timestamp or datetime-like to a plain date."""
    try:
        import pandas as pd  # type: ignore[import-untyped]  # lazy import
        if isinstance(value, pd.Timestamp):
            return value.date()  # type: ignore[no-any-return]
    except ImportError:
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None
