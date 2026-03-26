"""LangGraph pipeline construction for the trading system.

Defines the full agent graph with conditional routing so that the pipeline
exits early when there is no work to do (e.g. no new news, all signals
rejected by risk).
"""

from __future__ import annotations

from alpaca.trading.client import TradingClient
from langgraph.graph import END, StateGraph

from news_trade.agents.execution import ExecutionAgent
from news_trade.agents.market_data import MarketDataAgent
from news_trade.agents.news_ingestor import NewsIngestorAgent
from news_trade.agents.risk_manager import RiskManagerAgent
from news_trade.agents.sentiment_analyst import SentimentAnalystAgent
from news_trade.agents.signal_generator import SignalGeneratorAgent
from news_trade.config import Settings
from news_trade.graph.state import PipelineState
from news_trade.providers import (
    get_market_data_provider,
    get_news_provider,
    get_sentiment_provider,
)
from news_trade.services.database import build_engine, build_session_factory
from news_trade.services.event_bus import EventBus
from news_trade.services.llm_client import LLMClientFactory
from news_trade.services.tables import Base

# Node name constants
NEWS = "news_ingestor"
MARKET = "market_data"
SENTIMENT = "sentiment_analyst"
SIGNAL = "signal_generator"
RISK = "risk_manager"
EXECUTION = "execution"


def build_pipeline(settings: Settings, event_bus: EventBus) -> StateGraph:
    """Build and compile the LangGraph state graph.

    Graph topology::

        news_ingestor
            ↓ (has events?)
        market_data
            ↓
        sentiment_analyst
            ↓
        signal_generator
            ↓
        risk_manager
            ↓ (any approved?)
        execution
            ↓
        END

    Args:
        settings: Application configuration.
        event_bus: Redis-backed event bus shared across agents.

    Returns:
        A compiled LangGraph ``StateGraph``.
    """
    news_agent = NewsIngestorAgent(settings, event_bus, provider=get_news_provider(settings))
    market_agent = MarketDataAgent(settings, event_bus, provider=get_market_data_provider(settings))
    sentiment_agent = SentimentAnalystAgent(settings, event_bus, provider=get_sentiment_provider(settings))
    signal_agent = SignalGeneratorAgent(settings, event_bus, llm=LLMClientFactory(settings))
    risk_agent = RiskManagerAgent(settings, event_bus)
    alpaca_client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=True,
    )
    exec_engine = build_engine(settings)
    Base.metadata.create_all(exec_engine)
    exec_session = build_session_factory(settings)()
    exec_agent = ExecutionAgent(
        settings,
        event_bus,
        alpaca_client=alpaca_client,
        session=exec_session,
    )

    graph = StateGraph(PipelineState)

    # Register nodes
    graph.add_node(NEWS, news_agent.run)
    graph.add_node(MARKET, market_agent.run)
    graph.add_node(SENTIMENT, sentiment_agent.run)
    graph.add_node(SIGNAL, signal_agent.run)
    graph.add_node(RISK, risk_agent.run)
    graph.add_node(EXECUTION, exec_agent.run)

    # Entry point
    graph.set_entry_point(NEWS)

    # Conditional: only proceed if news was found
    graph.add_conditional_edges(
        NEWS,
        _has_news_events,
        {True: MARKET, False: END},
    )

    # Linear edges through analysis pipeline
    graph.add_edge(MARKET, SENTIMENT)
    graph.add_edge(SENTIMENT, SIGNAL)
    graph.add_edge(SIGNAL, RISK)

    # Conditional: only execute if risk approved at least one signal
    graph.add_conditional_edges(
        RISK,
        _has_approved_signals,
        {True: EXECUTION, False: END},
    )

    graph.add_edge(EXECUTION, END)

    return graph.compile()


def _has_news_events(state: PipelineState) -> bool:
    """Return True if the ingestor produced at least one news event."""
    return bool(state.get("news_events"))


def _has_approved_signals(state: PipelineState) -> bool:
    """Return True if risk management approved at least one signal."""
    return bool(state.get("approved_signals"))
