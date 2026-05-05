"""LangGraph shared state definition for the gov-trade contract-award pipeline.

GovTradeState is a strict superset of the keys that the reused news-trade
agents (SignalGeneratorAgent, RiskManagerAgent, ExecutionAgent,
HaltHandlerAgent, MarketDataAgent) read and write, plus the gov-specific
keys produced by the three new agents.

operator.add reducers are applied to ``news_events`` and ``errors`` so that
if parallel nodes ever write to them, accumulation is safe.
"""

from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, TypedDict

from news_trade.models import (
    MarketSnapshot,
    NewsEvent,
    Order,
    PortfolioState,
    SentimentResult,
    TradeSignal,
)
from news_trade.models.contracts import (
    ContractAwardEvent,
    EntityResolution,
    MaterialityResult,
)
from news_trade.models.surprise import EstimatesData


class GovTradeState(TypedDict, total=False):
    """Shared state flowing through the gov-trade LangGraph pipeline.

    Keys are populated progressively as each agent executes. Keys whose
    agent writes them are listed below.

    ContractPollerAgent writes:
        contract_awards, last_poll_govtrade

    EntityResolverAgent writes:
        resolutions

    MaterialityScorerAgent writes:
        materiality_results

    _synthesise_node writes:
        news_events, sentiment_results, market_context (stub)

    MarketDataAgent writes:
        market_context (real OHLCV snapshots)

    SignalGeneratorAgent writes:
        trade_signals

    RiskManagerAgent writes:
        approved_signals, rejected_signals, system_halted

    ExecutionAgent writes:
        orders
    """

    # --- gov-trade specific keys ---
    contract_awards: list[ContractAwardEvent]
    last_poll_govtrade: datetime
    resolutions: list[EntityResolution]
    materiality_results: list[MaterialityResult]

    # --- synthesised for reused agents; news_events uses operator.add ---
    news_events: Annotated[list[NewsEvent], operator.add]
    sentiment_results: list[SentimentResult]

    # --- MarketDataAgent / SignalGeneratorAgent ---
    market_context: dict[str, MarketSnapshot]
    estimates: dict[str, EstimatesData]

    # --- SignalGeneratorAgent ---
    trade_signals: list[TradeSignal]

    # --- RiskManagerAgent ---
    approved_signals: list[TradeSignal]
    rejected_signals: list[TradeSignal]
    system_halted: bool

    # --- ExecutionAgent ---
    orders: list[Order]

    # --- Shared infrastructure ---
    portfolio: PortfolioState
    errors: Annotated[list[str], operator.add]

    # --- Keys expected by SignalGeneratorAgent but unused in gov pipeline ---
    active_tickers: list[str]
