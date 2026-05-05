#!/usr/bin/env python3
"""One-time script to seed historical lobbying data for all contractors.

Downloads the last 8 quarters of Senate LDA filings for every company in
``contractor_tickers.csv`` and stores them in Redis for use by
``LobbyingEnrichmentService``.

Run once at setup:
    uv run python scripts/seed_lobbying_data.py

Requires:
    - LDA_API_KEY set in .env
    - Redis running (REDIS_URL in .env)

The script is intentionally slow (polite rate-limiting: 0.5s between API pages)
to avoid triggering LDA rate limits. For 70 contractors × 8 quarters, expect
a runtime of 5–15 minutes depending on how many filings exist.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Ensure the src directory is on the path when run directly
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import redis.asyncio as aioredis

from news_trade.config import GovTradeSettings, Settings
from news_trade.providers.lobbying.lda_provider import LdaProvider, available_quarters
from news_trade.services.contractor_lookup import ContractorLookup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_logger = logging.getLogger("seed_lobbying")

_LOOKBACK_QUARTERS = 8


async def seed(settings: GovTradeSettings, base_settings: Settings) -> None:
    if not settings.lda_api_key:
        _logger.error("LDA_API_KEY is not set — cannot seed lobbying data")
        sys.exit(1)

    redis_client = aioredis.from_url(base_settings.redis_url, decode_responses=True)
    provider = LdaProvider(
        api_key=settings.lda_api_key,
        redis_client=redis_client,
        cache_ttl_days=settings.lobbying_cache_ttl_days,
    )
    lookup = ContractorLookup()
    quarters = available_quarters(_LOOKBACK_QUARTERS)

    _logger.info(
        "Seeding lobbying data for %d contractors × %d quarters",
        lookup.size,
        len(quarters),
    )

    # Collect unique lda_client_names from the lookup table
    seen: set[str] = set()
    # Iterate by looking up all known tickers from the CSV
    # (ContractorLookup does not expose all keys, so we read the CSV directly)
    from pathlib import Path as _Path
    import csv

    csv_path = _Path(__file__).parents[1] / "src" / "news_trade" / "data" / "contractor_tickers.csv"
    client_names: list[tuple[str, str]] = []  # (ticker, lda_client_name)

    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ticker = row.get("ticker", "").strip()
            lda_name = row.get("lda_client_name", "").strip()
            if ticker and lda_name and lda_name not in seen:
                seen.add(lda_name)
                client_names.append((ticker, lda_name))

    total = len(client_names)
    for i, (ticker, client_name) in enumerate(client_names, start=1):
        _logger.info("[%d/%d] Fetching: %s (%s)", i, total, client_name, ticker)
        try:
            filings = await provider.get_filings(client_name, quarters)
            _logger.info(
                "  → %d filings found across %d quarters", len(filings), len(quarters)
            )
        except Exception as exc:
            _logger.warning("  → Failed: %s", exc)

    await redis_client.aclose()
    _logger.info("Seed complete.")


def main() -> None:
    gov_settings = GovTradeSettings()
    base_settings = Settings()
    asyncio.run(seed(gov_settings, base_settings))


if __name__ == "__main__":
    main()
