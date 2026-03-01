"""SQLAlchemy ORM table definitions for trade logging and signal history.

Each table mirrors a Pydantic model from ``news_trade.models`` and is used
for persistent storage, auditing, and deduplication.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class NewsEventRow(Base):
    """Persisted news event — used for deduplication and audit trail."""

    __tablename__ = "news_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text, default="")
    tickers_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        doc="JSON-encoded list of ticker strings",
    )
    event_type: Mapped[str] = mapped_column(String(64), default="other")
    published_at: Mapped[datetime] = mapped_column(DateTime)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def tickers(self) -> list[str]:
        return json.loads(self.tickers_json)

    @tickers.setter
    def tickers(self, value: list[str]) -> None:
        self.tickers_json = json.dumps(value)


class TradeSignalRow(Base):
    """Persisted trade signal — logs every signal, approved or rejected."""

    __tablename__ = "trade_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    event_id: Mapped[str] = mapped_column(String(256), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    conviction: Mapped[float] = mapped_column(Float)
    suggested_qty: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    approved: Mapped[int] = mapped_column(
        Integer,
        default=0,
        doc="1 = approved by RiskManager, 0 = rejected",
    )
    rejection_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OrderRow(Base):
    """Persisted order — tracks the full lifecycle of every order placed."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True, index=True
    )
    signal_id: Mapped[str] = mapped_column(String(256), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    qty: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    filled_avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
