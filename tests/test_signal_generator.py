"""Unit tests for SignalGeneratorAgent."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from news_trade.agents.signal_generator import (
    SignalGeneratorAgent,
    _aggregate_ticker_group,
    _build_thesis_bull_prompt,
    _decay_weight,
    _fetch_company_snapshot,
    _parse_calendar_fields,
    _ThesisVerdictSchema,
)
from news_trade.config import Settings
from news_trade.models.events import EventType, NewsEvent
from news_trade.models.market import MarketSnapshot
from news_trade.models.outcomes import HistoricalOutcomes
from news_trade.models.positions import OpenStage1Position, Stage1Status
from news_trade.models.sentiment import SentimentLabel, SentimentResult
from news_trade.models.signals import (
    DebateVerdict,
    SignalDirection,
    TradeSignal,
)
from news_trade.models.surprise import EstimatesData
from news_trade.services.confidence_scorer import ConfidenceScorer

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
    scorer = ConfidenceScorer(settings=s)
    stage1_repo = MagicMock()
    return SignalGeneratorAgent(
        settings=s,
        event_bus=event_bus,
        llm=llm_factory,
        scorer=scorer,
        stage1_repo=stage1_repo,
    )


# ---------------------------------------------------------------------------
# TestBuildSignal
# ---------------------------------------------------------------------------


class TestBuildSignal:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    async def test_bullish_sentiment_produces_long(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.BULLISH, score=0.85, confidence=0.90
        )
        market = _make_market()
        signal = await self.agent._build_signal(sentiment, market, {}, {})
        assert signal is not None
        assert signal.direction == SignalDirection.LONG
        assert signal.ticker == "AAPL"

    async def test_very_bullish_produces_long(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.VERY_BULLISH, score=0.95, confidence=0.95
        )
        signal = await self.agent._build_signal(sentiment, _make_market(), {}, {})
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    async def test_bearish_sentiment_produces_short(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.BEARISH, score=-0.80, confidence=0.85
        )
        signal = await self.agent._build_signal(sentiment, _make_market(), {}, {})
        assert signal is not None
        assert signal.direction == SignalDirection.SHORT

    async def test_very_bearish_produces_short(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.VERY_BEARISH,
            score=-0.90,
            confidence=0.90,
        )
        signal = await self.agent._build_signal(sentiment, _make_market(), {}, {})
        assert signal is not None
        assert signal.direction == SignalDirection.SHORT

    async def test_neutral_returns_none(self):
        sentiment = _make_sentiment(
            label=SentimentLabel.NEUTRAL, score=0.0, confidence=0.50
        )
        signal = await self.agent._build_signal(sentiment, _make_market(), {}, {})
        assert signal is None

    async def test_below_conviction_threshold_returns_none(self):
        # abs(score) * confidence = 0.3 * 0.5 = 0.15 < 0.60
        sentiment = _make_sentiment(
            label=SentimentLabel.BULLISH, score=0.30, confidence=0.50
        )
        signal = await self.agent._build_signal(sentiment, _make_market(), {}, {})
        assert signal is None

    async def test_signal_fields_populated(self):
        sentiment = _make_sentiment()
        market = _make_market(latest_close=150.0, volatility_20d=0.20)
        signal = await self.agent._build_signal(sentiment, market, {}, {})
        assert signal is not None
        assert signal.event_id == sentiment.event_id
        assert signal.rationale == sentiment.reasoning
        assert signal.model_id == "claude-haiku-4-5-20251001"
        assert signal.provider == "anthropic"
        assert signal.stop_loss is not None

    async def test_stop_loss_long_below_entry(self):
        market = _make_market(latest_close=100.0, volatility_20d=0.10)
        sentiment = _make_sentiment(
            label=SentimentLabel.BULLISH, score=0.9, confidence=0.9
        )
        signal = await self.agent._build_signal(sentiment, market, {}, {})
        assert signal is not None
        assert signal.stop_loss < 100.0  # long stop below entry

    async def test_stop_loss_short_above_entry(self):
        market = _make_market(latest_close=100.0, volatility_20d=0.10)
        sentiment = _make_sentiment(
            label=SentimentLabel.BEARISH, score=-0.9, confidence=0.9
        )
        signal = await self.agent._build_signal(sentiment, market, {}, {})
        assert signal is not None
        assert signal.stop_loss > 100.0  # short stop above entry

    async def test_position_size_positive(self):
        market = _make_market(volatility_20d=0.25)
        sentiment = _make_sentiment()
        signal = await self.agent._build_signal(sentiment, market, {}, {})
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
    scorer = ConfidenceScorer(settings=settings)
    stage1_repo = MagicMock()
    return SignalGeneratorAgent(
        settings=settings,
        event_bus=event_bus,
        llm=llm_factory,
        scorer=scorer,
        stage1_repo=stage1_repo,
    )


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


# ---------------------------------------------------------------------------
# Helpers shared by EARN_* tests
# ---------------------------------------------------------------------------


def _make_earn_event(
    ticker: str = "AAPL",
    event_type: EventType = EventType.EARN_PRE,
    report_date: date | None = None,
    fiscal_quarter: str = "Q2 2026",
) -> NewsEvent:
    rd = report_date or (date.today() + timedelta(days=4))
    return NewsEvent(
        event_id="earn-evt-1",
        headline=f"{ticker} scheduled to report {fiscal_quarter} on {rd} (pre_market)",
        summary="eps_estimate=2.50 days_until_report=4",
        source="earnings_calendar",
        tickers=[ticker],
        event_type=event_type,
        published_at=NOW,
    )


def _make_estimates(
    ticker: str = "AAPL",
    report_date: date | None = None,
    fiscal_period: str = "Q2 2026",
) -> EstimatesData:
    rd = report_date or (date.today() + timedelta(days=4))
    return EstimatesData(
        ticker=ticker,
        fiscal_period=fiscal_period,
        report_date=rd,
        eps_estimate=2.50,
        eps_low=2.20,
        eps_high=2.80,
        revenue_estimate=90_000_000.0,
        revenue_low=88_000_000.0,
        revenue_high=92_000_000.0,
        num_analysts=10,
    )


def _make_open_pos(
    ticker: str = "AAPL",
    direction: str = "long",
    size_pct: float = 0.33,
) -> OpenStage1Position:
    return OpenStage1Position(
        id=str(uuid.uuid4()),
        ticker=ticker,
        direction=direction,
        size_pct=size_pct,
        entry_price=200.0,
        opened_at=datetime.utcnow(),
        expected_report_date=date.today() + timedelta(days=4),
        fiscal_quarter="Q2 2026",
        historical_beat_rate=0.72,
    )


def _make_earn_agent(
    beat_rate: float | None = 0.72, source: str = "observed"
) -> SignalGeneratorAgent:
    """Agent with a mock Stage1Repository returning the given beat_rate."""
    s = _make_settings()
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
    scorer = ConfidenceScorer(settings=s)
    stage1_repo = MagicMock()
    outcomes = HistoricalOutcomes(
        source=source,
        beat_rate=beat_rate if source == "observed" else None,
        sample_size=5 if source == "observed" else 1,
    )
    stage1_repo.load_historical_outcomes.return_value = outcomes
    stage1_repo.load_open.return_value = None
    return SignalGeneratorAgent(
        settings=s,
        event_bus=event_bus,
        llm=llm_factory,
        scorer=scorer,
        stage1_repo=stage1_repo,
    )


# ---------------------------------------------------------------------------
# TestHandleEarnPre
# ---------------------------------------------------------------------------


class TestHandleEarnPre:
    def setup_method(self) -> None:
        self.agent = _make_earn_agent(beat_rate=0.72)
        # Mock the thesis debate so tests don't make real LLM calls.
        self.agent._run_thesis_debate = AsyncMock(
            return_value=_ThesisVerdictSchema(
                direction="LONG", conviction=0.80, reasoning="bull"
            )
        )
        self.sentiment = _make_sentiment(
            event_id="earn-evt-1",
            label=SentimentLabel.BULLISH,
            score=0.80,
            confidence=0.90,
        )
        self.market = _make_market(latest_close=200.0, volatility_20d=0.20)
        self.event = _make_earn_event(event_type=EventType.EARN_PRE)
        self.estimates = {"AAPL": _make_estimates()}

    async def test_earn_pre_produces_long_for_high_beat_rate(self):
        signal = await self.agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            self.estimates,
            group=[self.sentiment],
        )
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    async def test_earn_pre_sets_stage1_id(self):
        signal = await self.agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            self.estimates,
            group=[self.sentiment],
        )
        assert signal is not None
        assert signal.stage1_id is not None

    async def test_earn_pre_has_no_horizon_days(self):
        signal = await self.agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            self.estimates,
            group=[self.sentiment],
        )
        assert signal is not None
        assert signal.horizon_days is None

    async def test_earn_pre_persists_position(self):
        await self.agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            self.estimates,
            group=[self.sentiment],
        )
        self.agent._stage1_repo.persist.assert_called_once()

    async def test_earn_pre_stop_loss_below_entry_for_long(self):
        signal = await self.agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            self.estimates,
            group=[self.sentiment],
        )
        assert signal is not None
        # 4% stop: 200 * (1 - 0.04) = 192
        assert signal.stop_loss is not None
        assert signal.stop_loss < self.market.latest_close

    # Three-tier beat-rate fallback tests — assert debate called with correct beat_rate
    async def test_earn_pre_observed_beat_rate_passed_to_debate(self):
        """Observed beat_rate=0.72 is passed as context to _run_thesis_debate."""
        await self.agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            self.estimates,
            group=[self.sentiment],
        )
        call_kwargs = self.agent._run_thesis_debate.call_args.kwargs
        assert call_kwargs["beat_rate"] == pytest.approx(0.72)
        assert call_kwargs["beat_rate_source"] == "observed"

    async def test_earn_pre_uses_default_beat_rate_when_fmp_source(self):
        """With source='fmp' and no FMP estimate, default beat_rate is passed."""
        agent = _make_earn_agent(beat_rate=0.65, source="fmp")
        agent._run_thesis_debate = AsyncMock(
            return_value=_ThesisVerdictSchema(
                direction="LONG", conviction=0.80, reasoning="bull"
            )
        )
        await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            self.estimates,
            group=[self.sentiment],
        )
        call_kwargs = agent._run_thesis_debate.call_args.kwargs
        # source='fmp' and outcomes.beat_rate=None → falls through to
        # estimates[ticker].historical_beat_rate (None) → default
        assert call_kwargs["beat_rate_source"] == "default"

    # Three-tier fallback tests
    async def test_fmp_estimates_beat_rate_used_over_default(self):
        """When source='fmp' and estimates carry historical_beat_rate, use it."""
        agent = _make_earn_agent(beat_rate=None, source="fmp")
        agent._run_thesis_debate = AsyncMock(
            return_value=_ThesisVerdictSchema(
                direction="LONG", conviction=0.80, reasoning="bull"
            )
        )
        agent.settings = agent.settings.model_copy(
            update={"earn_default_beat_rate": 0.57}
        )
        estimates_with_rate = {
            "AAPL": _make_estimates().model_copy(update={"historical_beat_rate": 0.75})
        }
        await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            estimates_with_rate,
            group=[self.sentiment],
        )
        call_kwargs = agent._run_thesis_debate.call_args.kwargs
        assert call_kwargs["beat_rate"] == pytest.approx(0.75)
        assert call_kwargs["beat_rate_source"] == "fmp"

    async def test_default_beat_rate_used_when_no_fmp_estimates(self):
        """With source='fmp' and no estimates dict entry, fall back to default."""
        agent = _make_earn_agent(beat_rate=None, source="fmp")
        agent._run_thesis_debate = AsyncMock(
            return_value=_ThesisVerdictSchema(
                direction="LONG", conviction=0.80, reasoning="bull"
            )
        )
        agent.settings = agent.settings.model_copy(
            update={"earn_default_beat_rate": 0.65}
        )
        await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            {},  # empty estimates
            group=[self.sentiment],
        )
        call_kwargs = agent._run_thesis_debate.call_args.kwargs
        assert call_kwargs["beat_rate"] == pytest.approx(0.65)
        assert call_kwargs["beat_rate_source"] == "default"

    async def test_default_beat_rate_used_when_estimates_historical_beat_rate_is_none(
        self,
    ):
        """With source='fmp' and historical_beat_rate=None, fall back to default."""
        agent = _make_earn_agent(beat_rate=None, source="fmp")
        agent._run_thesis_debate = AsyncMock(
            return_value=_ThesisVerdictSchema(
                direction="LONG", conviction=0.80, reasoning="bull"
            )
        )
        agent.settings = agent.settings.model_copy(
            update={"earn_default_beat_rate": 0.57}
        )
        estimates_no_rate = {
            "AAPL": _make_estimates()  # historical_beat_rate defaults to None
        }
        await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            estimates_no_rate,
            group=[self.sentiment],
        )
        call_kwargs = agent._run_thesis_debate.call_args.kwargs
        assert call_kwargs["beat_rate"] == pytest.approx(0.57)
        assert call_kwargs["beat_rate_source"] == "default"

    async def test_observed_beat_rate_overrides_fmp_estimates(self):
        """Observed beat rate wins even when estimates carry historical_beat_rate."""
        agent = _make_earn_agent(beat_rate=0.57, source="observed")
        agent._run_thesis_debate = AsyncMock(
            return_value=_ThesisVerdictSchema(
                direction="SHORT", conviction=0.80, reasoning="bear"
            )
        )
        estimates_with_rate = {
            "AAPL": _make_estimates().model_copy(update={"historical_beat_rate": 0.75})
        }
        await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-evt-1": self.event},
            estimates_with_rate,
            group=[self.sentiment],
        )
        call_kwargs = agent._run_thesis_debate.call_args.kwargs
        assert call_kwargs["beat_rate"] == pytest.approx(0.57)
        assert call_kwargs["beat_rate_source"] == "observed"


# ---------------------------------------------------------------------------
# TestHandleEarnPost
# ---------------------------------------------------------------------------


class TestHandleEarnPost:
    def setup_method(self) -> None:
        self.market = _make_market(latest_close=210.0, volatility_20d=0.20)
        self.sentiment = _make_sentiment(
            event_id="earn-beat-1",
            label=SentimentLabel.VERY_BULLISH,
            score=0.90,
            confidence=0.92,
        )

    def _beat_agent(
        self, open_pos: OpenStage1Position | None = None
    ) -> SignalGeneratorAgent:
        s = _make_settings()
        event_bus = MagicMock()
        llm_quick = MagicMock()
        llm_quick.model_id = "claude-haiku-4-5-20251001"
        llm_quick.provider = "anthropic"
        llm_factory = MagicMock()
        llm_factory.quick = llm_quick
        scorer = ConfidenceScorer(settings=s)
        stage1_repo = MagicMock()
        stage1_repo.load_open.return_value = open_pos
        return SignalGeneratorAgent(
            settings=s,
            event_bus=event_bus,
            llm=llm_factory,
            scorer=scorer,
            stage1_repo=stage1_repo,
        )

    async def test_earn_beat_with_agreeing_stage1_long(self):
        pos = _make_open_pos(direction="long")
        agent = self._beat_agent(open_pos=pos)
        event = NewsEvent(
            event_id="earn-beat-1",
            headline="AAPL beats Q2",
            summary="",
            source="benzinga",
            tickers=["AAPL"],
            event_type=EventType.EARN_BEAT,
            published_at=NOW,
        )
        signal = await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-beat-1": event},
            {},
        )
        assert signal is not None
        assert signal.direction == SignalDirection.LONG
        assert signal.stage1_id == pos.id
        agent._stage1_repo.update_status.assert_called_once_with(
            pos.id, Stage1Status.CONFIRMED
        )

    async def test_earn_beat_reverses_stage1_short(self):
        pos = _make_open_pos(direction="short")
        agent = self._beat_agent(open_pos=pos)
        event = NewsEvent(
            event_id="earn-beat-1",
            headline="AAPL beats Q2",
            summary="",
            source="benzinga",
            tickers=["AAPL"],
            event_type=EventType.EARN_BEAT,
            published_at=NOW,
        )
        signal = await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-beat-1": event},
            {},
        )
        assert signal is not None
        assert signal.direction == SignalDirection.LONG
        agent._stage1_repo.update_status.assert_called_once_with(
            pos.id, Stage1Status.REVERSED
        )

    async def test_earn_miss_produces_short(self):
        agent = self._beat_agent(open_pos=None)
        sentiment = _make_sentiment(
            event_id="earn-miss-1",
            label=SentimentLabel.VERY_BEARISH,
            score=-0.88,
            confidence=0.91,
        )
        event = NewsEvent(
            event_id="earn-miss-1",
            headline="AAPL misses Q2",
            summary="",
            source="benzinga",
            tickers=["AAPL"],
            event_type=EventType.EARN_MISS,
            published_at=NOW,
        )
        signal = await agent._build_signal(
            sentiment,
            self.market,
            {"earn-miss-1": event},
            {},
        )
        assert signal is not None
        assert signal.direction == SignalDirection.SHORT

    async def test_earn_beat_no_stage1_fresh_pead(self):
        agent = self._beat_agent(open_pos=None)
        event = NewsEvent(
            event_id="earn-beat-1",
            headline="AAPL beats Q2",
            summary="",
            source="benzinga",
            tickers=["AAPL"],
            event_type=EventType.EARN_BEAT,
            published_at=NOW,
        )
        signal = await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-beat-1": event},
            {},
        )
        assert signal is not None
        assert signal.direction == SignalDirection.LONG
        assert signal.stage1_id is None  # no existing position
        agent._stage1_repo.update_status.assert_not_called()

    async def test_earn_beat_signal_has_horizon_days(self):
        agent = self._beat_agent(open_pos=None)
        event = NewsEvent(
            event_id="earn-beat-1",
            headline="AAPL beats Q2",
            summary="",
            source="benzinga",
            tickers=["AAPL"],
            event_type=EventType.EARN_BEAT,
            published_at=NOW,
        )
        signal = await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-beat-1": event},
            {},
        )
        assert signal is not None
        assert signal.horizon_days == agent.settings.pead_horizon_days

    async def test_earn_miss_signal_has_horizon_days(self):
        agent = self._beat_agent(open_pos=None)
        sentiment = _make_sentiment(
            event_id="earn-miss-1",
            label=SentimentLabel.VERY_BEARISH,
            score=-0.9,
            confidence=0.85,
        )
        event = NewsEvent(
            event_id="earn-miss-1",
            headline="AAPL misses Q2",
            summary="",
            source="benzinga",
            tickers=["AAPL"],
            event_type=EventType.EARN_MISS,
            published_at=NOW,
        )
        signal = await agent._build_signal(
            sentiment,
            self.market,
            {"earn-miss-1": event},
            {},
        )
        assert signal is not None
        assert signal.horizon_days == agent.settings.pead_horizon_days


# ---------------------------------------------------------------------------
# TestHandleEarnMixed
# ---------------------------------------------------------------------------


class TestHandleEarnMixed:
    def setup_method(self) -> None:
        self.market = _make_market()
        self.sentiment = _make_sentiment(
            event_id="earn-mixed-1",
            label=SentimentLabel.NEUTRAL,
            score=0.0,
            confidence=0.50,
        )
        self.event = NewsEvent(
            event_id="earn-mixed-1",
            headline="AAPL mixed Q2",
            summary="",
            source="benzinga",
            tickers=["AAPL"],
            event_type=EventType.EARN_MIXED,
            published_at=NOW,
        )

    def _mixed_agent(self, open_pos: OpenStage1Position | None) -> SignalGeneratorAgent:
        s = _make_settings()
        event_bus = MagicMock()
        llm_quick = MagicMock()
        llm_quick.model_id = "claude-haiku-4-5-20251001"
        llm_quick.provider = "anthropic"
        llm_factory = MagicMock()
        llm_factory.quick = llm_quick
        scorer = ConfidenceScorer(settings=s)
        stage1_repo = MagicMock()
        stage1_repo.load_open.return_value = open_pos
        return SignalGeneratorAgent(
            settings=s,
            event_bus=event_bus,
            llm=llm_factory,
            scorer=scorer,
            stage1_repo=stage1_repo,
        )

    async def test_earn_mixed_with_open_pos_emits_close(self):
        pos = _make_open_pos()
        agent = self._mixed_agent(open_pos=pos)
        signal = await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-mixed-1": self.event},
            {},
        )
        assert signal is not None
        assert signal.direction == SignalDirection.CLOSE
        assert signal.passed_confidence_gate is True
        assert signal.stage1_id == pos.id
        agent._stage1_repo.update_status.assert_called_once_with(
            pos.id, Stage1Status.EXITED
        )

    async def test_earn_mixed_no_stage1_returns_none(self):
        agent = self._mixed_agent(open_pos=None)
        signal = await agent._build_signal(
            self.sentiment,
            self.market,
            {"earn-mixed-1": self.event},
            {},
        )
        assert signal is None
        agent._stage1_repo.update_status.assert_not_called()


# ---------------------------------------------------------------------------
# TestParseCalendarFields
# ---------------------------------------------------------------------------


class TestParseCalendarFields:
    def test_uses_estimates_when_available(self):
        rd = date(2026, 4, 30)
        est = _make_estimates(report_date=rd, fiscal_period="Q2 2026")
        result_date, result_qtr = _parse_calendar_fields("AAPL", None, {"AAPL": est})
        assert result_date == rd
        assert result_qtr == "Q2 2026"

    def test_parses_headline_when_no_estimates(self):
        rd = date(2026, 4, 30)
        event = NewsEvent(
            event_id="e1",
            headline=f"AAPL scheduled to report Q2 2026 on {rd} (pre_market)",
            summary="",
            source="earnings_calendar",
            tickers=["AAPL"],
            event_type=EventType.EARN_PRE,
            published_at=NOW,
        )
        result_date, result_qtr = _parse_calendar_fields("AAPL", event, {})
        assert result_date == rd
        assert result_qtr == "Q2 2026"

    def test_fallback_when_no_estimates_no_event(self):
        result_date, result_qtr = _parse_calendar_fields("AAPL", None, {})
        assert result_date == date.today() + timedelta(days=3)
        assert result_qtr == "unknown"


# ---------------------------------------------------------------------------
# Helpers shared by decay / aggregation tests
# ---------------------------------------------------------------------------


def _make_lookup(pairs: dict[str, datetime]) -> dict[str, MagicMock]:
    """Build a mock event_lookup mapping event_id → object with .published_at."""
    return {eid: MagicMock(published_at=pub) for eid, pub in pairs.items()}


# ---------------------------------------------------------------------------
# TestDecayWeight
# ---------------------------------------------------------------------------


class TestDecayWeight:
    def test_age_zero_returns_one(self):
        now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        assert _decay_weight(now, now, halflife_hours=72) == pytest.approx(1.0)

    def test_halflife_gives_half(self):
        now = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
        pub = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)  # exactly 72 h ago
        assert _decay_weight(pub, now, halflife_hours=72) == pytest.approx(
            0.5, rel=1e-4
        )

    def test_naive_datetime_treated_as_utc(self):
        now = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
        pub = datetime(2026, 3, 3, 12, 0)  # naive — 24 h ago
        result = _decay_weight(pub, now, halflife_hours=72)
        assert 0.7 < result < 0.85  # expected ~0.794

    def test_none_returns_one(self):
        now = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
        assert _decay_weight(None, now, halflife_hours=72) == pytest.approx(1.0)

    def test_future_timestamp_clamps_to_one(self):
        now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        pub = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)  # clock-skewed future
        assert _decay_weight(pub, now, halflife_hours=72) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# TestAggregateTickerGroup
# ---------------------------------------------------------------------------


class TestAggregateTickerGroup:
    NOW = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
    HL = 72.0

    def test_single_bullish_returns_result(self):
        sr = _make_sentiment(
            event_id="e1", label=SentimentLabel.BULLISH, score=0.8, confidence=0.9
        )
        lookup = _make_lookup({"e1": self.NOW - timedelta(hours=10)})
        result = _aggregate_ticker_group("AAPL", [sr], lookup, self.HL, self.NOW)
        assert result is not None
        assert result.ticker == "AAPL"
        assert result.event_id == "e1"
        assert result.label in (SentimentLabel.BULLISH, SentimentLabel.VERY_BULLISH)

    def test_same_direction_representative_is_freshest(self):
        srs = [
            _make_sentiment(
                event_id="old", label=SentimentLabel.BULLISH, score=0.7, confidence=0.8
            ),
            _make_sentiment(
                event_id="new",
                label=SentimentLabel.VERY_BULLISH,
                score=0.9,
                confidence=0.9,
            ),
        ]
        lookup = _make_lookup(
            {
                "old": self.NOW - timedelta(hours=120),
                "new": self.NOW - timedelta(hours=2),
            }
        )
        result = _aggregate_ticker_group("AAPL", srs, lookup, self.HL, self.NOW)
        assert result is not None
        assert result.event_id == "new"  # highest weight = freshest

    def test_equal_weight_conflict_returns_none(self):
        srs = [
            _make_sentiment(
                event_id="bull", label=SentimentLabel.BULLISH, score=0.8, confidence=0.9
            ),
            _make_sentiment(
                event_id="bear",
                label=SentimentLabel.BEARISH,
                score=-0.8,
                confidence=0.9,
            ),
        ]
        pub = self.NOW - timedelta(hours=5)
        lookup = _make_lookup({"bull": pub, "bear": pub})
        result = _aggregate_ticker_group("AAPL", srs, lookup, self.HL, self.NOW)
        assert result is None

    def test_small_minority_does_not_trigger_mixed(self):
        # 3 strong bullish + 1 very-low-confidence bearish → minority < 25% → passes
        srs = [
            _make_sentiment(
                event_id="b1",
                label=SentimentLabel.VERY_BULLISH,
                score=0.9,
                confidence=0.9,
            ),
            _make_sentiment(
                event_id="b2",
                label=SentimentLabel.VERY_BULLISH,
                score=0.85,
                confidence=0.9,
            ),
            _make_sentiment(
                event_id="b3", label=SentimentLabel.BULLISH, score=0.8, confidence=0.9
            ),
            _make_sentiment(
                event_id="br", label=SentimentLabel.BEARISH, score=-0.5, confidence=0.15
            ),
        ]
        lookup = _make_lookup(
            {k: self.NOW - timedelta(hours=2) for k in ["b1", "b2", "b3", "br"]}
        )
        result = _aggregate_ticker_group("AAPL", srs, lookup, self.HL, self.NOW)
        assert result is not None
        assert result.label in (SentimentLabel.BULLISH, SentimentLabel.VERY_BULLISH)

    def test_all_neutral_returns_none(self):
        sr = _make_sentiment(
            event_id="e1", label=SentimentLabel.NEUTRAL, score=0.0, confidence=0.5
        )
        lookup = _make_lookup({"e1": self.NOW - timedelta(hours=5)})
        result = _aggregate_ticker_group("AAPL", [sr], lookup, self.HL, self.NOW)
        assert result is None

    def test_older_article_loses_to_fresh_opposing(self):
        # 200 h old BULLISH (decay ≈ 0.15) vs 1 h fresh BEARISH (decay ≈ 0.99)
        # bearish weight dominates → BEARISH, fresh article is representative
        srs = [
            _make_sentiment(
                event_id="old",
                label=SentimentLabel.VERY_BULLISH,
                score=0.9,
                confidence=0.9,
            ),
            _make_sentiment(
                event_id="fresh",
                label=SentimentLabel.BEARISH,
                score=-0.8,
                confidence=0.85,
            ),
        ]
        lookup = _make_lookup(
            {
                "old": self.NOW - timedelta(hours=200),
                "fresh": self.NOW - timedelta(hours=1),
            }
        )
        result = _aggregate_ticker_group("AAPL", srs, lookup, self.HL, self.NOW)
        assert result is not None
        assert result.label in (SentimentLabel.BEARISH, SentimentLabel.VERY_BEARISH)
        assert result.event_id == "fresh"

    def test_fresh_event_wins_over_stale_as_representative(self):
        srs = [
            _make_sentiment(
                event_id="stale-news",
                label=SentimentLabel.BULLISH,
                score=0.7,
                confidence=0.8,
            ),
            _make_sentiment(
                event_id="earn-pre",
                label=SentimentLabel.BULLISH,
                score=0.8,
                confidence=0.85,
            ),
        ]
        lookup = _make_lookup(
            {
                "stale-news": self.NOW - timedelta(hours=150),
                "earn-pre": self.NOW - timedelta(hours=2),
            }
        )
        result = _aggregate_ticker_group("AAPL", srs, lookup, self.HL, self.NOW)
        assert result is not None
        assert result.event_id == "earn-pre"

    def test_missing_event_in_lookup_uses_no_decay(self):
        # event_id not in lookup → published_at=None → decay=1.0 (no discount)
        sr = _make_sentiment(
            event_id="unknown", label=SentimentLabel.BULLISH, score=0.8, confidence=0.9
        )
        result = _aggregate_ticker_group("AAPL", [sr], {}, self.HL, self.NOW)
        assert result is not None
        assert result.event_id == "unknown"


# ---------------------------------------------------------------------------
# TestRunAggregation  (integration — exercises run())
# ---------------------------------------------------------------------------


class TestRunAggregation:
    def setup_method(self) -> None:
        self.agent = _make_agent(
            _make_settings(
                article_decay_halflife_hours=72.0, min_signal_conviction=0.60
            )
        )
        self.agent._stage1_repo.load_open.return_value = None

    async def test_five_same_ticker_produces_one_signal(self):
        now = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
        events = [
            NewsEvent(
                event_id=f"e{i}",
                headline="AAPL earnings",
                summary="",
                source="rss",
                tickers=["AAPL"],
                published_at=now - timedelta(hours=i * 10),
            )
            for i in range(5)
        ]
        srs = [
            _make_sentiment(
                event_id=f"e{i}",
                label=SentimentLabel.BULLISH,
                score=0.8,
                confidence=0.9,
            )
            for i in range(5)
        ]
        result = await self.agent.run(
            {
                "sentiment_results": srs,
                "market_context": {"AAPL": _make_market()},
                "news_events": events,
            }
        )
        assert len(result["trade_signals"]) == 1
        assert result["trade_signals"][0].ticker == "AAPL"

    async def test_conflicting_same_ticker_produces_no_signal(self):
        now = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
        pub = now - timedelta(hours=5)
        events = [
            NewsEvent(
                event_id="bull",
                headline="AAPL up",
                summary="",
                source="rss",
                tickers=["AAPL"],
                published_at=pub,
            ),
            NewsEvent(
                event_id="bear",
                headline="AAPL down",
                summary="",
                source="rss",
                tickers=["AAPL"],
                published_at=pub,
            ),
        ]
        srs = [
            _make_sentiment(
                event_id="bull", label=SentimentLabel.BULLISH, score=0.8, confidence=0.9
            ),
            _make_sentiment(
                event_id="bear",
                label=SentimentLabel.BEARISH,
                score=-0.8,
                confidence=0.9,
            ),
        ]
        result = await self.agent.run(
            {
                "sentiment_results": srs,
                "market_context": {"AAPL": _make_market()},
                "news_events": events,
            }
        )
        assert result["trade_signals"] == []

    async def test_two_tickers_each_produce_one_signal(self):
        now = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
        pub = now - timedelta(hours=1)
        events = [
            NewsEvent(
                event_id="a",
                headline="AAPL",
                summary="",
                source="rss",
                tickers=["AAPL"],
                published_at=pub,
            ),
            NewsEvent(
                event_id="m",
                headline="MSFT",
                summary="",
                source="rss",
                tickers=["MSFT"],
                published_at=pub,
            ),
        ]
        srs = [
            _make_sentiment(
                event_id="a",
                ticker="AAPL",
                label=SentimentLabel.BULLISH,
                score=0.85,
                confidence=0.9,
            ),
            _make_sentiment(
                event_id="m",
                ticker="MSFT",
                label=SentimentLabel.BULLISH,
                score=0.85,
                confidence=0.9,
            ),
        ]
        result = await self.agent.run(
            {
                "sentiment_results": srs,
                "market_context": {
                    "AAPL": _make_market(ticker="AAPL"),
                    "MSFT": _make_market(ticker="MSFT"),
                },
                "news_events": events,
            }
        )
        tickers = {s.ticker for s in result["trade_signals"]}
        assert tickers == {"AAPL", "MSFT"}


# ---------------------------------------------------------------------------
# TestRunThesisDebate
# ---------------------------------------------------------------------------


def _make_thesis_verdict(
    direction: str = "LONG", conviction: float = 0.80
) -> _ThesisVerdictSchema:
    return _ThesisVerdictSchema(
        direction=direction, conviction=conviction, reasoning="test reasoning"
    )


class TestRunThesisDebate:
    def setup_method(self) -> None:
        self.agent = _make_earn_agent(beat_rate=0.72)
        self.sentiment = _make_sentiment(
            event_id="earn-evt-1",
            label=SentimentLabel.BULLISH,
            score=0.80,
            confidence=0.90,
        )
        self.market = _make_market(latest_close=200.0, volatility_20d=0.20)
        self.event = _make_earn_event(event_type=EventType.EARN_PRE)
        self.estimates = {"AAPL": _make_estimates()}
        self.event_lookup = {"earn-evt-1": self.event}

    def _setup_debate(self, verdict: _ThesisVerdictSchema) -> None:
        self.agent._run_thesis_debate = AsyncMock(return_value=verdict)

    async def test_neutral_verdict_returns_none(self):
        self._setup_debate(_make_thesis_verdict(direction="NEUTRAL"))
        result = await self.agent._build_signal(
            self.sentiment,
            self.market,
            self.event_lookup,
            self.estimates,
            group=[self.sentiment],
        )
        assert result is None

    async def test_long_verdict_opens_stage1(self):
        self._setup_debate(_make_thesis_verdict(direction="LONG", conviction=0.80))
        result = await self.agent._build_signal(
            self.sentiment,
            self.market,
            self.event_lookup,
            self.estimates,
            group=[self.sentiment],
        )
        assert result is not None
        assert result.stage1_id is not None
        self.agent._stage1_repo.persist.assert_called_once()

    async def test_long_verdict_reaffirms_existing_long(self):
        existing = _make_open_pos(direction="long")
        self.agent._stage1_repo.load_open.return_value = existing
        # new_news_present=True: event_id does NOT start with "ticker_earn_pre_"
        self._setup_debate(_make_thesis_verdict(direction="LONG", conviction=0.80))
        result = await self.agent._build_signal(
            self.sentiment,
            self.market,
            self.event_lookup,
            self.estimates,
            group=[self.sentiment],
        )
        assert result is None
        self.agent._stage1_repo.persist.assert_not_called()
        self.agent._stage1_repo.update_status.assert_not_called()

    async def test_high_conviction_flip_emits_close(self):
        existing = _make_open_pos(direction="long")
        self.agent._stage1_repo.load_open.return_value = existing
        # conviction=0.80 > default flip threshold (0.65)
        self._setup_debate(_make_thesis_verdict(direction="SHORT", conviction=0.80))
        result = await self.agent._build_signal(
            self.sentiment,
            self.market,
            self.event_lookup,
            self.estimates,
            group=[self.sentiment],
        )
        assert result is not None
        assert result.direction == SignalDirection.CLOSE
        self.agent._stage1_repo.update_status.assert_called_once_with(
            existing.id, Stage1Status.REVERSED
        )

    async def test_low_conviction_flip_keeps_position(self):
        existing = _make_open_pos(direction="long")
        self.agent._stage1_repo.load_open.return_value = existing
        # conviction=0.50 <= default flip threshold (0.65) → keep
        self._setup_debate(_make_thesis_verdict(direction="SHORT", conviction=0.50))
        result = await self.agent._build_signal(
            self.sentiment,
            self.market,
            self.event_lookup,
            self.estimates,
            group=[self.sentiment],
        )
        assert result is None
        self.agent._stage1_repo.update_status.assert_not_called()

    async def test_no_new_news_skips_debate(self):
        existing = _make_open_pos(direction="long")
        self.agent._stage1_repo.load_open.return_value = existing
        self._setup_debate(_make_thesis_verdict(direction="LONG", conviction=0.80))
        # All event_ids start with "ticker_earn_pre_" → new_news_present=False
        ephemeral_sentiment = _make_sentiment(
            event_id="ticker_earn_pre_AAPL_2026-04-15",
            label=SentimentLabel.BULLISH,
            score=0.80,
            confidence=0.90,
        )
        ephemeral_event = _make_earn_event(event_type=EventType.EARN_PRE)
        result = await self.agent._build_signal(
            ephemeral_sentiment,
            self.market,
            {"ticker_earn_pre_AAPL_2026-04-15": ephemeral_event},
            self.estimates,
            group=[ephemeral_sentiment],
        )
        assert result is None
        self.agent._run_thesis_debate.assert_not_called()


# ---------------------------------------------------------------------------
# TestBuildThesisPrompts — prompt builder company_context injection
# ---------------------------------------------------------------------------


class TestBuildThesisPrompts:
    def test_build_thesis_bull_prompt_includes_company_context(self):
        ctx = "Company snapshot (ALOT):\n  Price: $5.42 | 52-week: 12.3%"
        result = _build_thesis_bull_prompt(
            ticker="ALOT",
            fiscal_quarter="Q2 2026",
            days_until_report=3,
            beat_rate=0.65,
            beat_rate_source="default",
            eps_estimate=0.12,
            news_summaries=["Revenue outlook positive."],
            company_context=ctx,
        )
        assert ctx in result

    def test_build_thesis_bull_prompt_without_context_omits_block(self):
        result = _build_thesis_bull_prompt(
            ticker="ALOT",
            fiscal_quarter="Q2 2026",
            days_until_report=3,
            beat_rate=0.65,
            beat_rate_source="default",
            eps_estimate=None,
            news_summaries=[],
        )
        assert "Company snapshot" not in result


# ---------------------------------------------------------------------------
# TestFetchCompanySnapshot — yfinance helper
# ---------------------------------------------------------------------------


class TestFetchCompanySnapshot:
    def test_returns_empty_on_yfinance_error(self):
        from unittest.mock import patch

        with patch("yfinance.Ticker", side_effect=Exception("network error")):
            result = _fetch_company_snapshot("X")

        assert result == ""

    def test_returns_empty_when_info_raises(self):
        from unittest.mock import MagicMock, PropertyMock, patch

        mock_ticker = MagicMock()
        type(mock_ticker).info = PropertyMock(side_effect=Exception("timeout"))

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = _fetch_company_snapshot("X")

        assert result == ""


# ---------------------------------------------------------------------------
# TestRunThesisDebateContextPropagation
# ---------------------------------------------------------------------------


class TestRunThesisDebateContextPropagation:
    def setup_method(self) -> None:
        from unittest.mock import AsyncMock

        from news_trade.services.llm_client import LLMResponse

        self.agent = _make_earn_agent(beat_rate=0.72)
        # Replace the MagicMock LLMs with proper AsyncMocks so invoke() is awaitable.
        quick_response = LLMResponse(
            content="Bullish because growth momentum.",
            model_id="claude-haiku-4-5-20251001",
            provider="anthropic",
        )
        deep_response = LLMResponse(
            content='{"direction": "LONG", "conviction": 0.78, "reasoning": "good"}',
            model_id="claude-sonnet-4-6",
            provider="anthropic",
        )
        self.agent._llm.quick.invoke = AsyncMock(return_value=quick_response)
        self.agent._llm.deep.invoke = AsyncMock(return_value=deep_response)

    async def test_company_context_appears_in_quick_invoke_prompt(self):
        ctx = "Company snapshot (TEST):\n  Price: $42.00 | 52-week: 5.0%"
        await self.agent._run_thesis_debate(
            ticker="TEST",
            days_until_report=4,
            fiscal_quarter="Q3 2026",
            beat_rate=0.72,
            beat_rate_source="observed",
            news_summaries=["Strong demand.", "Margins expanding."],
            eps_estimate=1.25,
            company_context=ctx,
        )
        # quick.invoke is called twice (bull + bear); both should contain the context.
        call_args_list = self.agent._llm.quick.invoke.call_args_list
        assert len(call_args_list) == 2
        for call in call_args_list:
            prompt = call.args[0]
            assert ctx in prompt

    async def test_no_context_does_not_inject_snapshot_block(self):
        await self.agent._run_thesis_debate(
            ticker="TEST",
            days_until_report=4,
            fiscal_quarter="Q3 2026",
            beat_rate=0.72,
            beat_rate_source="observed",
            news_summaries=[],
            eps_estimate=None,
            company_context="",
        )
        for call in self.agent._llm.quick.invoke.call_args_list:
            prompt = call.args[0]
            assert "Company snapshot" not in prompt
