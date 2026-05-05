"""Tests for the gov-trade LangGraph pipeline — Task 8 checkpoint.

Covers:
  - _synthesise_node: MaterialityResult → SentimentResult + NewsEvent conversion
  - _map_direction: label + score mapping for all three directions
  - Router functions: _has_awards, _has_resolutions, _has_materiality,
    _route_after_risk
  - Graph structure: correct node names and conditional edges
  - End-to-end (checkpoint): fixture ContractAwardEvent → TradeSignal at
    ExecutionAgent, all live API calls replaced by AsyncMock agents
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gov_trade.graph.pipeline import (
    EXECUTION,
    HALT,
    MARKET,
    MATERIALITY,
    POLLER,
    RESOLVER,
    RISK,
    SIGNAL,
    SYNTHESIS,
    _has_awards,
    _has_materiality,
    _has_resolutions,
    _map_direction,
    _route_after_risk,
    _synthesise_node,
    build_gov_pipeline,
)
from news_trade.config import GovTradeSettings, Settings
from news_trade.models.contracts import (
    ContractAwardEvent,
    EntityResolution,
    MaterialityDirection,
    MaterialityResult,
    NoveltyClass,
    ResolutionLayer,
)
from news_trade.models.events import EventType
from news_trade.models.portfolio import PortfolioState
from news_trade.models.sentiment import SentimentLabel
from news_trade.models.signals import SignalDirection, TradeSignal
from news_trade.services.event_bus import EventBus

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def _make_gov_settings() -> GovTradeSettings:
    return GovTradeSettings()


def _make_award(**kwargs: object) -> ContractAwardEvent:
    defaults: dict[str, object] = dict(
        award_id="AWARD-001",
        recipient_name="LOCKHEED MARTIN CORPORATION",
        amount_usd=50_000_000.0,
        awarding_agency="DEPT OF DEFENSE",
        award_type="D",
        sign_date=datetime.utcnow().date(),
    )
    return ContractAwardEvent(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_resolution(**kwargs: object) -> EntityResolution:
    defaults: dict[str, object] = dict(
        recipient_name="LOCKHEED MARTIN CORPORATION",
        ticker="LMT",
        exchange="NYSE",
        confidence=1.0,
        layer=ResolutionLayer.STATIC,
    )
    return EntityResolution(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_materiality(**kwargs: object) -> MaterialityResult:
    defaults: dict[str, object] = dict(
        award_id="AWARD-001",
        ticker="LMT",
        score=8.0,
        adjusted_score=8.0,
        direction=MaterialityDirection.BULLISH,
        novelty=NoveltyClass.RENEWAL,
        reasoning="Large defence contract renewal",
    )
    return MaterialityResult(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_signal(**kwargs: object) -> TradeSignal:
    defaults: dict[str, object] = dict(
        signal_id=str(uuid4()),
        event_id="gov_contract_AWARD-001",
        ticker="LMT",
        direction=SignalDirection.LONG,
        conviction=0.81,
        suggested_qty=5,
        passed_confidence_gate=True,
    )
    return TradeSignal(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_portfolio() -> PortfolioState:
    return PortfolioState(equity=100_000.0, cash=50_000.0)


def _make_mock_bus() -> MagicMock:
    return MagicMock(spec=EventBus)


# ---------------------------------------------------------------------------
# _synthesise_node
# ---------------------------------------------------------------------------


class TestSynthesisNode:
    async def test_bullish_result_produces_bullish_sentiment(self) -> None:
        result = _make_materiality(
            direction=MaterialityDirection.BULLISH, adjusted_score=8.0
        )
        state = {"materiality_results": [result]}

        out = await _synthesise_node(state)  # type: ignore[arg-type]

        assert len(out["sentiment_results"]) == 1
        sr = out["sentiment_results"][0]
        assert sr.label == SentimentLabel.BULLISH
        assert sr.score > 0

    async def test_bearish_result_produces_bearish_sentiment(self) -> None:
        result = _make_materiality(
            direction=MaterialityDirection.BEARISH, adjusted_score=6.0
        )
        state = {"materiality_results": [result]}

        out = await _synthesise_node(state)  # type: ignore[arg-type]

        sr = out["sentiment_results"][0]
        assert sr.label == SentimentLabel.BEARISH
        assert sr.score < 0

    async def test_neutral_result_produces_zero_score(self) -> None:
        result = _make_materiality(
            direction=MaterialityDirection.NEUTRAL, adjusted_score=5.0
        )
        state = {"materiality_results": [result]}

        out = await _synthesise_node(state)  # type: ignore[arg-type]

        sr = out["sentiment_results"][0]
        assert sr.label == SentimentLabel.NEUTRAL
        assert sr.score == 0.0

    async def test_event_id_matches_between_news_event_and_sentiment(self) -> None:
        result = _make_materiality(award_id="AWARD-XYZ")
        state = {"materiality_results": [result]}

        out = await _synthesise_node(state)  # type: ignore[arg-type]

        assert out["news_events"][0].event_id == "gov_contract_AWARD-XYZ"
        assert out["sentiment_results"][0].event_id == "gov_contract_AWARD-XYZ"

    async def test_news_event_has_correct_source_and_type(self) -> None:
        state = {"materiality_results": [_make_materiality()]}

        out = await _synthesise_node(state)  # type: ignore[arg-type]

        ne = out["news_events"][0]
        assert ne.source == "usaspending"
        assert ne.event_type == EventType.OTHER

    async def test_ticker_propagated_to_both_objects(self) -> None:
        result = _make_materiality(ticker="RTX")
        state = {"materiality_results": [result]}

        out = await _synthesise_node(state)  # type: ignore[arg-type]

        assert out["news_events"][0].tickers == ["RTX"]
        assert out["sentiment_results"][0].ticker == "RTX"

    async def test_confidence_normalised_to_zero_one(self) -> None:
        result = _make_materiality(adjusted_score=10.0)
        state = {"materiality_results": [result]}

        out = await _synthesise_node(state)  # type: ignore[arg-type]

        assert out["sentiment_results"][0].confidence == pytest.approx(1.0)

    async def test_adjusted_score_above_ceil_clamped_to_one(self) -> None:
        result = _make_materiality(adjusted_score=15.0)  # above _SCORE_CEIL=10
        state = {"materiality_results": [result]}

        out = await _synthesise_node(state)  # type: ignore[arg-type]

        sr = out["sentiment_results"][0]
        assert sr.confidence == pytest.approx(1.0)
        assert abs(sr.score) == pytest.approx(1.0)

    async def test_empty_materiality_results_produces_empty_output(self) -> None:
        out = await _synthesise_node({"materiality_results": []})  # type: ignore[arg-type]

        assert out["news_events"] == []
        assert out["sentiment_results"] == []

    async def test_multiple_results_all_synthesised(self) -> None:
        results = [
            _make_materiality(award_id=f"A{i}", ticker=f"T{i}") for i in range(3)
        ]
        state = {"materiality_results": results}

        out = await _synthesise_node(state)  # type: ignore[arg-type]

        assert len(out["news_events"]) == 3
        assert len(out["sentiment_results"]) == 3


# ---------------------------------------------------------------------------
# _map_direction
# ---------------------------------------------------------------------------


class TestMapDirection:
    def test_bullish_positive_score(self) -> None:
        label, score = _map_direction(MaterialityDirection.BULLISH, 0.7)
        assert label == SentimentLabel.BULLISH
        assert score == pytest.approx(0.7)

    def test_bearish_negative_score(self) -> None:
        label, score = _map_direction(MaterialityDirection.BEARISH, 0.7)
        assert label == SentimentLabel.BEARISH
        assert score == pytest.approx(-0.7)

    def test_neutral_zero_score(self) -> None:
        label, score = _map_direction(MaterialityDirection.NEUTRAL, 0.5)
        assert label == SentimentLabel.NEUTRAL
        assert score == 0.0


# ---------------------------------------------------------------------------
# Router functions
# ---------------------------------------------------------------------------


class TestRouters:
    def test_has_awards_true_when_awards_present(self) -> None:
        assert _has_awards({"contract_awards": [_make_award()]}) is True  # type: ignore[arg-type]

    def test_has_awards_false_when_empty(self) -> None:
        assert _has_awards({"contract_awards": []}) is False  # type: ignore[arg-type]

    def test_has_awards_false_when_key_absent(self) -> None:
        assert _has_awards({}) is False  # type: ignore[arg-type]

    def test_has_resolutions_true_when_resolutions_present(self) -> None:
        assert _has_resolutions({"resolutions": [_make_resolution()]}) is True  # type: ignore[arg-type]

    def test_has_resolutions_false_when_empty(self) -> None:
        assert _has_resolutions({"resolutions": []}) is False  # type: ignore[arg-type]

    def test_has_materiality_true_when_results_present(self) -> None:
        assert _has_materiality({"materiality_results": [_make_materiality()]}) is True  # type: ignore[arg-type]

    def test_has_materiality_false_when_empty(self) -> None:
        assert _has_materiality({"materiality_results": []}) is False  # type: ignore[arg-type]

    def test_route_after_risk_halts_when_system_halted(self) -> None:
        state = {"system_halted": True, "approved_signals": [_make_signal()]}
        assert _route_after_risk(state) == "halt"  # type: ignore[arg-type]

    def test_route_after_risk_executes_when_signals_approved(self) -> None:
        state = {"system_halted": False, "approved_signals": [_make_signal()]}
        assert _route_after_risk(state) == "execute"  # type: ignore[arg-type]

    def test_route_after_risk_ends_when_no_signals(self) -> None:
        state = {"system_halted": False, "approved_signals": []}
        assert _route_after_risk(state) == "end"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Graph structure
# ---------------------------------------------------------------------------

EXPECTED_NODES = {
    POLLER,
    RESOLVER,
    MATERIALITY,
    SYNTHESIS,
    MARKET,
    SIGNAL,
    RISK,
    EXECUTION,
    HALT,
}


class TestBuildGovPipeline:
    def _make_mock_agent(self) -> MagicMock:
        m = MagicMock()
        m.run = AsyncMock(return_value={})
        return m

    def _build(self) -> object:
        settings = _make_settings()
        gov_settings = _make_gov_settings()
        bus = _make_mock_bus()
        stage1_repo = MagicMock()

        return build_gov_pipeline(
            settings=settings,
            gov_settings=gov_settings,
            event_bus=bus,
            stage1_repo=stage1_repo,
            contract_poller=self._make_mock_agent(),  # type: ignore[arg-type]
            entity_resolver=self._make_mock_agent(),  # type: ignore[arg-type]
            materiality_scorer=self._make_mock_agent(),  # type: ignore[arg-type]
            exec_agent=self._make_mock_agent(),  # type: ignore[arg-type]
            halt_agent=self._make_mock_agent(),  # type: ignore[arg-type]
            signal_agent=self._make_mock_agent(),  # type: ignore[arg-type]
            risk_agent=self._make_mock_agent(),  # type: ignore[arg-type]
            market_agent=self._make_mock_agent(),  # type: ignore[arg-type]
        )

    def test_returns_compiled_graph(self) -> None:
        assert self._build() is not None

    def test_graph_has_all_expected_nodes(self) -> None:
        graph = self._build()
        user_nodes = {n for n in graph.nodes if not n.startswith("__")}  # type: ignore[union-attr]
        assert user_nodes == EXPECTED_NODES

    def test_graph_has_conditional_edge_after_poller(self) -> None:
        graph = self._build()
        assert POLLER in graph.builder.branches  # type: ignore[union-attr]

    def test_graph_has_conditional_edge_after_resolver(self) -> None:
        graph = self._build()
        assert RESOLVER in graph.builder.branches  # type: ignore[union-attr]

    def test_graph_has_conditional_edge_after_materiality(self) -> None:
        graph = self._build()
        assert MATERIALITY in graph.builder.branches  # type: ignore[union-attr]

    def test_graph_has_conditional_edge_after_risk(self) -> None:
        graph = self._build()
        assert RISK in graph.builder.branches  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# End-to-end checkpoint — fixture ContractAwardEvent → TradeSignal
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Checkpoint: full pipeline run with mock agents.

    Each agent is replaced by an AsyncMock that returns fixture data. The
    synthesis node runs for real (it is a pure function). The test confirms
    that a TradeSignal flows all the way from a ContractAwardEvent through
    the synthesis bridge to the ExecutionAgent.
    """

    def _build_pipeline(
        self,
        *,
        poller_result: dict,
        resolver_result: dict,
        materiality_result: dict,
        signal_result: dict,
        risk_result: dict,
    ) -> object:
        settings = _make_settings()
        gov_settings = _make_gov_settings()
        bus = _make_mock_bus()
        stage1_repo = MagicMock()

        def _mock(result: dict) -> MagicMock:
            m = MagicMock()
            m.run = AsyncMock(return_value=result)
            return m

        exec_calls: list[dict] = []

        async def _exec_run(state: dict) -> dict:
            exec_calls.append(dict(state))
            return {"orders": []}

        exec_mock = MagicMock()
        exec_mock.run = _exec_run

        graph = build_gov_pipeline(
            settings=settings,
            gov_settings=gov_settings,
            event_bus=bus,
            stage1_repo=stage1_repo,
            contract_poller=_mock(poller_result),  # type: ignore[arg-type]
            entity_resolver=_mock(resolver_result),  # type: ignore[arg-type]
            materiality_scorer=_mock(materiality_result),  # type: ignore[arg-type]
            exec_agent=exec_mock,  # type: ignore[arg-type]
            halt_agent=_mock({}),  # type: ignore[arg-type]
            signal_agent=_mock(signal_result),  # type: ignore[arg-type]
            risk_agent=_mock(risk_result),  # type: ignore[arg-type]
            market_agent=_mock({"market_context": {}}),  # type: ignore[arg-type]
        )
        return graph, exec_calls

    async def test_fixture_award_produces_trade_signal(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        materiality = _make_materiality()
        signal = _make_signal()

        graph, _exec_calls = self._build_pipeline(
            poller_result={"contract_awards": [award]},
            resolver_result={"resolutions": [resolution]},
            materiality_result={"materiality_results": [materiality]},
            signal_result={"trade_signals": [signal]},
            risk_result={
                "approved_signals": [signal],
                "rejected_signals": [],
                "system_halted": False,
            },
        )

        final_state = await graph.ainvoke(  # type: ignore[union-attr]
            {"portfolio": _make_portfolio()}
        )

        assert len(final_state.get("trade_signals", [])) == 1
        assert final_state["trade_signals"][0].ticker == "LMT"

    async def test_no_awards_ends_before_resolver(self) -> None:
        graph, _exec_calls = self._build_pipeline(
            poller_result={"contract_awards": []},
            resolver_result={"resolutions": [_make_resolution()]},
            materiality_result={"materiality_results": [_make_materiality()]},
            signal_result={"trade_signals": [_make_signal()]},
            risk_result={
                "approved_signals": [],
                "rejected_signals": [],
                "system_halted": False,
            },
        )

        final_state = await graph.ainvoke({"portfolio": _make_portfolio()})  # type: ignore[union-attr]

        assert final_state.get("trade_signals") is None
        assert final_state.get("resolutions") is None

    async def test_no_resolutions_ends_before_materiality(self) -> None:
        graph, _exec_calls = self._build_pipeline(
            poller_result={"contract_awards": [_make_award()]},
            resolver_result={"resolutions": []},
            materiality_result={"materiality_results": [_make_materiality()]},
            signal_result={"trade_signals": [_make_signal()]},
            risk_result={
                "approved_signals": [],
                "rejected_signals": [],
                "system_halted": False,
            },
        )

        final_state = await graph.ainvoke({"portfolio": _make_portfolio()})  # type: ignore[union-attr]

        assert final_state.get("trade_signals") is None
        assert final_state.get("materiality_results") is None

    async def test_no_materiality_results_ends_before_synthesis(self) -> None:
        graph, _exec_calls = self._build_pipeline(
            poller_result={"contract_awards": [_make_award()]},
            resolver_result={"resolutions": [_make_resolution()]},
            materiality_result={"materiality_results": []},
            signal_result={"trade_signals": [_make_signal()]},
            risk_result={
                "approved_signals": [],
                "rejected_signals": [],
                "system_halted": False,
            },
        )

        final_state = await graph.ainvoke({"portfolio": _make_portfolio()})  # type: ignore[union-attr]

        assert final_state.get("trade_signals") is None
        assert final_state.get("sentiment_results") is None

    async def test_approved_signal_reaches_execution_agent(self) -> None:
        award = _make_award()
        resolution = _make_resolution()
        materiality = _make_materiality()
        signal = _make_signal()

        exec_calls: list[dict] = []

        async def _exec_run(state: dict) -> dict:
            exec_calls.append(dict(state))
            return {"orders": []}

        settings = _make_settings()
        gov_settings = _make_gov_settings()
        bus = _make_mock_bus()
        stage1_repo = MagicMock()

        def _mock(result: dict) -> MagicMock:
            m = MagicMock()
            m.run = AsyncMock(return_value=result)
            return m

        exec_mock = MagicMock()
        exec_mock.run = _exec_run

        graph = build_gov_pipeline(
            settings=settings,
            gov_settings=gov_settings,
            event_bus=bus,
            stage1_repo=stage1_repo,
            contract_poller=_mock({"contract_awards": [award]}),  # type: ignore[arg-type]
            entity_resolver=_mock({"resolutions": [resolution]}),  # type: ignore[arg-type]
            materiality_scorer=_mock({"materiality_results": [materiality]}),  # type: ignore[arg-type]
            exec_agent=exec_mock,  # type: ignore[arg-type]
            halt_agent=_mock({}),  # type: ignore[arg-type]
            signal_agent=_mock({"trade_signals": [signal]}),  # type: ignore[arg-type]
            risk_agent=_mock({
                "approved_signals": [signal],
                "rejected_signals": [],
                "system_halted": False,
            }),  # type: ignore[arg-type]
            market_agent=_mock({"market_context": {}}),  # type: ignore[arg-type]
        )

        await graph.ainvoke({"portfolio": _make_portfolio()})  # type: ignore[union-attr]

        # ExecutionAgent.run() was called — the TradeSignal reached it
        assert len(exec_calls) == 1
        assert exec_calls[0].get("approved_signals") == [signal]
