"""LangGraph pipeline for the gov-trade contract-award signal system.

Linear topology (no parallelism — each stage depends on the previous one):

    ContractPollerAgent          fetch new awards from USASpending
        ↓  no awards? → END
    EntityResolverAgent          map recipient names → tickers
        ↓  no resolutions? → END
    MaterialityScorerAgent       score + enrich each award
        ↓  no results? → END
    _synthesise_node             convert MaterialityResult → SentimentResult + NewsEvent
        ↓
    MarketDataAgent              fetch OHLCV snapshots for resolved tickers
        ↓
    SignalGeneratorAgent         reused unchanged from news_trade
        ↓
    RiskManagerAgent             reused unchanged from news_trade
        ↓  3-way: halt | execute | end
    HaltHandlerAgent | ExecutionAgent
        ↓
    END

Early-exit conditions after each data stage prevent downstream agents from
running when there is nothing to process, keeping API cost proportional to
actual award volume.

The synthesis node is the bridge between the gov-trade domain (MaterialityResult)
and the news-trade domain (SentimentResult + NewsEvent). It converts each
scored award into the format SignalGeneratorAgent already knows how to handle,
so that agent runs completely unchanged.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from gov_trade.agents.contract_poller import ContractPollerAgent
from gov_trade.agents.entity_resolver_agent import EntityResolverAgent
from gov_trade.agents.materiality_scorer import MaterialityScorerAgent
from gov_trade.graph.state import GovTradeState
from news_trade.agents.execution import ExecutionAgent
from news_trade.agents.halt_handler import HaltHandlerAgent
from news_trade.agents.market_data import MarketDataAgent
from news_trade.agents.risk_manager import RiskManagerAgent
from news_trade.agents.signal_generator import SignalGeneratorAgent
from news_trade.config import GovTradeSettings, Settings
from news_trade.models.contracts import MaterialityDirection, MaterialityResult
from news_trade.models.events import EventType, NewsEvent
from news_trade.models.sentiment import SentimentLabel, SentimentResult
from news_trade.providers import get_market_data_provider
from news_trade.providers.materiality.heuristic_provider import (
    HeuristicMaterialityProvider,
)
from news_trade.services.confidence_scorer import ConfidenceScorer
from news_trade.services.contractor_lookup import ContractorLookup
from news_trade.services.entity_resolver import EntityResolutionService
from news_trade.services.estimates_renderer import EstimatesRenderer
from news_trade.services.event_bus import EventBus
from news_trade.services.llm_client import LLMClientFactory
from news_trade.services.lobbying_enrichment import LobbyingEnrichmentService
from news_trade.services.stage1_repository import Stage1Repository

_logger = logging.getLogger(__name__)

# Node name constants
POLLER     = "contract_poller"
RESOLVER   = "entity_resolver"
MATERIALITY = "materiality_scorer"
SYNTHESIS  = "synthesis"
MARKET     = "market_data"
SIGNAL     = "signal_generator"
RISK       = "risk_manager"
EXECUTION  = "execution"
HALT       = "halt_handler"

# Normalisation ceiling for adjusted_score → SentimentResult.score mapping.
# Adjusted scores above this are clamped to ±1.0.
_SCORE_CEIL = 10.0


def build_gov_pipeline(
    settings: Settings,
    gov_settings: GovTradeSettings,
    event_bus: EventBus,
    stage1_repo: Stage1Repository,
    contract_poller: ContractPollerAgent,
    entity_resolver: EntityResolverAgent,
    materiality_scorer: MaterialityScorerAgent,
    exec_agent: ExecutionAgent,
    halt_agent: HaltHandlerAgent,
    *,
    signal_agent: SignalGeneratorAgent | None = None,
    risk_agent: RiskManagerAgent | None = None,
    market_agent: MarketDataAgent | None = None,
) -> StateGraph:
    """Build and compile the gov-trade LangGraph pipeline.

    All agents are passed in fully constructed (dependency-injected externally)
    so this function is testable without real providers or database connections.

    The three reused agents (signal_agent, risk_agent, market_agent) are built
    from settings when not supplied — pass them explicitly in tests to avoid
    real LLM / database / network dependencies.

    Args:
        settings: Global application settings.
        gov_settings: Gov-trade-specific settings.
        event_bus: Redis-backed event bus shared across agents.
        stage1_repo: Stage1 position repository shared by Signal + Risk agents.
        contract_poller: Constructed ContractPollerAgent.
        entity_resolver: Constructed EntityResolverAgent.
        materiality_scorer: Constructed MaterialityScorerAgent.
        exec_agent: Constructed ExecutionAgent.
        halt_agent: Constructed HaltHandlerAgent.
        signal_agent: Optional pre-built SignalGeneratorAgent (built from settings if
            None).
        risk_agent: Optional pre-built RiskManagerAgent (built from settings if None).
        market_agent: Optional pre-built MarketDataAgent (built from settings if None).

    Returns:
        A compiled LangGraph ``StateGraph``.
    """
    if market_agent is None:
        market_agent = MarketDataAgent(
            settings, event_bus, provider=get_market_data_provider(settings)
        )
    if signal_agent is None:
        signal_agent = SignalGeneratorAgent(
            settings,
            event_bus,
            llm=LLMClientFactory(settings),
            scorer=ConfidenceScorer(settings=settings, renderer=EstimatesRenderer()),
            stage1_repo=stage1_repo,
        )
    if risk_agent is None:
        risk_agent = RiskManagerAgent(settings, event_bus, stage1_repo=stage1_repo)

    graph = StateGraph(GovTradeState)

    graph.add_node(POLLER,      contract_poller.run)
    graph.add_node(RESOLVER,    entity_resolver.run)
    graph.add_node(MATERIALITY, materiality_scorer.run)
    graph.add_node(SYNTHESIS,   _synthesise_node)
    graph.add_node(MARKET,      market_agent.run)
    graph.add_node(SIGNAL,      signal_agent.run)
    graph.add_node(RISK,        risk_agent.run)
    graph.add_node(EXECUTION,   exec_agent.run)
    graph.add_node(HALT,        halt_agent.run)

    graph.add_edge(START, POLLER)

    graph.add_conditional_edges(
        POLLER,
        _has_awards,
        {True: RESOLVER, False: END},
    )
    graph.add_conditional_edges(
        RESOLVER,
        _has_resolutions,
        {True: MATERIALITY, False: END},
    )
    graph.add_conditional_edges(
        MATERIALITY,
        _has_materiality,
        {True: SYNTHESIS, False: END},
    )

    graph.add_edge(SYNTHESIS, MARKET)
    graph.add_edge(MARKET,    SIGNAL)
    graph.add_edge(SIGNAL,    RISK)

    graph.add_conditional_edges(
        RISK,
        _route_after_risk,
        {"halt": HALT, "execute": EXECUTION, "end": END},
    )
    graph.add_edge(HALT,      END)
    graph.add_edge(EXECUTION, END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Synthesis node
# ---------------------------------------------------------------------------


async def _synthesise_node(state: GovTradeState) -> dict:  # type: ignore[type-arg]
    """Convert MaterialityResult objects into SentimentResult + NewsEvent.

    This bridges the gov-trade domain into the format SignalGeneratorAgent
    already understands without modifying that agent. Each scored award becomes:

    - One ``NewsEvent`` with ``event_type=OTHER``, ``source="usaspending"``,
      and ``tickers=[ticker]``, keyed by ``gov_contract_{award_id}``.
    - One ``SentimentResult`` referencing the same ``event_id``, with:
        - ``label``  derived from ``MaterialityDirection``
        - ``score``  = ±(adjusted_score / _SCORE_CEIL), clamped to [-1, +1]
        - ``confidence`` = min(1.0, adjusted_score / _SCORE_CEIL)

    NEUTRAL direction results in SentimentLabel.NEUTRAL (score=0). These are
    passed to SignalGeneratorAgent which will skip them in its NEUTRAL branch.
    """
    results: list[MaterialityResult] = state.get("materiality_results") or []

    news_events: list[NewsEvent] = []
    sentiment_results: list[SentimentResult] = []

    for r in results:
        event_id = f"gov_contract_{r.award_id}"
        normalised = min(1.0, r.adjusted_score / _SCORE_CEIL)

        label, score = _map_direction(r.direction, normalised)

        news_events.append(NewsEvent(
            event_id=event_id,
            headline=(
                f"Federal contract award: {r.ticker} "
                f"— {r.reasoning[:120] or r.award_id}"
            ),
            source="usaspending",
            tickers=[r.ticker],
            event_type=EventType.OTHER,
            published_at=r.scored_at,
        ))

        sentiment_results.append(SentimentResult(
            event_id=event_id,
            ticker=r.ticker,
            label=label,
            score=score,
            confidence=normalised,
            reasoning=r.reasoning,
            model_id=r.model_id or "heuristic",
            provider=r.provider,
        ))

    _logger.info(
        "Synthesis: converted %d materiality results → %d sentiment events",
        len(results),
        len(sentiment_results),
    )
    return {"news_events": news_events, "sentiment_results": sentiment_results}


def _map_direction(
    direction: MaterialityDirection,
    normalised_magnitude: float,
) -> tuple[SentimentLabel, float]:
    """Map MaterialityDirection + normalised magnitude to SentimentLabel + score."""
    match direction:
        case MaterialityDirection.BULLISH:
            return SentimentLabel.BULLISH, normalised_magnitude
        case MaterialityDirection.BEARISH:
            return SentimentLabel.BEARISH, -normalised_magnitude
        case _:
            return SentimentLabel.NEUTRAL, 0.0


# ---------------------------------------------------------------------------
# Router / gate functions
# ---------------------------------------------------------------------------


def _has_awards(state: GovTradeState) -> bool:
    return bool(state.get("contract_awards"))


def _has_resolutions(state: GovTradeState) -> bool:
    return bool(state.get("resolutions"))


def _has_materiality(state: GovTradeState) -> bool:
    return bool(state.get("materiality_results"))


def _route_after_risk(state: GovTradeState) -> str:
    if state.get("system_halted"):
        return "halt"
    if state.get("approved_signals"):
        return "execute"
    return "end"


# ---------------------------------------------------------------------------
# Factory helper (for main.py use)
# ---------------------------------------------------------------------------


def build_gov_pipeline_from_settings(
    settings: Settings,
    gov_settings: GovTradeSettings,
    event_bus: EventBus,
    stage1_repo: Stage1Repository,
    exec_agent: ExecutionAgent,
    halt_agent: HaltHandlerAgent,
    agency_mapping: dict[str, list[str]] | None = None,
) -> StateGraph:
    """Convenience factory that constructs all gov-trade agents from settings.

    Intended for use in ``main.py``. For tests, prefer ``build_gov_pipeline``
    directly with explicitly constructed mock agents.
    """
    from news_trade.providers.contracts.usaspending import USASpendingProvider
    from news_trade.providers.lobbying.lda_provider import LdaProvider

    lookup = ContractorLookup()
    resolver_svc = EntityResolutionService(
        lookup=lookup,
        edgar=None,
        llm=LLMClientFactory(settings) if gov_settings.gov_trade_llm_provider else None,
        redis_client=None,
        session=None,
        settings=gov_settings,
    )
    lobbying_provider = LdaProvider(api_key=gov_settings.lda_api_key)
    enrichment_svc = LobbyingEnrichmentService(
        provider=lobbying_provider,
        lookup=lookup,
        agency_mapping=agency_mapping or {},
        settings=gov_settings,
    )
    materiality_provider = HeuristicMaterialityProvider()

    contract_poller = ContractPollerAgent(
        settings=settings,
        event_bus=event_bus,
        gov_settings=gov_settings,
        provider=USASpendingProvider(gov_settings),
    )
    entity_resolver = EntityResolverAgent(
        settings=settings,
        event_bus=event_bus,
        gov_settings=gov_settings,
        resolver=resolver_svc,
    )
    materiality_scorer = MaterialityScorerAgent(
        settings=settings,
        event_bus=event_bus,
        gov_settings=gov_settings,
        provider=materiality_provider,
        enrichment=enrichment_svc,
    )

    return build_gov_pipeline(
        settings=settings,
        gov_settings=gov_settings,
        event_bus=event_bus,
        stage1_repo=stage1_repo,
        contract_poller=contract_poller,
        entity_resolver=entity_resolver,
        materiality_scorer=materiality_scorer,
        exec_agent=exec_agent,
        halt_agent=halt_agent,
    )
