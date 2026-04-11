"""FinnhubEstimatesProvider — historical EPS beat rates via Finnhub (free tier).

Endpoint: GET https://finnhub.io/api/v1/stock/eps-surprise?symbol={ticker}&token={key}

Each record contains:
    actual    — reported EPS
    estimate  — consensus EPS estimate at the time of report
    period    — fiscal quarter end date (YYYY-MM-DD)
    surprise  — absolute EPS surprise
    surprisePercent — % EPS surprise

Beat rate = quarters where actual > estimated / total valid quarters.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from news_trade.providers._http import http_get_with_retry

logger = logging.getLogger(__name__)

_EPS_SURPRISE_URL = "https://finnhub.io/api/v1/stock/eps-surprise"


class FinnhubEstimatesProvider:
    """Fetches historical EPS beat rates from the Finnhub eps-surprise endpoint.

    Returns ``None`` gracefully on any API or network failure so callers
    can fall back to the static ``earn_default_beat_rate`` without crashing.
    """

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("FinnhubEstimatesProvider requires a non-empty api_key")
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "finnhub_estimates"

    async def get_historical_beat_rate(
        self, ticker: str, lookback: int = 8
    ) -> float | None:
        """Fetch the historical EPS beat rate for *ticker* over *lookback* quarters.

        Args:
            ticker: Stock symbol (e.g. ``"AAPL"``).
            lookback: Number of past quarters to include. Defaults to 8 (two years).

        Returns:
            Beat rate as a float in ``[0.0, 1.0]``, or ``None`` when the
            data cannot be fetched or is insufficient (< 1 valid record).
        """
        params = {"symbol": ticker.upper(), "token": self._api_key}
        try:
            async with httpx.AsyncClient(
                timeout=15.0, follow_redirects=True
            ) as client:
                resp = await http_get_with_retry(
                    client, _EPS_SURPRISE_URL, params=params
                )
                data: list[dict[str, Any]] = resp.json()
        except Exception as exc:
            logger.debug(
                "Finnhub EPS-surprise unavailable for %s: %s", ticker, exc
            )
            return None
        # Finnhub returns newest first; trim to lookback before computing
        return _compute_beat_rate(data[:lookback], ticker)


def _compute_beat_rate(
    records: list[dict[str, Any]], ticker: str
) -> float | None:
    """Compute beat rate from raw Finnhub eps-surprise records.

    Skips any record where ``actual`` or ``estimate`` is ``None``.
    Returns ``None`` when no valid records remain.
    """
    beats = 0
    total = 0
    for record in records:
        actual = record.get("actual")
        estimate = record.get("estimate")
        if actual is None or estimate is None:
            continue
        try:
            actual_f = float(actual)
            estimated_f = float(estimate)
        except (TypeError, ValueError):
            logger.debug(
                "Finnhub estimates: skipping non-numeric record for %s: %r",
                ticker,
                record,
            )
            continue
        total += 1
        if actual_f > estimated_f:
            beats += 1

    if total == 0:
        logger.debug("Finnhub estimates: no valid records for %s", ticker)
        return None

    rate = beats / total
    logger.debug(
        "Finnhub estimates: %s beat_rate=%.2f (%d/%d quarters)",
        ticker,
        rate,
        beats,
        total,
    )
    return rate
