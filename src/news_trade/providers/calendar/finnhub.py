"""FinnhubCalendarProvider — earnings calendar from the Finnhub API.

Free tier supports the /calendar/earnings endpoint.
Provides report date, EPS estimate, and timing (bmo/amc).
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import httpx

from news_trade.models.calendar import EarningsCalendarEntry, ReportTiming

logger = logging.getLogger(__name__)

_BASE_URL = "https://finnhub.io/api/v1"

_TIMING_MAP: dict[str, ReportTiming] = {
    "bmo": ReportTiming.PRE_MARKET,
    "amc": ReportTiming.POST_MARKET,
}


class FinnhubCalendarProvider:
    """Fetches upcoming earnings from Finnhub /calendar/earnings.

    One request per ticker; free tier supports this endpoint.
    Provides EPS estimates and pre/post-market timing — better than the
    yfinance fallback, which returns date only.
    """

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("FinnhubCalendarProvider requires a non-empty api_key")
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "finnhub_calendar"

    async def get_upcoming_earnings(
        self,
        tickers: list[str],
        from_date: date,
        to_date: date,
    ) -> list[EarningsCalendarEntry]:
        """Return earnings entries for each ticker within the date window."""
        entries: list[EarningsCalendarEntry] = []

        async with httpx.AsyncClient(timeout=15.0) as client:
            for ticker in tickers:
                try:
                    resp = await client.get(
                        f"{_BASE_URL}/calendar/earnings",
                        params={
                            "symbol": ticker,
                            "from": str(from_date),
                            "to": str(to_date),
                            "token": self._api_key,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    for item in data.get("earningsCalendar") or []:
                        entry = _item_to_entry(item)
                        if entry is not None:
                            entries.append(entry)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "Finnhub calendar fetch failed for %s: %s", ticker, exc
                    )

        return entries


def _item_to_entry(item: dict) -> EarningsCalendarEntry | None:
    """Convert a Finnhub earningsCalendar item to an EarningsCalendarEntry."""
    try:
        report_date = date.fromisoformat(item["date"])
    except (KeyError, ValueError):
        return None

    symbol = (item.get("symbol") or "").upper()
    if not symbol:
        return None

    quarter = item.get("quarter")
    year = item.get("year") or report_date.year
    fiscal_quarter = f"Q{quarter} {year}" if quarter else f"Q? {year}"

    timing = _TIMING_MAP.get(item.get("hour") or "", ReportTiming.UNKNOWN)

    eps_estimate: float | None = None
    raw_eps = item.get("epsEstimate")
    if raw_eps is not None:
        try:
            eps_estimate = float(raw_eps)
        except (TypeError, ValueError):
            pass

    return EarningsCalendarEntry(
        ticker=symbol,
        report_date=report_date,
        fiscal_quarter=fiscal_quarter,
        fiscal_year=int(year),
        timing=timing,
        eps_estimate=eps_estimate,
        fetched_at=datetime.utcnow(),
    )
