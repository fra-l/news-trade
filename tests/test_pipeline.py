"""Unit tests for the LangGraph pipeline construction."""

from unittest.mock import MagicMock

import pytest

from news_trade.config import (
    MarketDataProviderType,
    NewsProviderType,
    SentimentProviderType,
    Settings,
)
from news_trade.graph.pipeline import (
    _has_approved_signals,
    _has_work_to_do,
    _route_after_risk,
    build_pipeline,
)
from news_trade.services.event_bus import EventBus

EXPECTED_NODES = {
    "portfolio_fetcher",
    "news_ingestor",
    "earnings_ticker",   # new: 3rd parallel branch
    "post_init",         # new: fan-in synchronisation node
    "analysis_fan",      # new: fan-out synchronisation node
    "market_data",
    "sentiment_analyst",
    "signal_generator",
    "risk_manager",
    "execution",
    "halt_handler",
}


@pytest.fixture()
def mock_settings() -> MagicMock:
    # spec against an instance: Pydantic v2 fields appear in dir(instance) but
    # not in dir(class), so MagicMock(spec=Settings) would reject field access.
    m = MagicMock(spec=Settings())
    m.database_url = "sqlite://"  # in-memory SQLite — no file needed
    m.news_provider = NewsProviderType.RSS
    m.market_data_provider = MarketDataProviderType.YFINANCE
    m.sentiment_provider = SentimentProviderType.KEYWORD
    m.llm_provider = "anthropic"
    m.llm_quick_model = "claude-haiku-4-5-20251001"
    m.llm_deep_model = "claude-sonnet-4-6"
    return m


@pytest.fixture()
def mock_event_bus() -> MagicMock:
    return MagicMock(spec=EventBus)


@pytest.fixture()
def compiled_graph(mock_settings, mock_event_bus):
    return build_pipeline(mock_settings, mock_event_bus, ["AAPL"])


class TestBuildPipeline:
    def test_returns_compiled_graph(self, compiled_graph):
        assert compiled_graph is not None

    def test_graph_has_all_nodes(self, compiled_graph):
        # compiled_graph.nodes includes __start__; filter internal nodes out
        user_nodes = {n for n in compiled_graph.nodes if not n.startswith("__")}
        assert user_nodes == EXPECTED_NODES

    def test_graph_has_conditional_edge_after_post_init(self, compiled_graph):
        # The gate (_has_work_to_do) is now on post_init, not news_ingestor
        assert "post_init" in compiled_graph.builder.branches

    def test_graph_has_conditional_edge_after_risk_manager(self, compiled_graph):
        assert "risk_manager" in compiled_graph.builder.branches


class TestHasWorkToDo:
    def test_returns_true_when_news_events_present(self):
        assert _has_work_to_do({"news_events": [object()]}) is True

    def test_returns_true_when_active_tickers_present(self):
        # Key scenario: no news but active earnings tickers → pipeline continues
        assert _has_work_to_do({"active_tickers": ["AAPL"]}) is True

    def test_returns_true_when_both_present(self):
        state = {"news_events": [object()], "active_tickers": ["AAPL"]}
        assert _has_work_to_do(state) is True

    def test_returns_false_when_both_empty(self):
        assert _has_work_to_do({"news_events": [], "active_tickers": []}) is False

    def test_returns_false_when_keys_absent(self):
        assert _has_work_to_do({}) is False


class TestHasApprovedSignals:
    def test_returns_true_when_signals_present(self):
        state = {"approved_signals": [object()]}
        assert _has_approved_signals(state) is True

    def test_returns_false_when_empty_list(self):
        state = {"approved_signals": []}
        assert _has_approved_signals(state) is False

    def test_returns_false_when_key_absent(self):
        assert _has_approved_signals({}) is False


class TestRouteAfterRisk:
    def test_routes_to_halt_when_system_halted(self):
        assert _route_after_risk({"system_halted": True}) == "halt"

    def test_routes_to_execute_when_signals_approved(self):
        assert _route_after_risk({"approved_signals": [object()]}) == "execute"

    def test_routes_to_end_when_no_signals_and_not_halted(self):
        assert _route_after_risk({}) == "end"

    def test_routes_to_end_when_empty_approved_and_not_halted(self):
        assert _route_after_risk({"approved_signals": []}) == "end"

    def test_halt_takes_priority_over_approved_signals(self):
        # Edge case: risk_dry_run=True can set system_halted=True with approved signals
        state = {"system_halted": True, "approved_signals": [object()]}
        assert _route_after_risk(state) == "halt"
