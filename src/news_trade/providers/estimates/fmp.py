"""FMP (Financial Modeling Prep) estimates provider.

Fetches historical EPS-surprise records from the FMP earnings-surprises
endpoint and computes a per-ticker beat rate for use in EARN_PRE sizing.

Endpoint used: GET /api/v3/earnings-surprises/{symbol}?limit=8&apikey=...

Each record contains:
    actualEarningResult   — reported EPS
    estimatedEarning      — consensus EPS estimate at the time of report

Beat rate = quarters where actual > estimated / total valid quarters.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"


class FMPEstimatesProvider:
    """Fetches historical EPS beat rates from the FMP earnings-surprises endpoint.

    Returns ``None`` gracefully on any API or network failure so callers
    can fall back to the static ``earn_default_beat_rate`` without crashing.
    """

    def __init__(self, api_key: str, base_url: str = _FMP_BASE_URL) -> None:
        if not api_key:
            raise ValueError("FMPEstimatesProvider requires a non-empty api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "fmp_estimates"

    async def get_historical_beat_rate(
        self, ticker: str, lookback: int = 8
    ) -> float | None:
        """Fetch the historical EPS beat rate for *ticker* over *lookback* quarters.

        Args:
            ticker: Stock symbol (e.g. ``"AAPL"``).
            lookback: Number of past quarters to include in the calculation.
                      Defaults to 8 (two years).

        Returns:
            Beat rate as a float in ``[0.0, 1.0]``, or ``None`` when the
            data cannot be fetched or is insufficient (< 1 valid record).
        """
        import aiohttp  # type: ignore[import-not-found]  # lazy import

        url = (
            f"{self._base_url}/earnings-surprises/{ticker.upper()}"
            f"?limit={lookback}&apikey={self._api_key}"
        )
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(url, timeout=timeout) as resp,
            ):
                if resp.status != 200:
                    logger.warning(
                        "FMP estimates returned HTTP %d for %s",
                        resp.status,
                        ticker,
                    )
                    return None
                data: list[dict[str, Any]] = await resp.json()
        except Exception as exc:
            logger.warning("FMP estimates request failed for %s: %s", ticker, exc)
            return None

        return _compute_beat_rate(data, ticker)


def _compute_beat_rate(
    records: list[dict[str, Any]], ticker: str
) -> float | None:
    """Compute beat rate from raw FMP earnings-surprises records.

    Skips any record where ``actualEarningResult`` or ``estimatedEarning``
    is ``None``.  Returns ``None`` when no valid records remain.
    """
    beats = 0
    total = 0
    for record in records:
        actual = record.get("actualEarningResult")
        estimated = record.get("estimatedEarning")
        if actual is None or estimated is None:
            continue
        try:
            actual_f = float(actual)
            estimated_f = float(estimated)
        except (TypeError, ValueError):
            logger.debug(
                "FMP estimates: skipping non-numeric record for %s: %r",
                ticker,
                record,
            )
            continue
        total += 1
        if actual_f > estimated_f:
            beats += 1

    if total == 0:
        logger.debug("FMP estimates: no valid records for %s", ticker)
        return None

    rate = beats / total
    logger.debug(
        "FMP estimates: %s beat_rate=%.2f (%d/%d quarters)",
        ticker,
        rate,
        beats,
        total,
    )
    return rate
