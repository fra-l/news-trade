"""Senate LDA lobbying disclosure API client.

Fetches quarterly lobbying filings from the Senate Lobbying Disclosure Act
API (https://lda.senate.gov/api/v1/), caches results in Redis, and is
aware of the LDA publication schedule so it never requests quarters that
have not yet been published.

Authentication: requires a free API key stored as ``LDA_API_KEY`` in .env.
Rate limits are generous — implement polite delay between pages regardless.

Publication schedule (data available ~45 days after quarter end):
  Q1 (Jan-Mar) -> available ~May 15
  Q2 (Apr-Jun) -> available ~Aug 15
  Q3 (Jul-Sep) -> available ~Nov 15
  Q4 (Oct-Dec) -> available ~Feb 15 (following year)
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import date
from typing import Any, Protocol, runtime_checkable

import httpx

from news_trade.models.lobbying import LobbyingFiling
from news_trade.providers._http import http_get_with_retry

_logger = logging.getLogger(__name__)

_BASE_URL = "https://lda.senate.gov/api/v1"
_FILINGS_URL = f"{_BASE_URL}/filings/"
_PAGE_SIZE = 25
_INTER_PAGE_DELAY_S = 0.5

# Publish month for each quarter (quarter -> month of that year the data lands)
_PUBLISH_MONTH: dict[int, int] = {1: 5, 2: 8, 3: 11, 4: 2}


@runtime_checkable
class LobbyingDataProvider(Protocol):
    """Minimal interface for fetching lobbying filing data."""

    async def get_filings(
        self,
        client_name: str,
        quarters: list[tuple[int, str]],
    ) -> list[LobbyingFiling]:
        """Return ``LobbyingFiling`` objects for *client_name* in *quarters*.

        Args:
            client_name: LDA client name (from ``contractor_tickers.csv``
                ``lda_client_name`` column).
            quarters: List of ``(year, "Q1"|"Q2"|"Q3"|"Q4")`` pairs to fetch.
        """
        ...


def available_quarters(
    lookback: int, as_of: date | None = None
) -> list[tuple[int, str]]:
    """Return up to *lookback* ``(year, period)`` pairs published as of *as_of*.

    Walks backwards quarter by quarter, skipping quarters whose 45-day
    publication window has not yet elapsed.

    Args:
        lookback: Maximum number of quarters to return.
        as_of: Reference date (defaults to today).
    """
    ref = as_of or date.today()
    results: list[tuple[int, str]] = []

    year = ref.year
    quarter = (ref.month - 1) // 3 + 1

    checked = 0
    while len(results) < lookback and checked < lookback + 6:
        # Compute publication date for this quarter
        pub_year = year + 1 if quarter == 4 else year
        pub_month = _PUBLISH_MONTH[quarter]
        try:
            pub_date = date(pub_year, pub_month, 15)
        except ValueError:
            pub_date = date(pub_year, pub_month, 1)

        if ref >= pub_date:
            results.append((year, f"Q{quarter}"))

        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
        checked += 1

    return results


class LdaProvider:
    """Fetches Senate LDA filings with Redis caching.

    Args:
        api_key: Senate LDA API key (free registration at lda.senate.gov).
        redis_client: Async Redis client for caching; ``None`` disables
                      caching (results are still correct, just slower).
        cache_ttl_days: Redis TTL in days for cached quarterly data.
    """

    def __init__(
        self,
        api_key: str,
        redis_client: Any | None = None,
        cache_ttl_days: int = 7,
    ) -> None:
        self._api_key = api_key
        self._redis = redis_client
        self._ttl = cache_ttl_days * 86400

    async def get_filings(
        self,
        client_name: str,
        quarters: list[tuple[int, str]],
    ) -> list[LobbyingFiling]:
        """Return filings for *client_name* across all requested *quarters*."""
        filings: list[LobbyingFiling] = []
        for year, period in quarters:
            quarter_filings = await self._get_quarter(client_name, year, period)
            filings.extend(quarter_filings)
        return filings

    async def _get_quarter(
        self,
        client_name: str,
        year: int,
        period: str,
    ) -> list[LobbyingFiling]:
        cache_key = f"lda:{client_name}:{year}:{period}"

        # Cache hit
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return [LobbyingFiling.model_validate(f) for f in cached]

        # Fetch from API
        filings = await self._fetch_from_api(client_name, year, period)

        # Cache result (even empty list — avoids repeated API calls)
        await self._cache_set(
            cache_key, [f.model_dump() for f in filings]
        )
        return filings

    async def _fetch_from_api(
        self,
        client_name: str,
        year: int,
        period: str,
    ) -> list[LobbyingFiling]:
        headers = {"Authorization": f"Token {self._api_key}"}
        params: dict[str, Any] = {
            "client_name": client_name,
            "filing_year": year,
            "filing_period": period,
            "limit": _PAGE_SIZE,
            "offset": 0,
        }

        filings: list[LobbyingFiling] = []
        async with httpx.AsyncClient(
            headers=headers, timeout=20.0
        ) as client:
            while True:
                try:
                    resp = await http_get_with_retry(
                        client, _FILINGS_URL, params=params
                    )
                except httpx.HTTPStatusError as exc:
                    _logger.warning(
                        "LDA API error (%s Q%s): %s", year, period, exc
                    )
                    break

                data = resp.json()
                for row in data.get("results", []):
                    filing = _map_filing(row, ticker="", client_name=client_name)
                    if filing is not None:
                        filings.append(filing)

                if not data.get("next"):
                    break
                params["offset"] = params["offset"] + _PAGE_SIZE

        return filings

    # ── Redis helpers ──────────────────────────────────────────────────────

    async def _cache_get(self, key: str) -> list[dict[str, Any]] | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            if raw:
                return json.loads(raw)  # type: ignore[no-any-return]
        except Exception:
            pass
        return None

    async def _cache_set(self, key: str, data: list[dict[str, Any]]) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(key, json.dumps(data), ex=self._ttl)
        except Exception:
            _logger.debug("Redis cache write failed for key %s", key)


# ---------------------------------------------------------------------------
# Mapping helper
# ---------------------------------------------------------------------------


def _map_filing(
    row: dict[str, Any],
    ticker: str,
    client_name: str,
) -> LobbyingFiling | None:
    """Map a raw LDA API result row to a ``LobbyingFiling``."""
    uuid = row.get("filing_uuid", "")
    if not uuid:
        return None

    income_raw = row.get("income")
    expenses_raw = row.get("expenses")
    spend = 0.0
    if income_raw is not None:
        with contextlib.suppress(TypeError, ValueError):
            spend = float(income_raw)
    elif expenses_raw is not None:
        with contextlib.suppress(TypeError, ValueError):
            spend = float(expenses_raw)

    # Collect all government entities across all activities
    all_agencies: list[str] = []
    raw_descs: list[str] = []
    for activity in row.get("lobbying_activities", []):
        for entity in activity.get("government_entities", []):
            name = entity.get("name", "").strip()
            if name:
                all_agencies.append(name)
        desc = activity.get("description", "")
        if desc:
            raw_descs.append(desc)

    unique_agencies = list(dict.fromkeys(all_agencies))
    n = len(unique_agencies) or 1
    agency_spend = {agency: spend / n for agency in unique_agencies}

    lda_name = (
        row.get("client", {}).get("name", client_name)
        if isinstance(row.get("client"), dict)
        else client_name
    )

    return LobbyingFiling(
        filing_uuid=uuid,
        ticker=ticker,
        lda_client_name=lda_name,
        year=int(row.get("filing_year", 0)),
        period=str(row.get("filing_period", "")),
        total_spend=spend,
        agency_spend=agency_spend,
        lobbied_agencies=unique_agencies,
        raw_description=" | ".join(raw_descs)[:2000],
    )
