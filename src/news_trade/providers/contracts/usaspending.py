"""USASpending.gov contract award provider.

Polls the free, unauthenticated USASpending API for federal contract awards
within a configurable time window. Deduplicates against Redis so each
award is emitted only once across polling cycles.

API: POST https://api.usaspending.gov/api/v2/search/spending_by_award/
Docs: https://api.usaspending.gov/
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import httpx

from news_trade.config import GovTradeSettings
from news_trade.models.contracts import ContractAwardEvent
from news_trade.providers._http import http_post_with_retry

_logger = logging.getLogger(__name__)

_BASE_URL = "https://api.usaspending.gov/api/v2"
_SEARCH_ENDPOINT = f"{_BASE_URL}/search/spending_by_award/"
_PAGE_LIMIT = 100
_REDIS_SEEN_PREFIX = "usaspending:seen:"
_REDIS_SEEN_TTL = 60 * 60 * 24 * 30  # 30 days

_FIELDS = [
    "Award ID",
    "Recipient Name",
    "recipient_uei",
    "recipient_parent_name",
    "Award Amount",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Contract Award Type",
    "Description",
    "NAICS Code",
    "NAICS Description",
    "Period of Performance Start Date",
    "Period of Performance Current End Date",
    "Action Date",
    "Last Modified Date",
    "Place of Performance State Code",
]


class USASpendingProvider:
    """Fetches federal contract awards from USASpending.gov.

    Pagination is handled internally — ``fetch()`` returns all awards in
    the requested window regardless of how many API pages that requires.
    Deduplication via Redis ensures each award is yielded only once even
    when polling windows overlap.

    Args:
        settings: Gov-trade settings (min award value, award type codes).
        redis_client: Async Redis client for dedup; ``None`` disables Redis
                      dedup (useful in tests and one-shot runs).
    """

    def __init__(
        self,
        settings: GovTradeSettings,
        redis_client: Any | None = None,
    ) -> None:
        self._min_amount = settings.usaspending_min_award_usd
        self._award_types = list(settings.usaspending_award_types)
        self._redis = redis_client

    @property
    def name(self) -> str:
        return "usaspending"

    async def fetch(
        self,
        since: datetime,
        until: datetime | None = None,
    ) -> list[ContractAwardEvent]:
        """Return awards with action dates in ``[since, until]``.

        Filters by configured award types and minimum obligated amount.
        Skips awards already seen in Redis.
        """
        end = until or datetime.utcnow()
        awards: list[ContractAwardEvent] = []
        page = 1

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                body = self._build_body(since, end, page)
                try:
                    resp = await http_post_with_retry(
                        client, _SEARCH_ENDPOINT, json=body
                    )
                except httpx.HTTPStatusError as exc:
                    _logger.error(
                        "USASpending API error (page %d): %s", page, exc
                    )
                    break

                data = resp.json()
                results: list[dict[str, Any]] = data.get("results", [])

                for row in results:
                    award = _map_award(row)
                    if not award.award_id:
                        continue
                    if await self._already_seen(award.award_id):
                        continue
                    await self._mark_seen(award.award_id)
                    awards.append(award)

                page_meta: dict[str, Any] = data.get("page_metadata", {})
                if not page_meta.get("hasNext", False):
                    break
                page += 1

        _logger.info(
            "USASpending: fetched %d new awards (%s → %s)",
            len(awards),
            since.date(),
            end.date(),
        )
        return awards

    def _build_body(
        self, since: datetime, until: datetime, page: int
    ) -> dict[str, Any]:
        return {
            "filters": {
                "award_type_codes": self._award_types,
                "time_period": [
                    {
                        "start_date": since.strftime("%Y-%m-%d"),
                        "end_date": until.strftime("%Y-%m-%d"),
                        "date_type": "action_date",
                    }
                ],
                "award_amounts": [{"lower_bound": self._min_amount}],
            },
            "fields": _FIELDS,
            "page": page,
            "limit": _PAGE_LIMIT,
            "sort": "Award Amount",
            "order": "desc",
        }

    # ── Redis dedup ────────────────────────────────────────────────────────

    def _redis_key(self, award_id: str) -> str:
        return f"{_REDIS_SEEN_PREFIX}{award_id}"

    async def _already_seen(self, award_id: str) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.exists(self._redis_key(award_id)))
        except Exception:
            return False

    async def _mark_seen(self, award_id: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(
                self._redis_key(award_id), "1", ex=_REDIS_SEEN_TTL
            )
        except Exception:
            _logger.debug("Redis mark-seen failed for award %s", award_id)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _map_award(row: dict[str, Any]) -> ContractAwardEvent:
    """Map a USASpending search result row to a ``ContractAwardEvent``."""
    return ContractAwardEvent(
        award_id=str(row.get("Award ID") or ""),
        recipient_name=str(row.get("Recipient Name") or ""),
        recipient_uei=_str_or_none(row.get("recipient_uei")),
        recipient_parent_name=_str_or_none(row.get("recipient_parent_name")),
        amount_usd=float(row.get("Award Amount") or 0.0),
        awarding_agency=str(row.get("Awarding Agency") or ""),
        awarding_sub_agency=_str_or_none(row.get("Awarding Sub Agency")),
        award_type=_parse_award_type(str(row.get("Contract Award Type") or "")),
        description=str(row.get("Description") or ""),
        naics_code=_str_or_none(row.get("NAICS Code")),
        naics_description=_str_or_none(row.get("NAICS Description")),
        period_start=_parse_date(row.get("Period of Performance Start Date")),
        period_end=_parse_date(row.get("Period of Performance Current End Date")),
        sign_date=_parse_date(row.get("Action Date")) or date.today(),
        last_modified_date=_parse_date(row.get("Last Modified Date")),
        place_of_performance_state=_str_or_none(
            row.get("Place of Performance State Code")
        ),
    )


def _parse_award_type(raw: str) -> str:
    """Extract the single-character type code from USASpending type strings.

    The API may return ``"D"`` or ``"D - Definitive Contract"``. We always
    store only the code character.
    """
    code = raw.strip()[:1].upper()
    return code if code in {"A", "B", "C", "D"} else raw.strip()


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None
