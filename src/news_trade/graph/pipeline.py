"""LangGraph pipeline construction for the trading system.

Defines the full agent graph with conditional routing so that the pipeline
exits early when there is no work to do (e.g. no new news and no active
earnings tickers in the 7-day horizon).

Parallel topology (Level 1):

    START ─┬── PortfolioFetcherAgent   (live Alpaca equity + positions)
           ├── NewsIngestorAgent       (news from RSS / Benzinga provider)
           └── EarningsTickerNode     (active earnings tickers from DB)
                       ↓ fan-in: post_init
                       │ no work? → END
                       ↓ fan-out: analysis_fan
           ┌───────────┴──────────────┐
      MarketDataAgent      SentimentAnalystAgent
           └───────────┬──────────────┘
                       ↓ fan-in: SignalGeneratorAgent
                  SignalGeneratorAgent
                       ↓
                  RiskManagerAgent
             ┌─────────┴──────────┐
         HaltHandler          ExecutionAgent
             └─────────┬──────────┘
                       ↓
                      END

``post_init`` and ``analysis_fan`` are no-op passthrough nodes whose only
purpose is synchronisation (fan-in / fan-out barrier).

``errors`` and ``news_events`` in ``PipelineState`` carry ``operator.add``
reducers so parallel nodes accumulate results without overwriting each other.
"""

from __future__ import annotations

from alpaca.trading.client import TradingClient
from langgraph.graph import END, START, StateGraph

from news_trade.agents.earnings_ticker import EarningsTickerNode
from news_trade.agents.execution import ExecutionAgent
from news_trade.agents.halt_handler import HaltHandlerAgent
from news_trade.agents.market_data import MarketDataAgent
from news_trade.agents.news_ingestor import NewsIngestorAgent
from news_trade.agents.portfolio_fetcher import PortfolioFetcherAgent
from news_trade.agents.risk_manager import RiskManagerAgent
from news_trade.agents.sentiment_analyst import SentimentAnalystAgent
from news_trade.agents.signal_generator import SignalGeneratorAgent
from news_trade.config import Settings
from news_trade.graph.state import PipelineState
from news_trade.providers import (
    get_calendar_provider,
    get_market_data_provider,
    get_news_provider,
    get_sentiment_provider,
)
from news_trade.providers.calendar.yfinance_provider import YFinanceCalendarProvider
from news_trade.services.confidence_scorer import ConfidenceScorer
from news_trade.services.database import build_session_factory
from news_trade.services.estimates_renderer import EstimatesRenderer
from news_trade.services.event_bus import EventBus
from news_trade.services.llm_client import LLMClientFactory
from news_trade.services.stage1_repository import Stage1Repository
from news_trade.services.watchlist_manager import WatchlistManager

# Node name constants
PORTFOLIO       = "portfolio_fetcher"
NEWS            = "news_ingestor"
EARNINGS_TICKER = "earnings_ticker"   # new: 3rd parallel branch
POST_INIT       = "post_init"         # fan-in: waits for portfolio + news + earnings
ANALYSIS_FAN    = "analysis_fan"      # fan-out: triggers market + sentiment in parallel
MARKET          = "market_data"
SENTIMENT       = "sentiment_analyst"
SIGNAL          = "signal_generator"
RISK            = "risk_manager"
EXECUTION       = "execution"
HALT            = "halt_handler"


def build_pipeline(settings: Settings, event_bus: EventBus) -> StateGraph:
    """Build and compile the LangGraph state graph.

    Graph topology::

        START ─┬── PortfolioFetcherAgent
               ├── NewsIngestorAgent
               └── EarningsTickerNode       ← always-on: active earnings tickers
                           ↓  (fan-in: post_init)
                           │  no work? → END
                           ↓  (fan-out: analysis_fan)
               ┌───────────┴──────────────┐
          MarketDataAgent    SentimentAnalystAgent   (parallel)
               └───────────┬──────────────┘
                           ↓  (fan-in: SignalGeneratorAgent)
                      SignalGeneratorAgent
                           ↓
                      RiskManagerAgent
                   ┌───────┴──────────┐
              HaltHandler        ExecutionAgent
                   └───────┬──────────┘
                           ↓
                          END

    Args:
        settings: Application configuration.
        event_bus: Redis-backed event bus shared across agents.

    Returns:
        A compiled LangGraph ``StateGraph``.
    """
    # WatchlistManager — shared across the three watchlist-reading agents.
    # Uses a separate session from stage1_repo to avoid state bleed.
    wl_session = build_session_factory(settings)()
    wl_manager = WatchlistManager(
        settings=settings,
        session=wl_session,
        primary=get_calendar_provider(settings),
        fallback=YFinanceCalendarProvider(),
    )

    news_agent = NewsIngestorAgent(
        settings,
        event_bus,
        provider=get_news_provider(settings),
        watchlist_manager=wl_manager,
    )
    market_agent = MarketDataAgent(
        settings, event_bus, provider=get_market_data_provider(settings)
    )
    sentiment_agent = SentimentAnalystAgent(
        settings,
        event_bus,
        provider=get_sentiment_provider(settings),
        watchlist_manager=wl_manager,
    )

    # Shared DB session for Stage1Repository (used by SignalGenerator + RiskManager +
    # EarningsTickerNode).
    shared_session = build_session_factory(settings)()
    stage1_repo = Stage1Repository(shared_session)

    scorer = ConfidenceScorer(settings=settings, renderer=EstimatesRenderer())

    signal_agent = SignalGeneratorAgent(
        settings,
        event_bus,
        llm=LLMClientFactory(settings),
        scorer=scorer,
        stage1_repo=stage1_repo,
    )
    risk_agent = RiskManagerAgent(settings, event_bus, stage1_repo=stage1_repo)

    alpaca_client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=True,
    )
    portfolio_agent = PortfolioFetcherAgent(
        settings,
        event_bus,
        alpaca_client=alpaca_client,
    )
    exec_session = build_session_factory(settings)()
    exec_agent = ExecutionAgent(
        settings,
        event_bus,
        alpaca_client=alpaca_client,
        session=exec_session,
    )

    # HaltHandlerAgent reuses the same alpaca_client and stage1_repo already built
    # above — no additional dependencies required.
    halt_agent = HaltHandlerAgent(
        settings,
        event_bus,
        alpaca_client=alpaca_client,
        stage1_repo=stage1_repo,
    )

    # EarningsTickerNode — DB-only; synthesises ephemeral EARN_PRE events each cycle.
    earnings_ticker_agent = EarningsTickerNode(
        settings,
        event_bus,
        watchlist_manager=wl_manager,
        stage1_repo=stage1_repo,
    )

    graph = StateGraph(PipelineState)

    # Register nodes
    graph.add_node(PORTFOLIO,       portfolio_agent.run)
    graph.add_node(NEWS,            news_agent.run)
    graph.add_node(EARNINGS_TICKER, earnings_ticker_agent.run)
    graph.add_node(POST_INIT,       _passthrough)
    graph.add_node(ANALYSIS_FAN,    _passthrough)
    graph.add_node(MARKET,          market_agent.run)
    graph.add_node(SENTIMENT,       sentiment_agent.run)
    graph.add_node(SIGNAL,          signal_agent.run)
    graph.add_node(RISK,            risk_agent.run)
    graph.add_node(EXECUTION,       exec_agent.run)
    graph.add_node(HALT,            halt_agent.run)

    # Three-way fan-out from START: portfolio + news + earnings ticker run in parallel
    graph.add_edge(START, PORTFOLIO)
    graph.add_edge(START, NEWS)
    graph.add_edge(START, EARNINGS_TICKER)

    # Fan-in at post_init (LangGraph waits for all three to complete)
    graph.add_edge(PORTFOLIO,       POST_INIT)
    graph.add_edge(NEWS,            POST_INIT)
    graph.add_edge(EARNINGS_TICKER, POST_INIT)

    # Gate: skip analysis entirely when there is nothing to process
    graph.add_conditional_edges(
        POST_INIT,
        _has_work_to_do,
        {True: ANALYSIS_FAN, False: END},
    )

    # Two-way fan-out from analysis_fan: market + sentiment run in parallel
    graph.add_edge(ANALYSIS_FAN, MARKET)
    graph.add_edge(ANALYSIS_FAN, SENTIMENT)

    # Fan-in at signal (LangGraph waits for both market and sentiment to complete)
    graph.add_edge(MARKET,    SIGNAL)
    graph.add_edge(SENTIMENT, SIGNAL)

    graph.add_edge(SIGNAL, RISK)

    # 3-way router after risk: halt takes priority, then execute, then end
    graph.add_conditional_edges(
        RISK,
        _route_after_risk,
        {"halt": HALT, "execute": EXECUTION, "end": END},
    )

    graph.add_edge(HALT,      END)
    graph.add_edge(EXECUTION, END)

    return graph.compile()


def _passthrough(_state: PipelineState) -> dict:  # type: ignore[type-arg]
    """No-op synchronisation node for fan-out / fan-in barriers."""
    return {}


def _has_work_to_do(state: PipelineState) -> bool:
    """Return True if there is anything for the analysis pipeline to process.

    True when at least one of:
    - ``news_events``: real news from the provider (RSS, Benzinga, etc.)
    - ``active_tickers``: earnings-calendar tickers from EarningsTickerNode
    """
    return bool(state.get("news_events") or state.get("active_tickers"))


def _has_approved_signals(state: PipelineState) -> bool:
    """Return True if risk management approved at least one signal."""
    return bool(state.get("approved_signals"))


def _route_after_risk(state: PipelineState) -> str:
    """3-way router after RiskManagerAgent.

    Priority order:
      1. ``system_halted=True`` → halt_handler (drawdown breach; cleanup required)
      2. ``approved_signals`` non-empty → execution
      3. Otherwise → END (all signals rejected, no cleanup needed)
    """
    if state.get("system_halted"):
        return "halt"
    if state.get("approved_signals"):
        return "execute"
    return "end"
