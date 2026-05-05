"""SEC EDGAR company-name → ticker resolution provider.

Implements the ``EdgarProvider`` protocol required by ``EntityResolutionService``
(Layer 2). Uses two free, unauthenticated EDGAR API endpoints:

  1. Full-text search to find the company's CIK:
     GET https://efts.sec.gov/LATEST/search-index?q="{name}"&forms=10-K

  2. Submissions endpoint to retrieve ticker symbols:
     GET https://data.sec.gov/submissions/CIK{cik:010d}.json

Both endpoints are operated by the SEC and require no API key. Rate limits
are generous for read operations. Retries use ``http_get_with_retry``.
"""

from __future__ import annotations

import logging

import httpx

from news_trade.providers._http import http_get_with_retry

_logger = logging.getLogger(__name__)

_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


class SECEdgarProvider:
    """Resolve a company name to a stock ticker via SEC EDGAR.

    Satisfies the ``EdgarProvider`` protocol defined in
    ``services/entity_resolver.py``.  Returns ``(ticker, exchange)`` on
    success or ``None`` when the company cannot be found or has no listed
    ticker.

    Resolution steps:
      1. Search EDGAR full-text index for the company name (10-K filings).
      2. Extract the CIK from the first hit.
      3. Fetch the company submissions JSON to read ``tickers`` + ``exchanges``.
      4. Return the first ticker/exchange pair.
    """

    @property
    def name(self) -> str:
        return "sec_edgar"

    async def lookup(self, company_name: str) -> tuple[str, str] | None:
        """Return ``(ticker, exchange)`` or ``None`` if resolution fails.

        Args:
            company_name: Recipient name as returned by USASpending.
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            cik = await self._find_cik(client, company_name)
            if cik is None:
                return None
            return await self._find_ticker(client, cik)

    # ── Step 1: search for CIK ─────────────────────────────────────────────

    async def _find_cik(
        self, client: httpx.AsyncClient, company_name: str
    ) -> str | None:
        # Quote the name so EDGAR treats it as a phrase search
        quoted = f'"{company_name}"'
        params = {
            "q": quoted,
            "forms": "10-K",
            "hits.hits._source": "ciks,entity_name",
        }
        try:
            resp = await http_get_with_retry(client, _SEARCH_URL, params=params)
        except httpx.HTTPStatusError as exc:
            _logger.warning("EDGAR search failed for '%s': %s", company_name, exc)
            return None

        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])

        if not hits:
            # Retry with unquoted name (handles cases where exact phrase has no 10-K)
            params["q"] = company_name
            try:
                resp = await http_get_with_retry(client, _SEARCH_URL, params=params)
                hits = resp.json().get("hits", {}).get("hits", [])
            except httpx.HTTPStatusError:
                return None

        if not hits:
            return None

        source = hits[0].get("_source", {})
        ciks: list[str] = source.get("ciks", [])
        return ciks[0] if ciks else None

    # ── Step 2: fetch tickers from submissions ─────────────────────────────

    async def _find_ticker(
        self, client: httpx.AsyncClient, cik: str
    ) -> tuple[str, str] | None:
        padded = str(cik).zfill(10)
        url = _SUBMISSIONS_URL.format(cik=padded)
        try:
            resp = await http_get_with_retry(client, url)
        except httpx.HTTPStatusError as exc:
            _logger.warning("EDGAR submissions fetch failed (CIK %s): %s", cik, exc)
            return None

        data = resp.json()
        tickers: list[str] = data.get("tickers", [])
        exchanges: list[str] = data.get("exchanges", [])

        if not tickers:
            return None

        ticker = tickers[0].upper()
        exchange = exchanges[0].upper() if exchanges else ""
        _logger.debug("EDGAR resolved CIK %s → %s (%s)", cik, ticker, exchange)
        return ticker, exchange
