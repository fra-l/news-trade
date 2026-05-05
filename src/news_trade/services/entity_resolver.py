"""Three-layer entity resolution service.

Maps USASpending recipient names to stock tickers using three escalating
layers, each more expensive than the last:

  Layer 1 — Static lookup table   (O(1), free, covers ~70 large contractors)
  Layer 2 — SEC EDGAR search      (async HTTP, free, covers mid-cap companies)
  Layer 3 — LLM fallback          (quick model, paid, last resort)

Every attempt is logged to ``EntityResolutionRow`` for later accuracy
analysis.  Negative results (all layers failed) are cached in Redis for
7 days to avoid redundant EDGAR calls for unresolvable names.

Design decisions (recorded per TASKS.md):
  - All layers are awaitable; Layer 1 is CPU-only but kept in the same
    async call chain to avoid `asyncio.to_thread` overhead for 248-key
    dict lookups.
  - ``EdgarProvider`` is an injected protocol; the concrete
    ``SECEdgarProvider`` is wired in Task 5.  ``None`` skips Layer 2.
  - LLM prompt includes recipient name + description (≤300 chars) +
    awarding agency to give sector context without leaking noisy text.
  - Redis miss key: ``entity_resolve:miss:{sha256(name)[:20]}`` — avoids
    key-length issues with unusual characters in legal entity names.
  - The entire ``resolve()`` method is wrapped in a top-level try/except;
    any infrastructure failure returns a graceful NONE resolution so the
    pipeline continues rather than crashing.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from news_trade.config import GovTradeSettings
from news_trade.models.contracts import EntityResolution, ResolutionLayer
from news_trade.services.contractor_lookup import ContractorLookup
from news_trade.services.llm_client import LLMClient, LLMClientFactory
from news_trade.services.tables import EntityResolutionRow

_logger = logging.getLogger(__name__)

_REDIS_MISS_TTL = 60 * 60 * 24 * 7  # 7 days
_REDIS_KEY_PREFIX = "entity_resolve:miss:"

_LLM_SYSTEM = (
    "You are an expert in US federal government contracting and public equity markets. "
    "Given a company name from a USASpending.gov contract award, identify whether the "
    "company is publicly traded and return its stock ticker. "
    "If the company is private, a foreign unlisted entity, or you cannot identify it "
    "with reasonable confidence, return an empty ticker string."
)


# ---------------------------------------------------------------------------
# EdgarProvider protocol (concrete implementation in providers/contracts/sec_edgar.py)
# ---------------------------------------------------------------------------


@runtime_checkable
class EdgarProvider(Protocol):
    """Minimal interface for SEC EDGAR company-name → ticker lookup."""

    async def lookup(self, company_name: str) -> tuple[str, str] | None:
        """Return ``(ticker, exchange)`` or ``None`` when not found.

        Args:
            company_name: Recipient name to search, as returned by USASpending.
        """
        ...


# ---------------------------------------------------------------------------
# LLM structured-output schema
# ---------------------------------------------------------------------------


class _LLMResolutionSchema(BaseModel):
    ticker: str = Field(
        description=(
            "Stock ticker symbol (e.g. 'PLTR'), or empty string if private or unknown"
        )
    )
    exchange: str = Field(
        default="",
        description="Primary listing exchange (NYSE / NASDAQ / OTC), or empty string",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this resolution from 0.0 (guess) to 1.0 (certain)",
    )
    reasoning: str = Field(
        description="One-sentence explanation of how the company was identified"
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class EntityResolutionService:
    """Resolve a USASpending recipient name to a public stock ticker.

    Args:
        lookup: Pre-loaded static contractor table (Layer 1).
        edgar: EDGAR provider for Layer 2; ``None`` disables the layer.
        llm: LLM factory for Layer 3 (uses ``factory.quick``); ``None``
             disables the layer.
        redis_client: Async Redis client for negative-result caching;
                      ``None`` disables caching (results are still correct,
                      just potentially slower on repeated misses).
        session: SQLAlchemy session for logging resolution attempts;
                 ``None`` skips DB logging.
        settings: Gov-trade settings (``entity_resolution_min_confidence``).
    """

    def __init__(
        self,
        lookup: ContractorLookup,
        edgar: EdgarProvider | None,
        llm: LLMClientFactory | None,
        redis_client: Any | None,
        session: Session | None,
        settings: GovTradeSettings,
    ) -> None:
        self._lookup = lookup
        self._edgar = edgar
        self._llm = llm
        self._redis = redis_client
        self._session = session
        self._min_confidence = settings.entity_resolution_min_confidence

    async def resolve(
        self,
        recipient_name: str,
        award_id: str = "",
        description: str = "",
        awarding_agency: str = "",
    ) -> EntityResolution:
        """Resolve *recipient_name* to a ticker through all available layers.

        Args:
            recipient_name: Legal entity name exactly as returned by USASpending.
            award_id: USASpending award ID — attached to the DB log row.
            description: Award description text — used in the LLM fallback prompt.
            awarding_agency: Awarding agency name — used in the LLM fallback prompt.

        Returns:
            ``EntityResolution`` with a non-None ``ticker`` on success, or
            ``ticker=None`` and ``layer=NONE`` when all layers fail.  Never raises.
        """
        try:
            return await self._resolve_inner(
                recipient_name, award_id, description, awarding_agency
            )
        except Exception:
            _logger.exception(
                "Unexpected error resolving '%s' — returning NONE", recipient_name
            )
            return EntityResolution(
                recipient_name=recipient_name,
                ticker=None,
                confidence=0.0,
                layer=ResolutionLayer.NONE,
            )

    async def _resolve_inner(
        self,
        recipient_name: str,
        award_id: str,
        description: str,
        awarding_agency: str,
    ) -> EntityResolution:
        # ── Redis negative-result cache ────────────────────────────────────
        if self._redis is not None and await self._cached_miss(recipient_name):
            _logger.debug("Redis miss cache hit for '%s'", recipient_name)
            return EntityResolution(
                recipient_name=recipient_name,
                ticker=None,
                confidence=0.0,
                layer=ResolutionLayer.NONE,
            )

        # ── Layer 1: static lookup table ──────────────────────────────────
        result = self._lookup.lookup(recipient_name)
        if result is not None:
            resolution = EntityResolution(
                recipient_name=recipient_name,
                ticker=result.ticker,
                exchange=result.exchange,
                confidence=result.confidence,
                layer=ResolutionLayer.STATIC,
            )
            self._log(award_id, resolution)
            return resolution

        # ── Layer 2: SEC EDGAR ────────────────────────────────────────────
        if self._edgar is not None:
            try:
                edgar_result = await self._edgar.lookup(recipient_name)
            except Exception:
                _logger.warning(
                    "EDGAR lookup failed for '%s' — skipping Layer 2", recipient_name
                )
                edgar_result = None

            if edgar_result is not None:
                ticker, exchange = edgar_result
                resolution = EntityResolution(
                    recipient_name=recipient_name,
                    ticker=ticker,
                    exchange=exchange,
                    confidence=0.9,
                    layer=ResolutionLayer.EDGAR,
                )
                self._log(award_id, resolution)
                return resolution

        # ── Layer 3: LLM fallback ─────────────────────────────────────────
        if self._llm is not None:
            llm_resolution = await self._llm_resolve(
                self._llm.quick, recipient_name, description, awarding_agency
            )
            if (
                llm_resolution is not None
                and llm_resolution.ticker
                and llm_resolution.confidence >= self._min_confidence
            ):
                self._log(award_id, llm_resolution)
                return llm_resolution

        # ── All layers failed ─────────────────────────────────────────────
        await self._cache_miss(recipient_name)
        failed = EntityResolution(
            recipient_name=recipient_name,
            ticker=None,
            confidence=0.0,
            layer=ResolutionLayer.NONE,
        )
        self._log(award_id, failed)
        return failed

    async def _llm_resolve(
        self,
        client: LLMClient,
        recipient_name: str,
        description: str,
        awarding_agency: str,
    ) -> EntityResolution | None:
        prompt = (
            f"Company name: {recipient_name}\n"
            f"Awarding agency: {awarding_agency or 'unknown'}\n"
            f"Award description: {description[:300] or 'not provided'}"
        )
        try:
            response = await client.invoke(
                prompt,
                system=_LLM_SYSTEM,
                response_schema=_LLMResolutionSchema,
            )
            parsed = _LLMResolutionSchema.model_validate_json(response.content)
        except Exception:
            _logger.warning(
                "LLM resolution failed for '%s'", recipient_name, exc_info=True
            )
            return None

        return EntityResolution(
            recipient_name=recipient_name,
            ticker=parsed.ticker or None,
            exchange=parsed.exchange or None,
            confidence=parsed.confidence,
            layer=ResolutionLayer.LLM,
            reasoning=parsed.reasoning,
        )

    # ── Redis helpers ──────────────────────────────────────────────────────

    def _miss_key(self, name: str) -> str:
        digest = hashlib.sha256(name.encode()).hexdigest()[:20]
        return f"{_REDIS_KEY_PREFIX}{digest}"

    async def _cached_miss(self, name: str) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.exists(self._miss_key(name)))
        except Exception:
            return False

    async def _cache_miss(self, name: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(self._miss_key(name), "1", ex=_REDIS_MISS_TTL)
        except Exception:
            _logger.debug("Redis miss-cache write failed for '%s'", name)

    # ── DB logging ─────────────────────────────────────────────────────────

    def _log(self, award_id: str, resolution: EntityResolution) -> None:
        if self._session is None:
            return
        try:
            row = EntityResolutionRow(
                award_id=award_id,
                recipient_name=resolution.recipient_name,
                ticker=resolution.ticker,
                exchange=resolution.exchange,
                confidence=resolution.confidence,
                layer=resolution.layer.value,
                reasoning=resolution.reasoning,
                resolved_at=datetime.utcnow(),
            )
            self._session.add(row)
            self._session.commit()
        except Exception:
            _logger.warning(
                "Failed to log resolution for '%s'",
                resolution.recipient_name,
                exc_info=True,
            )
            self._session.rollback()
