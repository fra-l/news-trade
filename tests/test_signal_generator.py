"""Unit tests for SignalGeneratorAgent."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from news_trade.agents.signal_generator import SignalGeneratorAgent
from news_trade.config import Settings
from news_trade.models.market import MarketSnapshot
from news_trade.models.sentiment import SentimentLabel, SentimentResult
from news_trade.models.signals import (
    DebateVerdict,
    SignalDirection,
    TradeSignal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 26, 12, 0, 0, tzinfo=UTC)


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        anthropic_api_key="test-key",
        llm_provider="anthropic",
        llm_quick_model="claude-haiku-4-5-20251001",
        llm_deep_model="claude-sonnet-4-6",
        min_signal_conviction=0.60,
        signal_debate_rounds=0,
        signal_debate_threshold=0.70,
    )
    return Settings(**(defaults | kwargs))


def _make_sentiment(**kwargs) -> SentimentResult:
    defaults: dict[str, object] = dict(
        event_id="evt-1",
        ticker="AAPL",
        label=SentimentLabel.BULLISH,
        score=0.85,
        confidence=0.90,
        reasoning="Strong beat expected.",
    )
    return SentimentResult(**(defaults | kwargs))


def _make_market(**kwargs) -> MarketSnapshot:
    defaults: dict[str, object] = dict(
        ticker="AAPL",
        latest_close=200.0,
        volume=1_000_000,
        vwap=199.5,
        volatility_20d=0.25,
        fetched_at=NOW,
    )
    return MarketSnapshot(**(defaults | kwargs))


def _make_signal(**kwargs) -> TradeSignal:
    defaults: dict[str, object] = dict(
        signal_id="sig-1",
        event_id="evt-1",
        ticker="AAPL",
        direction=SignalDirection.LONG,
        conviction=0.80,
        suggested_qty=10,
    )
    return TradeSignal(**(defaults | kwargs))


def _make_agent(settings: Settings | None = None) -> SignalGeneratorAgent:
    s = settings or _make_settings()
    event_bus = MagicMock()
    llm_quick = MagicMock()
    llm_quick.model_id = "claude-haiku-4-5-20251001"
    llm_quick.provider = "anthropic"
    llm_deep = MagicMock()
    llm_deep.model_id = "claude-sonnet-4-6"
    llm_deep.provider = "anthropic"
    llm_factory = MagicMock()
    llm_factory.quick = llm_quick
    llm_factory.deep = llm_deep
    return SignalGeneratorAgent(settings=s, event_bus=event_bus, llm=llm_factory)


# ---------------------------------------------------------------------------
# TestBuildSignal
# ---------------------------------------------------------------------------


class TestBuildSignal:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    def test_bullish_sentiment_produces_long(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.BULLISH, score=0.85, confidence=0.90
        )
        market = _make_market()
        signal = self.agent._build_signal(sentiment, market)
        assert signal is not None
        assert signal.direction == SignalDirection.LONG
        assert signal.ticker == "AAPL"

    def test_very_bullish_produces_long(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.VERY_BULLISH, score=0.95, confidence=0.95
        )
        signal = self.agent._build_signal(sentiment, _make_market())
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    def test_bearish_sentiment_produces_short(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.BEARISH, score=-0.80, confidence=0.85
        )
        signal = self.agent._build_signal(sentiment, _make_market())
        assert signal is not None
        assert signal.direction == SignalDirection.SHORT

    def test_very_bearish_produces_short(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.VERY_BEARISH, score=-0.90, confidence=0.90,
        )
        signal = self.agent._build_signal(sentiment, _make_market())
        assert signal is not None
        assert signal.direction == SignalDirection.SHORT

    def test_neutral_returns_none(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.NEUTRAL, score=0.0, confidence=0.50
        )
        signal = self.agent._build_signal(sentiment, _make_market())
        assert signal is None

    def test_below_conviction_threshold_returns_none(self):
        # abs(score) * confidence = 0.3 * 0.5 = 0.15 < 0.60
        sentiment = _make_sentiment(
            label=SentimentLabel.BULLISH, score=0.30, confidence=0.50
        )
        signal = self.agent._build_signal(sentiment, _make_market())
        assert signal is None

    def test_signal_fields_populated(self):
        sentiment = _make_sentiment()
        market = _make_market(latest_close=150.0, volatility_20d=0.20)
        signal = self.agent._build_signal(sentiment, market)
        assert signal is not None
        assert signal.event_id == sentiment.event_id
        assert signal.rationale == sentiment.reasoning
        assert signal.model_id == "claude-haiku-4-5-20251001"
        assert signal.provider == "anthropic"
        assert signal.stop_loss is not None

    def test_stop_loss_long_below_entry(self):
        market = _make_market(latest_close=100.0, volatility_20d=0.10)
        sentiment = _make_sentiment(
            label=SentimentLabel.BULLISH, score=0.9, confidence=0.9
        )
        signal = self.agent._build_signal(sentiment, market)
        assert signal is not None
        assert signal.stop_loss < 100.0  # long stop below entry

    def test_stop_loss_short_above_entry(self):
        market = _make_market(latest_close=100.0, volatility_20d=0.10)
        sentiment = _make_sentiment(
            label=SentimentLabel.BEARISH, score=-0.9, confidence=0.9
        )
        signal = self.agent._build_signal(sentiment, market)
        assert signal is not None
        assert signal.stop_loss > 100.0  # short stop above entry

    def test_position_size_positive(self):
        market = _make_market(volatility_20d=0.25)
        sentiment = _make_sentiment()
        signal = self.agent._build_signal(sentiment, market)
        assert signal is not None
        assert signal.suggested_qty >= 1


# ---------------------------------------------------------------------------
# TestRun
# ---------------------------------------------------------------------------


class TestRun:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    @pytest.mark.asyncio
    async def test_no_market_context_skips_signal(self):
        sentiment = _make_sentiment(ticker="AAPL")
        state = {
            "sentiment_results": [sentiment],
            "market_context": {},  # no AAPL entry
        }
        result = await self.agent.run(state)
        assert result["trade_signals"] == []

    @pytest.mark.asyncio
    async def test_neutral_sentiment_produces_no_signal(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.NEUTRAL, score=0.0, confidence=0.50
        )
        market = _make_market()
        state = {
            "sentiment_results": [sentiment],
            "market_context": {"AAPL": market},
        }
        result = await self.agent.run(state)
        assert result["trade_signals"] == []

    @pytest.mark.asyncio
    async def test_valid_sentiment_produces_signal(self):
        sentiment = _make_sentiment()
        market = _make_market()
        state = {
            "sentiment_results": [sentiment],
            "market_context": {"AAPL": market},
        }
        result = await self.agent.run(state)
        signals = result["trade_signals"]
        assert len(signals) == 1
        assert signals[0].ticker == "AAPL"
        assert signals[0].direction == SignalDirection.LONG


# ---------------------------------------------------------------------------
# TestDebateSignalDisabled
# ---------------------------------------------------------------------------


class TestDebateSignalDisabled:
    def setup_method(self) -> None:
        self.agent = _make_agent(_make_settings(signal_debate_rounds=0))

    @pytest.mark.asyncio
    async def test_no_llm_calls_when_disabled(self):
        signal = _make_signal(passed_confidence_gate=True, confidence_score=0.85)
        result = await self.agent._debate_signal(signal)
        # signal returned unchanged
        assert result is signal
        # no LLM calls made
        self.agent._llm.quick.invoke.assert_not_called()
        self.agent._llm.deep.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_debate_result_remains_none(self):
        signal = _make_signal(passed_confidence_gate=True, confidence_score=0.85)
        result = await self.agent._debate_signal(signal)
        assert result.debate_result is None


# ---------------------------------------------------------------------------
# TestDebateSignalBelowThreshold
# ---------------------------------------------------------------------------


class TestDebateSignalBelowThreshold:
    def setup_method(self) -> None:
        self.agent = _make_agent(
            _make_settings(signal_debate_rounds=1, signal_debate_threshold=0.70)
        )

    @pytest.mark.asyncio
    async def test_low_confidence_skips_debate(self):
        # confidence_score=0.60 < threshold=0.70
        signal = _make_signal(passed_confidence_gate=True, confidence_score=0.60)
        result = await self.agent._debate_signal(signal)
        assert result is signal
        self.agent._llm.quick.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_confidence_score_skips_debate(self):
        signal = _make_signal(passed_confidence_gate=True, confidence_score=None)
        result = await self.agent._debate_signal(signal)
        assert result is signal
        self.agent._llm.quick.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# TestDebateSignalVerdicts
# ---------------------------------------------------------------------------


def _stub_quick_llm(content: str) -> AsyncMock:
    from news_trade.services.llm_client import LLMResponse

    mock = AsyncMock()
    mock.invoke = AsyncMock(
        return_value=LLMResponse(
            content=content,
            model_id="claude-haiku-4-5-20251001",
            provider="anthropic",
        )
    )
    mock.model_id = "claude-haiku-4-5-20251001"
    mock.provider = "anthropic"
    return mock


def _stub_deep_llm(verdict: DebateVerdict, delta: float = 0.0) -> AsyncMock:
    from news_trade.services.llm_client import LLMResponse

    payload = json.dumps(
        {"verdict": verdict.value, "confidence_delta": delta, "reasoning": "test"}
    )
    mock = AsyncMock()
    mock.invoke = AsyncMock(
        return_value=LLMResponse(
            content=payload,
            model_id="claude-sonnet-4-6",
            provider="anthropic",
        )
    )
    mock.model_id = "claude-sonnet-4-6"
    mock.provider = "anthropic"
    return mock


def _make_agent_with_llm(
    verdict: DebateVerdict,
    delta: float = 0.0,
    rounds: int = 1,
) -> SignalGeneratorAgent:
    settings = _make_settings(signal_debate_rounds=rounds, signal_debate_threshold=0.70)
    event_bus = MagicMock()
    quick = _stub_quick_llm("A convincing argument.")
    deep = _stub_deep_llm(verdict, delta)
    llm_factory = MagicMock()
    llm_factory.quick = quick
    llm_factory.deep = deep
    return SignalGeneratorAgent(settings=settings, event_bus=event_bus, llm=llm_factory)


class TestDebateSignalVerdicts:
    @pytest.mark.asyncio
    async def test_confirm_adds_debate_result(self):
        agent = _make_agent_with_llm(DebateVerdict.CONFIRM, delta=0.05)
        signal = _make_signal(
            passed_confidence_gate=True, confidence_score=0.80, suggested_qty=20
        )
        result = await agent._debate_signal(signal)
        assert result.debate_result is not None
        assert result.debate_result.verdict == DebateVerdict.CONFIRM
        assert result.confidence_score == pytest.approx(0.85)
        assert result.passed_confidence_gate is True
        assert result.suggested_qty == 20  # unchanged

    @pytest.mark.asyncio
    async def test_reduce_halves_position_size(self):
        agent = _make_agent_with_llm(DebateVerdict.REDUCE, delta=-0.05)
        signal = _make_signal(
            passed_confidence_gate=True, confidence_score=0.80, suggested_qty=20
        )
        result = await agent._debate_signal(signal)
        assert result.debate_result is not None
        assert result.debate_result.verdict == DebateVerdict.REDUCE
        assert result.suggested_qty == 10  # halved
        assert result.passed_confidence_gate is True  # gate not flipped

    @pytest.mark.asyncio
    async def test_reduce_minimum_qty_is_one(self):
        agent = _make_agent_with_llm(DebateVerdict.REDUCE)
        signal = _make_signal(
            passed_confidence_gate=True, confidence_score=0.80, suggested_qty=1
        )
        result = await agent._debate_signal(signal)
        assert result.suggested_qty == 1  # max(1, 1//2) = max(1,0) = 1

    @pytest.mark.asyncio
    async def test_reject_flips_gate_and_sets_reason(self):
        agent = _make_agent_with_llm(DebateVerdict.REJECT, delta=-0.15)
        signal = _make_signal(
            passed_confidence_gate=True, confidence_score=0.80, suggested_qty=20
        )
        result = await agent._debate_signal(signal)
        assert result.passed_confidence_gate is False
        assert result.rejection_reason == "Debate: bear thesis dominated"
        assert result.debate_result is not None
        assert result.debate_result.verdict == DebateVerdict.REJECT

    @pytest.mark.asyncio
    async def test_debate_rounds_recorded(self):
        agent = _make_agent_with_llm(DebateVerdict.CONFIRM, rounds=2)
        signal = _make_signal(
            passed_confidence_gate=True, confidence_score=0.80, suggested_qty=10
        )
        result = await agent._debate_signal(signal)
        assert result.debate_result is not None
        assert len(result.debate_result.rounds) == 2
        assert result.debate_result.rounds[0].round_number == 0
        assert result.debate_result.rounds[1].round_number == 1
