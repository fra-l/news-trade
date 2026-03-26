"""SignalGeneratorAgent — combines sentiment and market data into signals."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel

from news_trade.agents.base import BaseAgent
from news_trade.models.market import MarketSnapshot
from news_trade.models.sentiment import SentimentLabel, SentimentResult
from news_trade.models.signals import (
    DebateResult,
    DebateRound,
    DebateVerdict,
    SignalDirection,
    TradeSignal,
)

if TYPE_CHECKING:
    from news_trade.config import Settings
    from news_trade.services.event_bus import EventBus
    from news_trade.services.llm_client import LLMClientFactory


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
        - Optionally run a bull/bear debate for high-confidence signals.
        - Emit TradeSignal instances for downstream risk validation.
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        llm: LLMClientFactory,
    ) -> None:
        super().__init__(settings, event_bus)
        self._llm = llm

    async def run(self, state: dict) -> dict:  # type: ignore[type-arg]
        """Generate trade signals from sentiment results and market context.

        Returns:
            ``{"trade_signals": [TradeSignal, ...]}``
        """
        sentiment_results: list[SentimentResult] = state.get("sentiment_results", [])
        market_context: dict[str, MarketSnapshot] = state.get("market_context", {})

        trade_signals: list[TradeSignal] = []
        for sentiment in sentiment_results:
            market_ctx = market_context.get(sentiment.ticker)
            if market_ctx is None:
                self.logger.warning(
                    "No market context for ticker %s — skipping signal",
                    sentiment.ticker,
                )
                continue

            signal = self._build_signal(sentiment, market_ctx)
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
    ) -> TradeSignal | None:
        """Create a TradeSignal from a sentiment result and market snapshot.

        Returns None if the label is neutral or conviction is below the configured
        threshold.

        """
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

        return TradeSignal(
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
