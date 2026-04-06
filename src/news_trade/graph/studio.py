"""LangGraph Studio entry point.

Exports a compiled ``graph`` for use with ``langgraph dev`` / LangGraph Studio.
A no-op EventBus stub is used so the graph starts without a live Redis connection.
TradingClient is initialised with .env credentials but Alpaca API calls will fail
gracefully at node execution time — execution/portfolio nodes log errors, all
other nodes (sentiment, signal, risk) work normally.
"""
from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from news_trade.config import get_settings
from news_trade.graph.pipeline import build_pipeline
from news_trade.services.database import create_tables
from news_trade.services.event_bus import EventBus


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
graph = build_pipeline(_settings, _make_stub_event_bus())
