"""SignalGeneratorAgent — combines sentiment and market data into signals."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, date, datetime, timedelta
from math import exp, log
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

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
# Direction label sets used by the per-ticker decay-weighted aggregation.
_BULLISH_LABELS = frozenset({SentimentLabel.BULLISH, SentimentLabel.VERY_BULLISH})
_BEARISH_LABELS = frozenset({SentimentLabel.BEARISH, SentimentLabel.VERY_BEARISH})
# Minority direction weight fraction above which a ticker group is considered MIXED.
_MIXED_DIRECTION_THRESHOLD = 0.25


# ---------------------------------------------------------------------------
# Internal schema for structured debate verdict output
# ---------------------------------------------------------------------------


class _DebateVerdictSchema(BaseModel):
    verdict: DebateVerdict
    confidence_delta: float = 0.0
    reasoning: str = ""


class _ThesisVerdictSchema(BaseModel):
    """Structured output for the EARN_PRE quarterly thesis debate."""

    direction: Literal["LONG", "SHORT", "NEUTRAL"]
    conviction: Annotated[float, Field(ge=0.0, le=1.0)]
    reasoning: str


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


def _build_thesis_bull_prompt(
    ticker: str,
    fiscal_quarter: str,
    days_until_report: int,
    beat_rate: float,
    beat_rate_source: str,
    eps_estimate: float | None,
    news_summaries: list[str],
) -> str:
    news_block = "\n".join(f"- {s}" for s in news_summaries) or "No recent news."
    eps_str = f"{eps_estimate:.2f}" if eps_estimate is not None else "N/A"
    return (
        f"You are a bullish equity analyst. {ticker} reports {fiscal_quarter} "
        f"in {days_until_report} day(s).\n"
        f"Historical beat rate: {beat_rate:.0%} ({beat_rate_source}). "
        f"Consensus EPS estimate: {eps_str}.\n"
        f"Recent news:\n{news_block}\n\n"
        "In 2-3 sentences, make the strongest possible LONG case for entering "
        "a pre-earnings position."
    )


def _build_thesis_bear_prompt(
    ticker: str,
    fiscal_quarter: str,
    days_until_report: int,
    beat_rate: float,
    beat_rate_source: str,
    eps_estimate: float | None,
    news_summaries: list[str],
) -> str:
    news_block = "\n".join(f"- {s}" for s in news_summaries) or "No recent news."
    eps_str = f"{eps_estimate:.2f}" if eps_estimate is not None else "N/A"
    return (
        f"You are a bearish equity analyst. {ticker} reports {fiscal_quarter} "
        f"in {days_until_report} day(s).\n"
        f"Historical beat rate: {beat_rate:.0%} ({beat_rate_source}). "
        f"Consensus EPS estimate: {eps_str}.\n"
        f"Recent news:\n{news_block}\n\n"
        "In 2-3 sentences, make the strongest possible SHORT case against "
        "a pre-earnings position (or for shorting)."
    )


def _build_thesis_synthesis_prompt(
    ticker: str,
    fiscal_quarter: str,
    days_until_report: int,
    bull_argument: str,
    bear_argument: str,
) -> str:
    return (
        f"You are a senior portfolio manager deciding on a pre-earnings position "
        f"for {ticker} ({fiscal_quarter}, reports in {days_until_report} day(s)).\n\n"
        f"BULL case:\n{bull_argument}\n\n"
        f"BEAR case:\n{bear_argument}\n\n"
        "Decide: LONG (enter long), SHORT (enter short), or NEUTRAL (skip).\n"
        "Also provide a conviction score 0.0-1.0 and brief reasoning.\n"
        "Return JSON with keys: direction, conviction, reasoning."
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
        """Generate trade signals — at most one per ticker per cycle.

        Groups sentiment_results by ticker, applies decay-weighted aggregation
        to produce a single synthetic SentimentResult per ticker, then calls
        _build_signal() and optionally _debate_signal() as before.

        Returns:
            ``{"trade_signals": [TradeSignal, ...]}``
        """
        sentiment_results: list[SentimentResult] = state.get("sentiment_results", [])
        market_context: dict[str, MarketSnapshot] = state.get("market_context", {})
        news_events: list[NewsEvent] = state.get("news_events", [])
        estimates: dict[str, EstimatesData] = state.get("estimates", {})

        # Build event lookup so _build_signal can access event_type and source.
        event_lookup: dict[str, NewsEvent] = {e.event_id: e for e in news_events}

        # Group by ticker so we produce at most one signal per ticker per cycle.
        groups: dict[str, list[SentimentResult]] = {}
        for sr in sentiment_results:
            groups.setdefault(sr.ticker, []).append(sr)

        now = datetime.now(UTC)
        trade_signals: list[TradeSignal] = []

        for ticker, group in groups.items():
            market_ctx = market_context.get(ticker)
            if market_ctx is None:
                self.logger.warning(
                    "No market context for ticker %s — skipping signal", ticker
                )
                continue

            agg = _aggregate_ticker_group(
                ticker,
                group,
                event_lookup,
                self.settings.article_decay_halflife_hours,
                now,
            )
            if agg is None:
                self.logger.info(
                    "Signal: %-6s  %d article(s) → MIXED or all-neutral, no signal",
                    ticker,
                    len(group),
                )
                continue

            signal = await self._build_signal(
                agg, market_ctx, event_lookup, estimates, group
            )
            if signal is None:
                continue

            _ev = event_lookup.get(signal.event_id)
            _ev_type = _ev.event_type.value if _ev else "unknown"
            self.logger.info(
                "Signal: %-6s  type=%-20s  direction=%-6s  conviction=%.3f  "
                "conf_score=%.3f  gate=%s  articles=%d%s",
                signal.ticker,
                _ev_type,
                signal.direction.value,
                signal.conviction,
                signal.confidence_score or 0.0,
                "PASS" if signal.passed_confidence_gate else "FAIL",
                len(group),
                (
                    f"  reason={signal.rejection_reason!r}"
                    if not signal.passed_confidence_gate
                    else ""
                ),
            )

            if signal.passed_confidence_gate and (
                _ev is None or _ev.event_type != EventType.EARN_PRE
            ):
                # EARN_PRE already ran _run_thesis_debate(); bypass gate debate
                signal = await self._debate_signal(signal)

            trade_signals.append(signal)

        return {"trade_signals": trade_signals}

    async def _build_signal(
        self,
        sentiment: SentimentResult,
        market_ctx: MarketSnapshot,
        event_lookup: dict[str, NewsEvent],
        estimates: dict[str, EstimatesData],
        group: list[SentimentResult] | None = None,
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
                return await self._handle_earn_pre(
                    sentiment, market_ctx, news_event, estimates,
                    group or [sentiment], event_lookup,
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
                self.logger.info(
                    "Signal: %s  label=%s → NEUTRAL, no signal",
                    sentiment.ticker,
                    sentiment.label.value,
                )
                return None

        conviction = abs(sentiment.score) * sentiment.confidence
        if conviction < self.settings.min_signal_conviction:
            self.logger.info(
                "Signal: %s  conviction=%.3f < threshold=%.3f → skipped",
                sentiment.ticker,
                conviction,
                self.settings.min_signal_conviction,
            )
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

    async def _run_thesis_debate(
        self,
        ticker: str,
        days_until_report: int,
        fiscal_quarter: str,
        beat_rate: float,
        beat_rate_source: str,
        news_summaries: list[str],
        eps_estimate: float | None,
    ) -> _ThesisVerdictSchema:
        """Run parallel bull/bear debate then synthesis for an EARN_PRE ticker."""
        bull_resp, bear_resp = await asyncio.gather(
            self._llm.quick.invoke(
                _build_thesis_bull_prompt(
                    ticker,
                    fiscal_quarter,
                    days_until_report,
                    beat_rate,
                    beat_rate_source,
                    eps_estimate,
                    news_summaries,
                )
            ),
            self._llm.quick.invoke(
                _build_thesis_bear_prompt(
                    ticker,
                    fiscal_quarter,
                    days_until_report,
                    beat_rate,
                    beat_rate_source,
                    eps_estimate,
                    news_summaries,
                )
            ),
        )
        verdict_resp = await self._llm.deep.invoke(
            _build_thesis_synthesis_prompt(
                ticker,
                fiscal_quarter,
                days_until_report,
                bull_resp.content,
                bear_resp.content,
            ),
            response_schema=_ThesisVerdictSchema,
        )
        return _ThesisVerdictSchema.model_validate(json.loads(verdict_resp.content))

    async def _handle_earn_pre(
        self,
        sentiment: SentimentResult,
        market_ctx: MarketSnapshot,
        news_event: NewsEvent | None,
        estimates: dict[str, EstimatesData],
        group: list[SentimentResult],
        event_lookup: dict[str, NewsEvent],
    ) -> TradeSignal | None:
        """Stage 1: pre-earnings thesis-debate positioning before the report.

        Runs a 3-LLM bull/bear debate (Bull + Bear in parallel, then Synthesis)
        to determine direction (LONG/SHORT/NEUTRAL) and conviction.  Beat rate
        is used as context for the debaters, not as a gate.  The debate is skipped
        (cost guard) when an open position already exists and no new non-ephemeral
        news is present in the group.
        """
        ticker = sentiment.ticker
        source = news_event.source if news_event else "unknown"

        # Step 1 — load any existing open Stage 1 position.
        existing = self._stage1_repo.load_open(ticker)

        # Step 2 — detect new (non-ephemeral) news in the group.
        new_news_present = any(
            not sr.event_id.startswith("ticker_earn_pre_") for sr in group
        )

        # Step 3 — cost guard: skip debate when position exists and no new news.
        if existing is not None and not new_news_present:
            self.logger.debug(
                "EARN_PRE %s: existing Stage1 id=%s, no new news — skipping debate",
                ticker,
                existing.id,
            )
            return None

        # Step 4 — three-tier beat rate fallback (context for the debaters).
        outcomes = self._stage1_repo.load_historical_outcomes(ticker)
        if outcomes.source == "observed" and outcomes.beat_rate is not None:
            beat_rate = outcomes.beat_rate
            beat_rate_source = "observed"
        else:
            ticker_estimates = estimates.get(ticker)
            fmp_rate = (
                ticker_estimates.historical_beat_rate
                if ticker_estimates is not None
                else None
            )
            if fmp_rate is not None:
                beat_rate = fmp_rate
                beat_rate_source = "fmp"
            else:
                beat_rate = self.settings.earn_default_beat_rate
                beat_rate_source = "default"

        # Step 5 — build news_summaries (top-5 by conviction x decay, descending).
        now = datetime.now(UTC)
        halflife = self.settings.article_decay_halflife_hours

        def _sr_weight(sr: SentimentResult) -> float:
            ev = event_lookup.get(sr.event_id)
            published_at = ev.published_at if ev else None
            return sr.confidence * _decay_weight(published_at, now, halflife)

        sorted_group = sorted(group, key=_sr_weight, reverse=True)
        news_summaries = [sr.reasoning for sr in sorted_group[:5] if sr.reasoning]

        # Step 6 — resolve calendar fields.
        report_date, fiscal_quarter = _parse_calendar_fields(
            ticker, news_event, estimates
        )
        days_until_report = max(1, (report_date - date.today()).days)
        ticker_estimates = estimates.get(ticker)
        eps_estimate = ticker_estimates.eps_estimate if ticker_estimates else None

        # Step 7 — run the thesis debate.
        verdict = await self._run_thesis_debate(
            ticker=ticker,
            days_until_report=days_until_report,
            fiscal_quarter=fiscal_quarter,
            beat_rate=beat_rate,
            beat_rate_source=beat_rate_source,
            news_summaries=news_summaries,
            eps_estimate=eps_estimate,
        )
        self.logger.info(
            "EARN_PRE %s: thesis debate → direction=%s conviction=%.2f reasoning=%r",
            ticker,
            verdict.direction,
            verdict.conviction,
            verdict.reasoning,
        )

        # Step 8 — NEUTRAL verdict → no signal.
        if verdict.direction == "NEUTRAL":
            self.logger.info("EARN_PRE %s: NEUTRAL verdict — no signal", ticker)
            return None

        new_direction = (
            SignalDirection.LONG
            if verdict.direction == "LONG"
            else SignalDirection.SHORT
        )

        # Step 9 — reaffirmation: existing position, same direction → hold.
        if existing is not None and existing.direction == new_direction.value:
            self.logger.info(
                "EARN_PRE %s: thesis reaffirms existing %s Stage1 id=%s — no action",
                ticker,
                existing.direction,
                existing.id,
            )
            return None

        # Step 10 — direction flip: existing position, opposite direction.
        if existing is not None:
            flip_threshold = self.settings.earn_thesis_flip_conviction_threshold
            if verdict.conviction > flip_threshold:
                self._stage1_repo.update_status(existing.id, Stage1Status.REVERSED)
                self.logger.info(
                    "EARN_PRE %s: flip conviction=%.2f > threshold=%.2f — "
                    "REVERSED Stage1 id=%s, emitting CLOSE",
                    ticker,
                    verdict.conviction,
                    flip_threshold,
                    existing.id,
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
                    stage1_id=existing.id,
                    passed_confidence_gate=True,
                    rationale=f"EARN_PRE thesis flip: {verdict.reasoning}",
                    model_id=self._llm.deep.model_id,
                    provider=self._llm.deep.provider,
                )
            else:
                self.logger.info(
                    "EARN_PRE %s: flip conviction=%.2f <= threshold=%.2f — "
                    "keeping existing Stage1 id=%s",
                    ticker,
                    verdict.conviction,
                    flip_threshold,
                    existing.id,
                )
                return None

        # Step 11 — open new position.
        entry_price = market_ctx.latest_close
        stop_loss = (
            entry_price * (1 - _EARN_PRE_STOP_PCT)
            if new_direction == SignalDirection.LONG
            else entry_price * (1 + _EARN_PRE_STOP_PCT)
        )

        base_size = min(
            max(
                _EARN_PRE_SIZE_MIN
                + (beat_rate - _BEAT_RATE_LONG_THRESHOLD)
                / (_BEAT_RATE_MAX - _BEAT_RATE_LONG_THRESHOLD)
                * (_EARN_PRE_SIZE_MAX - _EARN_PRE_SIZE_MIN),
                _EARN_PRE_SIZE_MIN,
            ),
            _EARN_PRE_SIZE_MAX,
        )
        size_pct = min(
            max(base_size * max(verdict.conviction, 0.25), _EARN_PRE_SIZE_MIN),
            _EARN_PRE_SIZE_MAX,
        )

        position = OpenStage1Position(
            id=str(uuid4()),
            ticker=ticker,
            direction=new_direction.value,
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
            "beat_rate=%.2f (%s) report=%s conviction=%.2f",
            ticker,
            position.id,
            new_direction.value,
            size_pct,
            beat_rate,
            beat_rate_source,
            report_date,
            verdict.conviction,
        )

        conviction = verdict.conviction
        suggested_qty = self._compute_position_size(
            ticker, conviction, market_ctx.volatility_20d
        )
        signal = TradeSignal(
            signal_id=str(uuid4()),
            event_id=sentiment.event_id,
            ticker=ticker,
            direction=new_direction,
            conviction=conviction,
            suggested_qty=suggested_qty,
            entry_price=None,
            stop_loss=stop_loss,
            take_profit=None,
            stage1_id=position.id,
            rationale=(
                f"EARN_PRE thesis: {verdict.direction} conviction={conviction:.2f} "
                f"beat_rate={beat_rate:.2f} ({beat_rate_source}) report={report_date}"
            ),
            model_id=self._llm.deep.model_id,
            provider=self._llm.deep.provider,
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
                    event_type.value.upper(),
                    ticker,
                    remaining_pct * 100,
                    open_pos.direction,
                )
                self._stage1_repo.update_status(open_pos.id, Stage1Status.CONFIRMED)
            else:
                # Existing position is in the wrong direction — reverse it.
                self.logger.info(
                    "%s %s: Stage1 REVERSED — closing %s, opening %s",
                    event_type.value.upper(),
                    ticker,
                    open_pos.direction,
                    direction.value,
                )
                self._stage1_repo.update_status(open_pos.id, Stage1Status.REVERSED)
        else:
            self.logger.info(
                "%s %s: no open Stage1 position — fresh PEAD entry",
                event_type.value.upper(),
                ticker,
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
            ticker,
            open_pos.id,
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

        self.logger.info(
            "Debate: starting %d-round bull/bear debate for %s  "
            "conviction=%.3f  conf_score=%.3f",
            self.settings.signal_debate_rounds,
            signal.ticker,
            signal.conviction,
            signal.confidence_score or 0.0,
        )

        history: list[DebateRound] = []
        for round_n in range(self.settings.signal_debate_rounds):
            bull_resp, bear_resp = await asyncio.gather(
                self._llm.quick.invoke(_build_bull_prompt(signal, history)),
                self._llm.quick.invoke(_build_bear_prompt(signal, history)),
            )
            history.append(
                DebateRound(
                    round_number=round_n,
                    bull_argument=bull_resp.content,
                    bear_argument=bear_resp.content,
                )
            )
            self.logger.info(
                "Debate: %s  round=%d/%d\n  BULL: %s\n  BEAR: %s",
                signal.ticker,
                round_n + 1,
                self.settings.signal_debate_rounds,
                bull_resp.content,
                bear_resp.content,
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
        self.logger.info(
            "Debate: %s  verdict=%s  confidence_delta=%+.3f  reasoning=%r",
            signal.ticker,
            parsed.verdict.value,
            parsed.confidence_delta,
            parsed.reasoning,
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


def _decay_weight(
    published_at: datetime | None,
    now: datetime,
    halflife_hours: float,
) -> float:
    """Exponential time-decay weight: 1.0 for a brand-new article, approaching 0
    for very old ones.  Half-life is set via ``article_decay_halflife_hours``.

    Handles both naive datetimes (treated as UTC) and timezone-aware datetimes.
    Returns 1.0 when ``published_at`` is None (no discount applied).
    """
    if published_at is None:
        return 1.0
    # Normalise to UTC-aware for safe subtraction.
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    age_hours = max(0.0, (now - published_at).total_seconds() / 3600)
    return exp(-log(2) / halflife_hours * age_hours)


def _aggregate_ticker_group(
    ticker: str,
    group: list[SentimentResult],
    event_lookup: dict[str, NewsEvent],
    halflife_hours: float,
    now: datetime,
) -> SentimentResult | None:
    """Aggregate N SentimentResults for one ticker into a single representative result.

    Weight per article = confidence * decay(age).  Returns None when:
    - All articles are NEUTRAL (no directional weight).
    - Both bullish and bearish camps each exceed ``_MIXED_DIRECTION_THRESHOLD``
      of the total directional weight (conflicting signals → skip).

    The representative ``event_id`` is taken from the highest-weight article so
    that a fresh EARN_PRE event naturally wins over stale generic news and
    ``_build_signal()`` dispatches to the correct handler.
    """
    weights: list[float] = []
    for sr in group:
        event = event_lookup.get(sr.event_id)
        pub_at = event.published_at if event is not None else None
        weights.append(sr.confidence * _decay_weight(pub_at, now, halflife_hours))

    total_weight = sum(weights)
    if total_weight == 0.0:
        return None

    pairs = list(zip(group, weights, strict=True))
    bullish_w = sum(w for sr, w in pairs if sr.label in _BULLISH_LABELS)
    bearish_w = sum(w for sr, w in pairs if sr.label in _BEARISH_LABELS)
    directional_w = bullish_w + bearish_w

    if directional_w == 0.0:
        return None  # all neutral

    if (
        bullish_w > 0.0
        and bearish_w > 0.0
        and min(bullish_w, bearish_w) / directional_w > _MIXED_DIRECTION_THRESHOLD
    ):
        return None  # conflicting directions above noise floor

    score_agg = sum(sr.score * w for sr, w in pairs) / total_weight
    confidence_agg = sum(sr.confidence for sr in group) / len(group)

    if score_agg >= 0.8:
        label = SentimentLabel.VERY_BULLISH
    elif score_agg >= 0.1:
        label = SentimentLabel.BULLISH
    elif score_agg <= -0.8:
        label = SentimentLabel.VERY_BEARISH
    elif score_agg <= -0.1:
        label = SentimentLabel.BEARISH
    else:
        label = SentimentLabel.NEUTRAL

    best_idx = max(range(len(group)), key=lambda i: weights[i])
    best = group[best_idx]

    return SentimentResult(
        event_id=best.event_id,
        ticker=ticker,
        label=label,
        score=round(score_agg, 6),
        confidence=round(confidence_agg, 6),
        reasoning=(
            f"Aggregated {len(group)} article(s) "
            f"(decay halflife={halflife_hours:.0f}h): {best.reasoning}"
        ),
        model_id=best.model_id,
        provider=best.provider,
    )


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
