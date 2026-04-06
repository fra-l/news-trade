"""LangGraph Studio entry points.

Exports two compiled graphs for use with ``langgraph dev --allow-blocking``:

``graph``
    Normal pipeline — identical to production.  Submit ``{}`` as the initial
    state to trigger a live news fetch; or manually provide ``news_events`` +
    ``replay_mode=true`` to bypass the live fetch.

``replay_graph``
    Pipeline with an automatic seed node prepended.  The seed node loads the
    last 5 events from the local SQLite DB before ``portfolio_fetcher`` runs,
    so the full chain always fires — no live news source or configuration
    required.  Just submit ``{}`` as the initial state.

Both graphs use a no-op EventBus stub so Redis is not required.
TradingClient is initialised with .env credentials but Alpaca API calls will fail
gracefully at node execution time — execution/portfolio nodes log errors, all
other nodes (sentiment, signal, risk) work normally.
"""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from langgraph.graph import END, StateGraph
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from news_trade.config import get_settings
from news_trade.graph.pipeline import (
    EXECUTION,
    HALT,
    MARKET,
    NEWS,
    PORTFOLIO,
    RISK,
    SENTIMENT,
    SIGNAL,
    _has_news_events,
    _route_after_risk,
    build_pipeline,
)
from news_trade.graph.state import PipelineState
from news_trade.models import NewsEvent
from news_trade.models.events import EventType
from news_trade.services.database import build_engine, create_tables
from news_trade.services.event_bus import EventBus
from news_trade.services.tables import NewsEventRow

_STUDIO_SEED = "studio_seed"


def _make_stub_event_bus() -> EventBus:
    """Return a no-op EventBus that satisfies the type without needing Redis."""
    bus = MagicMock(spec=EventBus)
    bus.publish = AsyncMock()
    bus.connect = AsyncMock()
    bus.close = AsyncMock()
    bus.subscribe = AsyncMock()
    return cast(EventBus, bus)


_settings = get_settings()
create_tables(_settings)  # ensure schema exists; no-op if already up-to-date

# --------------------------------------------------------------------------- #
# Normal graph (identical to production)                                       #
# --------------------------------------------------------------------------- #
graph = build_pipeline(_settings, _make_stub_event_bus())


# --------------------------------------------------------------------------- #
# Replay graph — studio_seed node + full pipeline wired inline                 #
# --------------------------------------------------------------------------- #

_REPLAY_LIMIT = 5  # number of recent DB events loaded by the replay graph


async def _studio_seed_node(state: dict) -> dict:  # type: ignore[type-arg]
    """Load the most recent DB events and enable replay mode.

    Always loads the last ``_REPLAY_LIMIT`` events across all tickers —
    no configuration required.  Sets ``replay_mode=True`` so
    ``NewsIngestorAgent`` skips the live fetch and passes through the
    pre-loaded events instead.
    """
    ticker: str = ""
    limit: int = _REPLAY_LIMIT

    def _load() -> list[NewsEvent]:
        engine = build_engine(_settings)
        with Session(engine) as session:
            query = (
                select(NewsEventRow)
                .order_by(desc(NewsEventRow.ingested_at))
                .limit(limit)
            )
            if ticker:
                query = query.where(
                    NewsEventRow.tickers_json.contains(f'"{ticker}"')
                )
            rows = session.execute(query).scalars().all()
        return [
            NewsEvent(
                event_id=row.event_id,
                headline=row.headline,
                summary=row.summary,
                source=row.source,
                url=row.url,
                tickers=row.tickers,
                event_type=EventType(row.event_type),
                published_at=row.published_at,
            )
            for row in rows
        ]

    events = await asyncio.to_thread(_load)
    return {"news_events": events, "replay_mode": True}


def _build_replay_pipeline() -> StateGraph:
    """Build the replay variant of the pipeline.

    Topology::

        studio_seed  (loads DB events, sets replay_mode=True)
            ↓
        portfolio_fetcher  (fetches live equity + positions from Alpaca)
            ↓
        news_ingestor  (replay_mode=True → passes through pre-loaded events)
            ↓ (has events?)
        market_data → sentiment_analyst → signal_generator → risk_manager
                                                                  ↓ (approved?)
                                                              execution → END

    All agents are freshly constructed (independent of ``graph``).
    """
    from alpaca.trading.client import TradingClient

    from news_trade.agents.execution import ExecutionAgent
    from news_trade.agents.halt_handler import HaltHandlerAgent
    from news_trade.agents.market_data import MarketDataAgent
    from news_trade.agents.news_ingestor import NewsIngestorAgent
    from news_trade.agents.portfolio_fetcher import PortfolioFetcherAgent
    from news_trade.agents.risk_manager import RiskManagerAgent
    from news_trade.agents.sentiment_analyst import SentimentAnalystAgent
    from news_trade.agents.signal_generator import SignalGeneratorAgent
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
    from news_trade.services.llm_client import LLMClientFactory
    from news_trade.services.stage1_repository import Stage1Repository
    from news_trade.services.watchlist_manager import WatchlistManager

    event_bus = _make_stub_event_bus()

    wl_session = build_session_factory(_settings)()
    wl_manager = WatchlistManager(
        settings=_settings,
        session=wl_session,
        primary=get_calendar_provider(_settings),
        fallback=YFinanceCalendarProvider(),
    )

    news_agent = NewsIngestorAgent(
        _settings, event_bus,
        provider=get_news_provider(_settings),
        watchlist_manager=wl_manager,
    )
    market_agent = MarketDataAgent(
        _settings, event_bus, provider=get_market_data_provider(_settings)
    )
    sentiment_agent = SentimentAnalystAgent(
        _settings, event_bus,
        provider=get_sentiment_provider(_settings),
        watchlist_manager=wl_manager,
    )

    shared_session = build_session_factory(_settings)()
    stage1_repo = Stage1Repository(shared_session)
    scorer = ConfidenceScorer(settings=_settings, renderer=EstimatesRenderer())

    signal_agent = SignalGeneratorAgent(
        _settings, event_bus,
        llm=LLMClientFactory(_settings),
        scorer=scorer,
        stage1_repo=stage1_repo,
    )
    risk_agent = RiskManagerAgent(_settings, event_bus, stage1_repo=stage1_repo)

    alpaca_client = TradingClient(
        api_key=_settings.alpaca_api_key,
        secret_key=_settings.alpaca_secret_key,
        paper=True,
    )
    portfolio_agent = PortfolioFetcherAgent(
        _settings, event_bus, alpaca_client=alpaca_client
    )
    exec_session = build_session_factory(_settings)()
    exec_agent = ExecutionAgent(
        _settings, event_bus,
        alpaca_client=alpaca_client,
        session=exec_session,
    )
    halt_agent = HaltHandlerAgent(
        _settings, event_bus,
        alpaca_client=alpaca_client,
        stage1_repo=stage1_repo,
    )

    g: StateGraph = StateGraph(PipelineState)

    g.add_node(_STUDIO_SEED, _studio_seed_node)
    g.add_node(PORTFOLIO, portfolio_agent.run)
    g.add_node(NEWS, news_agent.run)
    g.add_node(MARKET, market_agent.run)
    g.add_node(SENTIMENT, sentiment_agent.run)
    g.add_node(SIGNAL, signal_agent.run)
    g.add_node(RISK, risk_agent.run)
    g.add_node(EXECUTION, exec_agent.run)
    g.add_node(HALT, halt_agent.run)

    g.set_entry_point(_STUDIO_SEED)
    g.add_edge(_STUDIO_SEED, PORTFOLIO)
    g.add_edge(PORTFOLIO, NEWS)
    g.add_conditional_edges(NEWS, _has_news_events, {True: MARKET, False: END})
    g.add_edge(MARKET, SENTIMENT)
    g.add_edge(SENTIMENT, SIGNAL)
    g.add_edge(SIGNAL, RISK)
    g.add_conditional_edges(
        RISK, _route_after_risk,
        {"halt": HALT, "execute": EXECUTION, "end": END},
    )
    g.add_edge(HALT, END)
    g.add_edge(EXECUTION, END)

    return g.compile()


replay_graph = _build_replay_pipeline()
