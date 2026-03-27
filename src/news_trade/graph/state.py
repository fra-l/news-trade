"""LangGraph shared state definition for the trading pipeline.

The PipelineState TypedDict is passed through the LangGraph state graph.
Each agent node reads from and writes to specific keys.
"""

from __future__ import annotations

from typing import TypedDict

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
    """

    # Input
    news_events: list[NewsEvent]

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

    # Control flow
    errors: list[str]
