"""Unit tests for the LangGraph pipeline construction."""

from unittest.mock import MagicMock

import pytest

from news_trade.config import Settings
from news_trade.graph.pipeline import (
    _has_approved_signals,
    _has_news_events,
    build_pipeline,
)
from news_trade.services.event_bus import EventBus

EXPECTED_NODES = {
    "news_ingestor",
    "market_data",
    "sentiment_analyst",
    "signal_generator",
    "risk_manager",
    "execution",
}


@pytest.fixture()
def mock_settings() -> MagicMock:
    m = MagicMock(spec=Settings)
    m.database_url = "sqlite://"  # in-memory SQLite — no file needed
    return m


@pytest.fixture()
def mock_event_bus() -> MagicMock:
    return MagicMock(spec=EventBus)


@pytest.fixture()
def compiled_graph(mock_settings, mock_event_bus):
    return build_pipeline(mock_settings, mock_event_bus)


class TestBuildPipeline:
    def test_returns_compiled_graph(self, compiled_graph):
        assert compiled_graph is not None

    def test_graph_has_all_nodes(self, compiled_graph):
        # compiled_graph.nodes includes __start__; filter internal nodes out
        user_nodes = {n for n in compiled_graph.nodes if not n.startswith("__")}
        assert user_nodes == EXPECTED_NODES

    def test_graph_has_conditional_edge_after_news_ingestor(self, compiled_graph):
        # builder.branches holds the conditional-edge specs keyed by source node
        assert "news_ingestor" in compiled_graph.builder.branches

    def test_graph_has_conditional_edge_after_risk_manager(self, compiled_graph):
        assert "risk_manager" in compiled_graph.builder.branches


class TestHasNewsEvents:
    def test_returns_true_when_events_present(self):
        state = {"news_events": [object()]}
        assert _has_news_events(state) is True

    def test_returns_false_when_empty_list(self):
        state = {"news_events": []}
        assert _has_news_events(state) is False

    def test_returns_false_when_key_absent(self):
        assert _has_news_events({}) is False


class TestHasApprovedSignals:
    def test_returns_true_when_signals_present(self):
        state = {"approved_signals": [object()]}
        assert _has_approved_signals(state) is True

    def test_returns_false_when_empty_list(self):
        state = {"approved_signals": []}
        assert _has_approved_signals(state) is False

    def test_returns_false_when_key_absent(self):
        assert _has_approved_signals({}) is False
