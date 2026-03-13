"""OrchestratorAgent — coordinates the pipeline via a LangGraph state graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from news_trade.agents.base import BaseAgent
from news_trade.providers import (
    get_market_data_provider,
    get_news_provider,
    get_sentiment_provider,
)

if TYPE_CHECKING:
    from langgraph.graph import StateGraph

    from news_trade.config import Settings
    from news_trade.services.event_bus import EventBus


class OrchestratorAgent(BaseAgent):
    """Builds and runs the LangGraph state graph that sequences all agents.

    The graph encodes the full pipeline:

        NewsIngestor → MarketData → SentimentAnalyst
            → SignalGenerator → RiskManager → Execution

    Providers are resolved from config via the factory functions and injected
    into each agent constructor.  Conditional edges handle early exits
    (e.g. no news, all signals rejected).
    """

    def __init__(self, settings: Settings, event_bus: EventBus) -> None:
        super().__init__(settings, event_bus)
        self._graph = self._build_graph()

    async def run(self, state: dict) -> dict:
        """Execute one full pipeline cycle through the state graph.

        Returns:
            The final PipelineState after all nodes have executed.
        """
        raise NotImplementedError

    def _build_graph(self) -> StateGraph:
        """Construct the LangGraph StateGraph with agent nodes and edges.

        Returns:
            A compiled StateGraph ready to invoke.
        """
        raise NotImplementedError

    def _should_continue_after_news(self, state: dict) -> str:
        """Conditional edge: proceed only if new events were ingested."""
        raise NotImplementedError

    def _should_continue_after_risk(self, state: dict) -> str:
        """Conditional edge: proceed only if at least one signal was approved."""
        raise NotImplementedError
