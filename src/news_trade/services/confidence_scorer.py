"""ConfidenceScorer — pure-Python four-component weighted confidence scorer.

Computes a composite confidence score in [0.0, 1.0] for a trade signal by
combining four components (surprise, sentiment, coverage, source) weighted
per the event type's row in the weight matrix.

No LLM calls are made in v1.  The ``LLMClientFactory`` is accepted in the
constructor for future use (e.g. Pattern A debate synthesis) but is not
called here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from news_trade.config import Settings
from news_trade.models.events import EventType
from news_trade.models.signals import TradeSignal
from news_trade.models.surprise import EarningsSurprise, EstimatesData
from news_trade.services.estimates_renderer import EstimatesRenderer

if TYPE_CHECKING:
    from news_trade.models.sentiment import SentimentResult
    from news_trade.services.llm_client import LLMClientFactory

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Weight matrix — keyed by EventType string value for O(1) lookup.
# Each tuple is (surprise, sentiment, coverage, source).
# Rows sum to <= 1.0; EARN_MIXED is all-zero by design (never generates signals).
# ---------------------------------------------------------------------------

_WEIGHTS: dict[str, tuple[float, float, float, float]] = {
    # Fine-grained earnings & guidance
    "earn_pre": (0.00, 0.30, 0.40, 0.30),
    "earn_beat": (0.50, 0.30, 0.15, 0.05),
    "earn_miss": (0.50, 0.30, 0.15, 0.05),
    "earn_mixed": (0.00, 0.00, 0.00, 0.00),  # never passes gate
    "guid_up": (0.30, 0.50, 0.10, 0.10),
    "guid_down": (0.30, 0.50, 0.10, 0.10),
    "guid_warn": (0.20, 0.50, 0.10, 0.20),
    # Fine-grained M&A
    "ma_target": (0.00, 0.30, 0.00, 0.70),
    "ma_acquirer": (0.00, 0.30, 0.00, 0.70),
    "ma_rumour": (0.00, 0.20, 0.00, 0.80),
    "ma_break": (0.00, 0.40, 0.00, 0.60),
    "ma_counter": (0.00, 0.30, 0.00, 0.70),
    # Fine-grained regulatory
    "reg_action": (0.00, 0.40, 0.00, 0.60),
    "reg_fine": (0.00, 0.40, 0.00, 0.60),
    "reg_block": (0.00, 0.30, 0.00, 0.70),
    "reg_clear": (0.00, 0.30, 0.00, 0.70),
    "reg_license": (0.00, 0.30, 0.00, 0.70),
    # Fine-grained sector contagion
    "sector_beat_spill": (0.40, 0.30, 0.20, 0.10),
    "sector_miss_spill": (0.40, 0.30, 0.20, 0.10),
    "supply_chain": (0.30, 0.40, 0.10, 0.20),
    # Coarse legacy fallbacks
    "earnings": (0.40, 0.30, 0.15, 0.15),
    "guidance": (0.30, 0.50, 0.10, 0.10),
    "merger_acquisition": (0.00, 0.30, 0.00, 0.70),
    "macro": (0.00, 0.50, 0.10, 0.40),
    "analyst_rating": (0.00, 0.40, 0.20, 0.40),
    "sec_filing": (0.00, 0.40, 0.00, 0.60),
    "fda_approval": (0.10, 0.50, 0.10, 0.30),  # FDA is out-of-scope v1; use default
    # Default: applied when event_type is OTHER or unrecognised
    "_default": (0.10, 0.50, 0.10, 0.30),
}

# ---------------------------------------------------------------------------
# Confidence gates — minimum score required for passed_confidence_gate=True.
# EARN_MIXED gate > 1.0: mathematically impossible to pass — this is intentional.
# ---------------------------------------------------------------------------

_GATES: dict[str, float] = {
    "earn_pre": 0.45,
    "earn_beat": 0.55,
    "earn_miss": 0.55,
    "earn_mixed": 1.01,  # INTENTIONAL: gate > 1.0, never passes
    "guid_up": 0.50,
    "guid_down": 0.50,
    "guid_warn": 0.60,
    "ma_target": 0.65,
    "ma_acquirer": 0.65,
    "ma_rumour": 0.75,
    "ma_break": 0.65,
    "ma_counter": 0.65,
    "reg_action": 0.60,
    "reg_fine": 0.55,
    "reg_block": 0.65,
    "reg_clear": 0.65,
    "reg_license": 0.65,
    "sector_beat_spill": 0.50,
    "sector_miss_spill": 0.50,
    "supply_chain": 0.55,
}

_DEFAULT_GATE: float = 0.50

# ---------------------------------------------------------------------------
# Source credibility tiers — normalised lowercase source strings → score.
# Unknown sources receive _DEFAULT_SOURCE_SCORE, not zero.
# ---------------------------------------------------------------------------

_SOURCE_SCORES: dict[str, float] = {
    "sec.gov": 0.95,
    "businesswire": 0.95,
    "prnewswire": 0.95,
    "reuters": 0.80,
    "bloomberg": 0.80,
    "wsj": 0.80,
    "benzinga": 0.80,
    "yahoo_finance": 0.60,
    "cnbc": 0.60,
    "marketwatch": 0.60,
    "twitter": 0.17,
    "reddit": 0.17,
    "rss": 0.50,
}

_DEFAULT_SOURCE_SCORE: float = 0.30


# ---------------------------------------------------------------------------
# ConfidenceScorer
# ---------------------------------------------------------------------------


class ConfidenceScorer:
    """Computes composite confidence scores and applies per-event-type gates.

    All four components are computed in pure Python.  No LLM calls in v1.

    Args:
        settings:    Application settings; provides ``earn_min_analyst_count``.
        llm_factory: Reserved for future Pattern A use; not called in v1.
        renderer:    Inject a custom ``EstimatesRenderer`` for testing; if None
                     a default instance is created automatically.
    """

    def __init__(
        self,
        settings: Settings,
        llm_factory: LLMClientFactory | None = None,
        renderer: EstimatesRenderer | None = None,
    ) -> None:
        self._settings = settings
        self._llm_factory = llm_factory  # reserved; unused in v1
        self._renderer = renderer or EstimatesRenderer()

    # ------------------------------------------------------------------
    # Component scorers — all return float in [0.0, 1.0]
    # ------------------------------------------------------------------

    def _score_surprise(
        self,
        estimates: EstimatesData | None,
        earnings_surprise: EarningsSurprise | None = None,
    ) -> float:
        """Surprise component score.

        Post-announcement path (``earnings_surprise`` present):
            min(max(|eps_sigma|, |rev_sigma|) / 3.0, 1.0)

        Pre-announcement path (only ``estimates`` present):
            min(|compute_pre_surprise_delta(estimates)|, 1.0)
            The delta is already normalised to [-1, 1], so its magnitude
            serves as a proxy for expected sigma.

        Returns 0.0 when no surprise data is available.
        Post-announcement path takes precedence when both are provided.
        """
        if earnings_surprise is not None:
            eps_sigma = abs(earnings_surprise.eps.sigma_surprise)
            rev_sigma = abs(earnings_surprise.revenue.sigma_surprise)
            return min(max(eps_sigma, rev_sigma) / 3.0, 1.0)
        if estimates is not None:
            delta = self._renderer.compute_pre_surprise_delta(estimates)
            return min(abs(delta), 1.0)
        return 0.0

    def _score_sentiment(self, sentiment: SentimentResult | None) -> float:
        """Sentiment component: ``confidence * |score|``. Returns 0.0 if None."""
        if sentiment is None:
            return 0.0
        return sentiment.confidence * abs(sentiment.score)

    def _score_coverage(self, analyst_count: int | None) -> float:
        """Coverage component.

        Formula: min(analyst_count / 10.0, 1.0)
        Floor:   0.1 when analyst_count < earn_min_analyst_count or None.

        The floor of 0.1 (not 0.0) avoids an unfair zero-score penalty for
        event types (M&A, regulatory) that naturally lack analyst coverage.
        """
        if (
            analyst_count is None
            or analyst_count < self._settings.earn_min_analyst_count
        ):
            return 0.1
        return min(analyst_count / 10.0, 1.0)

    def _score_source(self, source: str) -> float:
        """Source credibility tier lookup (case-insensitive).

        Returns 0.30 for unknown sources.
        """
        return _SOURCE_SCORES.get(source.lower(), _DEFAULT_SOURCE_SCORE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(
        self,
        event_type: EventType,
        sentiment: SentimentResult | None = None,
        estimates: EstimatesData | None = None,
        earnings_surprise: EarningsSurprise | None = None,
        analyst_count: int | None = None,
        source: str = "unknown",
    ) -> float:
        """Compute the weighted composite confidence score.

        Args:
            event_type:        Fine-grained or coarse EventType of the news event.
            sentiment:         SentimentResult from SentimentAnalystAgent; None allowed.
            estimates:         Pre-announcement EstimatesData; used for surprise_score.
            earnings_surprise: Post-announcement EarningsSurprise; takes precedence
                               over estimates for surprise when both are present.
            analyst_count:     Analyst coverage count; None yields the minimum floor.
            source:            Normalised source string (e.g. 'benzinga', 'reuters').

        Returns:
            Float in [0.0, 1.0].
        """
        key = str(event_type)
        w_surprise, w_sentiment, w_coverage, w_source = _WEIGHTS.get(
            key, _WEIGHTS["_default"]
        )

        raw = (
            w_surprise * self._score_surprise(estimates, earnings_surprise)
            + w_sentiment * self._score_sentiment(sentiment)
            + w_coverage * self._score_coverage(analyst_count)
            + w_source * self._score_source(source)
        )
        return max(0.0, min(1.0, raw))

    def apply_gate(
        self,
        signal: TradeSignal,
        event_type: EventType,
        confidence_score: float,
    ) -> TradeSignal:
        """Stamp confidence_score onto a signal and evaluate the confidence gate.

        Returns a *new* ``TradeSignal`` via ``model_copy()`` — the original is
        not modified.  Sets ``confidence_score``, ``passed_confidence_gate``,
        and ``rejection_reason``.

        The EARN_MIXED gate is 1.01 by design: it is mathematically impossible
        to satisfy and the signal will always be rejected with a reason.

        Args:
            signal:           The signal to evaluate.
            event_type:       Determines which confidence gate to apply.
            confidence_score: Value returned by ``score()``.

        Returns:
            New ``TradeSignal`` with gate fields populated.
        """
        gate = _GATES.get(str(event_type), _DEFAULT_GATE)
        passed = confidence_score >= gate
        reason: str | None = None
        if not passed:
            reason = (
                f"confidence {confidence_score:.3f} below gate "
                f"{gate:.2f} for {event_type}"
            )
            _logger.debug("Signal %s rejected: %s", signal.signal_id, reason)

        return signal.model_copy(
            update={
                "confidence_score": confidence_score,
                "passed_confidence_gate": passed,
                "rejection_reason": reason,
            }
        )
