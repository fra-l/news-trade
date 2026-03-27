"""SignalGeneratorAgent — combines sentiment and market data into signals."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel

from news_trade.agents.base import BaseAgent
from news_trade.models.events import EventType, NewsEvent
from news_trade.models.market import MarketSnapshot
from news_trade.models.positions import OpenStage1Position, Stage1Status
from news_trade.models.sentiment import SentimentLabel, SentimentResult
from news_trade.models.signals import (
    DebateResult,
    DebateRound,
    DebateVerdict,
    SignalDirection,
    TradeSignal,
)
from news_trade.models.surprise import EstimatesData

if TYPE_CHECKING:
    from news_trade.config import Settings
    from news_trade.services.confidence_scorer import ConfidenceScorer
    from news_trade.services.event_bus import EventBus
    from news_trade.services.llm_client import LLMClientFactory
    from news_trade.services.stage1_repository import Stage1Repository

# EARN_PRE beat-rate bounds — outside this range the signal is too uncertain.
_BEAT_RATE_MIN = 0.55
_BEAT_RATE_MAX = 0.85
# Beat rate at which we switch from short to long direction.
_BEAT_RATE_LONG_THRESHOLD = 0.60
# Size bounds for EARN_PRE positions (fraction of portfolio).
_EARN_PRE_SIZE_MIN = 0.25
_EARN_PRE_SIZE_MAX = 0.40
# Fixed stop-loss percentage for pre-earnings positions.
_EARN_PRE_STOP_PCT = 0.04
# Post-announcement fresh-PEAD size when no Stage 1 position exists.
_EARN_POST_FRESH_SIZE_FACTOR = 0.75
# Regex to extract report date from EarningsCalendarAgent headline.
_REPORT_DATE_RE = re.compile(r"\bon (\d{4}-\d{2}-\d{2})\b")
# Regex to extract fiscal quarter from EarningsCalendarAgent headline.
_FISCAL_QTR_RE = re.compile(r"report\s+(Q\d\s+\d{4})\s+on\b")


# ---------------------------------------------------------------------------
# Internal schema for structured debate verdict output
# ---------------------------------------------------------------------------


class _DebateVerdictSchema(BaseModel):
    verdict: DebateVerdict
    confidence_delta: float = 0.0
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def _round_summary(rounds: list[DebateRound]) -> str:
    if not rounds:
        return ""
    lines: list[str] = []
    for r in rounds:
        lines.append(
            f"Round {r.round_number + 1}:\n"
            f"  Bull: {r.bull_argument}\n"
            f"  Bear: {r.bear_argument}"
        )
    return "\n".join(lines)


def _build_bull_prompt(signal: TradeSignal, history: list[DebateRound]) -> str:
    prior = _round_summary(history)
    prior_section = f"\n\nPrior debate rounds:\n{prior}" if prior else ""
    return (
        f"You are a bullish equity analyst arguing in favour of a {signal.direction} "
        f"position on {signal.ticker}.\n"
        f"Signal conviction: {signal.conviction:.2f}. "
        f"Rationale: {signal.rationale or 'N/A'}.{prior_section}\n\n"
        "In 2-3 sentences, make the strongest possible bull case for this trade."
    )


def _build_bear_prompt(signal: TradeSignal, history: list[DebateRound]) -> str:
    prior = _round_summary(history)
    prior_section = f"\n\nPrior debate rounds:\n{prior}" if prior else ""
    return (
        f"You are a bearish equity analyst arguing against a {signal.direction} "
        f"position on {signal.ticker}.\n"
        f"Signal conviction: {signal.conviction:.2f}. "
        f"Rationale: {signal.rationale or 'N/A'}.{prior_section}\n\n"
        "In 2-3 sentences, make the strongest possible bear case against this trade."
    )


def _build_synthesis_prompt(signal: TradeSignal, history: list[DebateRound]) -> str:
    debate_text = _round_summary(history)
    return (
        f"You are a senior portfolio manager synthesising a bull/bear debate about "
        f"a {signal.direction} position on {signal.ticker} "
        f"(conviction={signal.conviction:.2f}).\n\n"
        f"Debate transcript:\n{debate_text}\n\n"
        "Based on the arguments above, decide:\n"
        "  CONFIRM — proceed with the signal unchanged\n"
        "  REDUCE  — proceed but halve position size\n"
        "  REJECT  — block the signal (bear thesis dominated)\n\n"
        "Return your verdict and a confidence_delta in the range [-0.20, +0.10] "
        "that reflects how the debate changed your view of the signal quality. "
        "Also provide brief reasoning."
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class SignalGeneratorAgent(BaseAgent):
    """Generates actionable trade signals from sentiment + market context.

    Responsibilities:
        - Pair each SentimentResult with the corresponding market context.
        - Apply conviction thresholds and directional logic.
        - Compute suggested position size, stop-loss, and take-profit.
        - Run ConfidenceScorer to set confidence_score and passed_confidence_gate.
        - Optionally run a bull/bear debate for high-confidence signals.
        - Emit TradeSignal instances for downstream risk validation.
        - Handle the two-stage EARN_PRE / EARN_BEAT / EARN_MISS / EARN_MIXED logic
          via Stage1Repository.
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        llm: LLMClientFactory,
        scorer: ConfidenceScorer,
        stage1_repo: Stage1Repository,
    ) -> None:
        super().__init__(settings, event_bus)
        self._llm = llm
        self._scorer = scorer
        self._stage1_repo = stage1_repo

    async def run(self, state: dict) -> dict:  # type: ignore[type-arg]
        """Generate trade signals from sentiment results and market context.

        Returns:
            ``{"trade_signals": [TradeSignal, ...]}``
        """
        sentiment_results: list[SentimentResult] = state.get("sentiment_results", [])
        market_context: dict[str, MarketSnapshot] = state.get("market_context", {})
        news_events: list[NewsEvent] = state.get("news_events", [])
        estimates: dict[str, EstimatesData] = state.get("estimates", {})

        # Build event lookup so _build_signal can access event_type and source.
        event_lookup: dict[str, NewsEvent] = {e.event_id: e for e in news_events}

        trade_signals: list[TradeSignal] = []
        for sentiment in sentiment_results:
            market_ctx = market_context.get(sentiment.ticker)
            if market_ctx is None:
                self.logger.warning(
                    "No market context for ticker %s — skipping signal",
                    sentiment.ticker,
                )
                continue

            signal = self._build_signal(
                sentiment, market_ctx, event_lookup, estimates
            )
            if signal is None:
                continue

            if signal.passed_confidence_gate:
                signal = await self._debate_signal(signal)

            trade_signals.append(signal)

        return {"trade_signals": trade_signals}

    def _build_signal(
        self,
        sentiment: SentimentResult,
        market_ctx: MarketSnapshot,
        event_lookup: dict[str, NewsEvent],
        estimates: dict[str, EstimatesData],
    ) -> TradeSignal | None:
        """Create a TradeSignal from a sentiment result and market snapshot.

        Dispatches to specialised handlers for EARN_* event types.
        For all other events: maps label → direction, applies conviction threshold,
        scores, and gates.  Returns None when no signal should be generated.
        """
        news_event = event_lookup.get(sentiment.event_id)
        event_type = news_event.event_type if news_event else EventType.OTHER
        source = news_event.source if news_event else "unknown"

        # Dispatch EARN_* event types to dedicated two-stage handlers.
        match event_type:
            case EventType.EARN_PRE:
                return self._handle_earn_pre(
                    sentiment, market_ctx, news_event, estimates
                )
            case EventType.EARN_BEAT | EventType.EARN_MISS:
                return self._handle_earn_post(
                    sentiment, market_ctx, news_event, event_type
                )
            case EventType.EARN_MIXED:
                return self._handle_earn_mixed(sentiment, market_ctx, news_event)
            case _:
                pass  # fall through to generic label-based logic

        match sentiment.label:
            case SentimentLabel.BULLISH | SentimentLabel.VERY_BULLISH:
                direction = SignalDirection.LONG
            case SentimentLabel.BEARISH | SentimentLabel.VERY_BEARISH:
                direction = SignalDirection.SHORT
            case _:
                return None

        conviction = abs(sentiment.score) * sentiment.confidence
        if conviction < self.settings.min_signal_conviction:
            return None

        suggested_qty = self._compute_position_size(
            sentiment.ticker, conviction, market_ctx.volatility_20d
        )
        stop_loss = self._compute_stop_loss(
            market_ctx.latest_close, market_ctx.volatility_20d, direction
        )

        signal = TradeSignal(
            signal_id=str(uuid4()),
            event_id=sentiment.event_id,
            ticker=sentiment.ticker,
            direction=direction,
            conviction=conviction,
            suggested_qty=suggested_qty,
            entry_price=None,
            stop_loss=stop_loss,
            take_profit=None,
            rationale=sentiment.reasoning,
            model_id=self._llm.quick.model_id,
            provider=self._llm.quick.provider,
        )

        score = self._scorer.score(
            event_type=event_type,
            sentiment=sentiment,
            source=source,
        )
        return self._scorer.apply_gate(signal, event_type, score)

    # ------------------------------------------------------------------
    # EARN_* two-stage handlers
    # ------------------------------------------------------------------

    def _handle_earn_pre(
        self,
        sentiment: SentimentResult,
        market_ctx: MarketSnapshot,
        news_event: NewsEvent | None,
        estimates: dict[str, EstimatesData],
    ) -> TradeSignal | None:
        """Stage 1: pre-earnings positioning 2-5 days before the report.

        Sizes from the historical beat rate: long when beat_rate >= 0.60,
        short otherwise.  Skips when beat_rate is outside [0.55, 0.85].
        Persists an OpenStage1Position to SQLite so Stage 2 can confirm/reverse.
        """
        ticker = sentiment.ticker
        source = news_event.source if news_event else "unknown"

        outcomes = self._stage1_repo.load_historical_outcomes(ticker)
        if outcomes.source == "observed" and outcomes.beat_rate is not None:
            beat_rate = outcomes.beat_rate
        else:
            ticker_estimates = estimates.get(ticker)
            fmp_rate = (
                ticker_estimates.historical_beat_rate
                if ticker_estimates is not None
                else None
            )
            if fmp_rate is not None:
                beat_rate = fmp_rate
                self.logger.info(
                    "EARN_PRE %s: using FMP historical beat_rate=%.2f "
                    "(observed sample too small: %d quarters)",
                    ticker, beat_rate, outcomes.sample_size,
                )
            else:
                beat_rate = self.settings.earn_default_beat_rate
                self.logger.info(
                    "EARN_PRE %s: insufficient observed history (%d samples), "
                    "no FMP data — using default beat_rate=%.2f",
                    ticker, outcomes.sample_size, beat_rate,
                )

        if beat_rate < _BEAT_RATE_MIN or beat_rate > _BEAT_RATE_MAX:
            self.logger.info(
                "EARN_PRE %s: beat_rate=%.2f outside [%.2f, %.2f] — skipping",
                ticker, beat_rate, _BEAT_RATE_MIN, _BEAT_RATE_MAX,
            )
            return None

        direction = (
            SignalDirection.LONG
            if beat_rate >= _BEAT_RATE_LONG_THRESHOLD
            else SignalDirection.SHORT
        )
        size_pct = min(
            max(
                _EARN_PRE_SIZE_MIN
                + (beat_rate - _BEAT_RATE_LONG_THRESHOLD)
                / (_BEAT_RATE_MAX - _BEAT_RATE_LONG_THRESHOLD)
                * (_EARN_PRE_SIZE_MAX - _EARN_PRE_SIZE_MIN),
                _EARN_PRE_SIZE_MIN,
            ),
            _EARN_PRE_SIZE_MAX,
        )

        entry_price = market_ctx.latest_close
        if direction == SignalDirection.LONG:
            stop_loss = entry_price * (1 - _EARN_PRE_STOP_PCT)
        else:
            stop_loss = entry_price * (1 + _EARN_PRE_STOP_PCT)

        # Resolve report_date and fiscal_quarter from estimates or headline.
        report_date, fiscal_quarter = _parse_calendar_fields(
            ticker, news_event, estimates
        )

        position = OpenStage1Position(
            id=str(uuid4()),
            ticker=ticker,
            direction=direction.value,
            size_pct=size_pct,
            entry_price=entry_price,
            opened_at=datetime.utcnow(),
            expected_report_date=report_date,
            fiscal_quarter=fiscal_quarter,
            historical_beat_rate=beat_rate,
        )
        self._stage1_repo.persist(position)
        self.logger.info(
            "EARN_PRE %s: persisted Stage1 id=%s direction=%s size_pct=%.2f "
            "beat_rate=%.2f report=%s",
            ticker, position.id, direction.value, size_pct, beat_rate, report_date,
        )

        conviction = abs(sentiment.score) * sentiment.confidence
        suggested_qty = self._compute_position_size(
            ticker, conviction, market_ctx.volatility_20d
        )
        signal = TradeSignal(
            signal_id=str(uuid4()),
            event_id=sentiment.event_id,
            ticker=ticker,
            direction=direction,
            conviction=conviction,
            suggested_qty=suggested_qty,
            entry_price=None,
            stop_loss=stop_loss,
            take_profit=None,
            stage1_id=position.id,
            rationale=(
                f"EARN_PRE: beat_rate={beat_rate:.2f} size_pct={size_pct:.2f} "
                f"report={report_date}"
            ),
            model_id=self._llm.quick.model_id,
            provider=self._llm.quick.provider,
        )
        score = self._scorer.score(
            event_type=EventType.EARN_PRE,
            sentiment=sentiment,
            source=source,
        )
        return self._scorer.apply_gate(signal, EventType.EARN_PRE, score)

    def _handle_earn_post(
        self,
        sentiment: SentimentResult,
        market_ctx: MarketSnapshot,
        news_event: NewsEvent | None,
        event_type: EventType,
    ) -> TradeSignal | None:
        """Stage 2: post-announcement PEAD signal on EARN_BEAT or EARN_MISS.

        Loads any open Stage 1 position and confirms, reverses, or opens a
        fresh PEAD entry as appropriate.
        """
        ticker = sentiment.ticker
        source = news_event.source if news_event else "unknown"
        direction = (
            SignalDirection.LONG
            if event_type == EventType.EARN_BEAT
            else SignalDirection.SHORT
        )

        open_pos = self._stage1_repo.load_open(ticker)
        stage1_id: str | None = None

        if open_pos is not None:
            stage1_id = open_pos.id
            stage1_agrees = open_pos.direction == direction.value

            if stage1_agrees:
                # Add remaining size to the existing position.
                remaining_pct = 1.0 - open_pos.size_pct
                self.logger.info(
                    "%s %s: Stage1 CONFIRMED — adding %.0f%% to existing %s",
                    event_type.value.upper(), ticker,
                    remaining_pct * 100, open_pos.direction,
                )
                self._stage1_repo.update_status(open_pos.id, Stage1Status.CONFIRMED)
            else:
                # Existing position is in the wrong direction — reverse it.
                self.logger.info(
                    "%s %s: Stage1 REVERSED — closing %s, opening %s",
                    event_type.value.upper(), ticker,
                    open_pos.direction, direction.value,
                )
                self._stage1_repo.update_status(open_pos.id, Stage1Status.REVERSED)
        else:
            self.logger.info(
                "%s %s: no open Stage1 position — fresh PEAD entry",
                event_type.value.upper(), ticker,
            )

        conviction = abs(sentiment.score) * sentiment.confidence
        suggested_qty = self._compute_position_size(
            ticker, conviction, market_ctx.volatility_20d
        )
        if open_pos is None:
            # Fresh PEAD: scale down to avoid over-sizing.
            suggested_qty = max(1, int(suggested_qty * _EARN_POST_FRESH_SIZE_FACTOR))

        stop_loss = self._compute_stop_loss(
            market_ctx.latest_close, market_ctx.volatility_20d, direction
        )
        signal = TradeSignal(
            signal_id=str(uuid4()),
            event_id=sentiment.event_id,
            ticker=ticker,
            direction=direction,
            conviction=conviction,
            suggested_qty=suggested_qty,
            entry_price=None,
            stop_loss=stop_loss,
            take_profit=None,
            stage1_id=stage1_id,
            horizon_days=self.settings.pead_horizon_days,
            rationale=sentiment.reasoning,
            model_id=self._llm.quick.model_id,
            provider=self._llm.quick.provider,
        )
        score = self._scorer.score(
            event_type=event_type,
            sentiment=sentiment,
            source=source,
        )
        return self._scorer.apply_gate(signal, event_type, score)

    def _handle_earn_mixed(
        self,
        sentiment: SentimentResult,
        market_ctx: MarketSnapshot,
        news_event: NewsEvent | None,
    ) -> TradeSignal | None:
        """EARN_MIXED: exit any open Stage 1 position; no new directional signal.

        The ConfidenceScorer gate for EARN_MIXED is 1.01 (always fails by design),
        so an CLOSE signal is emitted directly with passed_confidence_gate=True to
        bypass the gate — exiting an existing position must not be blocked.
        """
        ticker = sentiment.ticker
        open_pos = self._stage1_repo.load_open(ticker)

        if open_pos is None:
            self.logger.info(
                "EARN_MIXED %s: no open Stage1 position — no signal emitted", ticker
            )
            return None

        self._stage1_repo.update_status(open_pos.id, Stage1Status.EXITED)
        self.logger.info(
            "EARN_MIXED %s: Stage1 id=%s EXITED — emitting CLOSE signal",
            ticker, open_pos.id,
        )
        return TradeSignal(
            signal_id=str(uuid4()),
            event_id=sentiment.event_id,
            ticker=ticker,
            direction=SignalDirection.CLOSE,
            conviction=1.0,
            suggested_qty=0,
            entry_price=None,
            stop_loss=None,
            take_profit=None,
            stage1_id=open_pos.id,
            rationale="EARN_MIXED: exiting Stage 1 position",
            model_id=self._llm.quick.model_id,
            provider=self._llm.quick.provider,
            # EXIT signals bypass the confidence gate — closing a position must
            # not be blocked by the gate that was designed for new entries.
            passed_confidence_gate=True,
        )

    # ------------------------------------------------------------------
    # Shared sizing helpers
    # ------------------------------------------------------------------

    def _compute_position_size(
        self, ticker: str, conviction: float, volatility: float
    ) -> int:
        """Determine the number of shares to trade.

        Uses a volatility-adjusted sizing model scaled by conviction.
        Higher volatility → smaller position; higher conviction → larger position.
        """
        return max(1, int(conviction / max(volatility, 0.01) * 10))

    def _compute_stop_loss(
        self, entry_price: float, volatility: float, direction: SignalDirection
    ) -> float:
        """Calculate a volatility-based stop-loss level.

        Uses 2x the daily volatility proxy as the offset from entry.
        """
        offset = entry_price * volatility * 2
        if direction == SignalDirection.LONG:
            return entry_price - offset
        return entry_price + offset

    async def _debate_signal(self, signal: TradeSignal) -> TradeSignal:
        """Run the bull/bear debate for a high-confidence signal.

        Returns the signal unchanged if the debate feature is disabled or the
        signal's confidence_score is below the configured threshold.
        """
        if self.settings.signal_debate_rounds == 0:
            return signal

        if (
            signal.confidence_score is None
            or signal.confidence_score < self.settings.signal_debate_threshold
        ):
            return signal

        history: list[DebateRound] = []
        for round_n in range(self.settings.signal_debate_rounds):
            bull_resp = await self._llm.quick.invoke(
                _build_bull_prompt(signal, history)
            )
            bear_resp = await self._llm.quick.invoke(
                _build_bear_prompt(signal, history)
            )
            history.append(
                DebateRound(
                    round_number=round_n,
                    bull_argument=bull_resp.content,
                    bear_argument=bear_resp.content,
                )
            )

        verdict_resp = await self._llm.deep.invoke(
            _build_synthesis_prompt(signal, history),
            response_schema=_DebateVerdictSchema,
        )
        parsed = _DebateVerdictSchema.model_validate(json.loads(verdict_resp.content))
        result = DebateResult(
            verdict=parsed.verdict,
            confidence_delta=parsed.confidence_delta,
            rounds=history,
        )

        new_score = (signal.confidence_score or 0.0) + result.confidence_delta
        updates: dict[str, object] = {
            "debate_result": result,
            "confidence_score": new_score,
        }
        if result.verdict == DebateVerdict.REJECT:
            updates["passed_confidence_gate"] = False
            updates["rejection_reason"] = "Debate: bear thesis dominated"
        elif result.verdict == DebateVerdict.REDUCE:
            updates["suggested_qty"] = max(1, signal.suggested_qty // 2)

        return signal.model_copy(update=updates)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _parse_calendar_fields(
    ticker: str,
    news_event: NewsEvent | None,
    estimates: dict[str, EstimatesData],
) -> tuple[date, str]:
    """Extract report_date and fiscal_quarter for an EARN_PRE signal.

    Priority:
    1. ``estimates[ticker]`` — populated by EarningsCalendarAgent (most reliable).
    2. Parse from ``news_event.headline`` (format: "… {quarter} on {date} (…)").
    3. Fall back to today + 3 days / "unknown" if both fail.
    """
    est = estimates.get(ticker)
    if est is not None:
        return est.report_date, est.fiscal_period

    if news_event is not None:
        date_match = _REPORT_DATE_RE.search(news_event.headline)
        qtr_match = _FISCAL_QTR_RE.search(news_event.headline)
        if date_match and qtr_match:
            try:
                return (
                    date.fromisoformat(date_match.group(1)),
                    qtr_match.group(1),
                )
            except ValueError:
                pass

    # Last-resort fallback.
    fallback_date = date.today() + timedelta(days=3)
    return fallback_date, "unknown"
