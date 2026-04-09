"""FMP (Financial Modeling Prep) earnings calendar provider.

Uses the free-tier endpoint: GET /api/v3/earning_calendar
Requires an FMP API key (250 req/day on free tier).

Note on broad vs per-ticker queries
------------------------------------
Passing an empty ``tickers`` list performs a broad date-range scan that returns
all companies reporting in the window.  This endpoint requires an FMP paid plan;
free-tier keys receive HTTP 403.  When tickers are supplied, the same endpoint is
queried and results are filtered client-side — this works on the free tier because
FMP does not distinguish the request shape.
``FMPBroadScanError`` is raised on 403 so callers can fall back gracefully
to a per-ticker query.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from news_trade.models.calendar import EarningsCalendarEntry, ReportTiming

logger = logging.getLogger(__name__)


class FMPBroadScanError(Exception):
    """Raised when a broad (no-ticker) FMP scan returns HTTP 403.

    Indicates the API key is on the free tier, which requires explicit ticker
    symbols.  Callers should retry with an explicit tickers list.
    """


_FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

_TIMING_MAP: dict[str, ReportTiming] = {
    "bmo": ReportTiming.PRE_MARKET,   # Before Market Open
    "amc": ReportTiming.POST_MARKET,  # After Market Close
    "pre market": ReportTiming.PRE_MARKET,
    "after market": ReportTiming.POST_MARKET,
    "post market": ReportTiming.POST_MARKET,
}


class FMPCalendarProvider:
    """Fetches upcoming earnings from the FMP earning_calendar endpoint."""

    def __init__(self, api_key: str, base_url: str = _FMP_BASE_URL) -> None:
        if not api_key:
            raise ValueError("FMPCalendarProvider requires a non-empty api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "fmp_calendar"

    async def get_upcoming_earnings(
        self,
        tickers: list[str],
        from_date: date,
        to_date: date,
    ) -> list[EarningsCalendarEntry]:
        """Fetch earnings calendar from FMP and filter to the requested tickers."""
        import httpx  # type: ignore[import-not-found]  # lazy import

        url = (
            f"{self._base_url}/earning_calendar"
            f"?from={from_date}&to={to_date}&apikey={self._api_key}"
        )
        # Empty tickers = broad scan (return all companies in the date window).
        ticker_filter = {t.upper() for t in tickers} if tickers else None

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 403 and ticker_filter is None:
                    raise FMPBroadScanError(
                        "FMP broad calendar scan returned 403 — "
                        "free-tier keys require explicit ticker symbols. "
                        "Upgrade to FMP paid plan for broad market scans."
                    )
                if resp.status_code != 200:
                    logger.warning(
                        "FMP calendar returned HTTP %d for window %s - %s",
                        resp.status_code, from_date, to_date,
                    )
                    return []
                data: list[dict[str, Any]] = resp.json()
        except Exception as exc:
            logger.warning("FMP calendar request failed: %s", exc)
            return []

        entries: list[EarningsCalendarEntry] = []
        for item in data:
            symbol = (item.get("symbol") or "").upper()
            if ticker_filter is not None and symbol not in ticker_filter:
                continue
            entry = self._parse_item(item, symbol)
            if entry is not None:
                entries.append(entry)

        logger.debug(
            "FMP calendar: %d entries fetched for window %s - %s",
            len(entries), from_date, to_date,
        )
        return entries

    def _parse_item(
        self, item: dict[str, Any], ticker: str
    ) -> EarningsCalendarEntry | None:
        raw_date = item.get("date") or item.get("reportDate") or ""
        if not raw_date:
            return None
        try:
            report_date = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            logger.debug("FMP: unparseable date %r for %s, skipping", raw_date, ticker)
            return None

        # FMP returns fiscal period like "Q1 2026"
        fiscal_period: str = item.get("period") or ""
        fiscal_quarter = (
            fiscal_period.upper() if fiscal_period else f"Q? {report_date.year}"
        )
        fiscal_year = report_date.year

        # Timing: FMP uses "bmo" / "amc" / None
        raw_timing = (item.get("time") or "").lower().strip()
        timing = _TIMING_MAP.get(raw_timing, ReportTiming.UNKNOWN)

        eps_estimate = item.get("epsEstimated")

        return EarningsCalendarEntry(
            ticker=ticker,
            report_date=report_date,
            fiscal_quarter=fiscal_quarter,
            fiscal_year=fiscal_year,
            timing=timing,
            eps_estimate=float(eps_estimate) if eps_estimate is not None else None,
            fetched_at=datetime.utcnow(),
        )
