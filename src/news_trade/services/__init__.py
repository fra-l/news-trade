"""External service clients (Redis, SQLite, APIs)."""

from news_trade.services.database import build_session_factory, create_tables
from news_trade.services.tables import Base, NewsEventRow, OrderRow, TradeSignalRow

__all__ = [
    "Base",
    "NewsEventRow",
    "OrderRow",
    "TradeSignalRow",
    "build_session_factory",
    "create_tables",
]
