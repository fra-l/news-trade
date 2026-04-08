"""LangGraph shared state definition for the trading pipeline.

The PipelineState TypedDict is passed through the LangGraph state graph.
Each agent node reads from and writes to specific keys.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from news_trade.models import (
    MarketSnapshot,
    NewsEvent,
    Order,
    PortfolioState,
    SentimentResult,
    TradeSignal,
)
from news_trade.models.surprise import EstimatesData


class PipelineState(TypedDict, total=False):
    """Shared state flowing through the LangGraph orchestration graph.

    Keys are populated progressively as each agent node executes.
    ``news_events`` and ``errors`` use ``operator.add`` reducers so parallel
    nodes can write to them concurrently without overwriting each other.
    """

    # Input — merged from NewsIngestorAgent + EarningsTickerNode via operator.add
    news_events: Annotated[list[NewsEvent], operator.add]

    # Active earnings tickers (next 1-7 days) produced by EarningsTickerNode
    active_tickers: list[str]

    # After EarningsCalendarAgent — ticker → pre-announcement consensus estimates
    estimates: dict[str, EstimatesData]

    # After MarketDataAgent enrichment
    market_context: dict[str, MarketSnapshot]  # ticker -> OHLCV / volatility snapshot

    # After SentimentAnalystAgent
    sentiment_results: list[SentimentResult]

    # After SignalGeneratorAgent
    trade_signals: list[TradeSignal]

    # After RiskManagerAgent
    approved_signals: list[TradeSignal]
    rejected_signals: list[TradeSignal]

    # After ExecutionAgent
    orders: list[Order]

    # Available throughout
    portfolio: PortfolioState

    # Control flow — operator.add reducer accumulates errors from parallel nodes
    errors: Annotated[list[str], operator.add]
    system_halted: bool  # set True by RiskManagerAgent when drawdown limit is breached
    replay_mode: bool  # set True by --replay-ticker; NewsIngestorAgent skips live fetch
