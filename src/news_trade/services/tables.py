"""SQLAlchemy ORM table definitions for trade logging and signal history.

Each table mirrors a Pydantic model from ``news_trade.models`` and is used
for persistent storage, auditing, and deduplication.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import cast

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
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
        return cast(list[str], json.loads(self.tickers_json))

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


class OpenStage1PositionRow(Base):
    """Persisted Stage 1 pre-earnings position.

    The primary key is a UUID string (not an autoincrement integer) because
    ``OpenStage1Position.id`` is generated in application code before the row
    is inserted.  This allows the Pydantic model and the DB row to share the
    same identifier without a round-trip.
    """

    __tablename__ = "stage1_positions"

    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    size_pct: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expected_report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    fiscal_quarter: Mapped[str] = mapped_column(String(32), nullable=False)
    historical_beat_rate: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True, default="open"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class EarningsOutcomeRow(Base):
    """Recorded earnings outcome for Pattern D reflection loop.

    Appended by Stage1Repository.record_outcome() when a Stage 1 position
    resolves.  ``stage1_id`` is unique so that a double call to record_outcome()
    for the same position is a silent no-op (idempotent).
    """

    __tablename__ = "earnings_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    stage1_id: Mapped[str | None] = mapped_column(
        String(256),
        ForeignKey("stage1_positions.id"),
        nullable=True,
        unique=True,
        index=True,
    )
    final_status: Mapped[str] = mapped_column(String(16), nullable=False)
    eps_surprise_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_move_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class LobbyingFilingRow(Base):
    """Cached LDA lobbying filing — one row per company per quarter.

    Populated by ``LdaProvider`` and used as the data source for
    ``LobbyingEnrichmentService``.  ``filing_uuid`` is unique so that
    re-fetching the same quarter is a safe no-op.
    """

    __tablename__ = "lobbying_filings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    lda_client_name: Mapped[str] = mapped_column(Text, nullable=False)
    filing_uuid: Mapped[str] = mapped_column(
        String(256), unique=True, nullable=False, index=True
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    period: Mapped[str] = mapped_column(String(8), nullable=False)
    total_spend: Mapped[float] = mapped_column(Float, nullable=False)
    agency_breakdown_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="{}",
        doc="JSON dict: normalised agency name → proportional spend",
    )
    lobbied_agencies_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
        doc="JSON list of normalised agency names targeted",
    )
    raw_description: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class EnrichmentLogRow(Base):
    """Audit log for every lobbying enrichment call.

    One row per ``LobbyingEnrichmentService.get_multiplier()`` call.
    Used for calibration: after 6 months of paper trading, regress
    ``multiplier_applied`` against actual price outcomes.
    """

    __tablename__ = "enrichment_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_award_id: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        index=True,
        default="",
        doc="References ContractAwardEvent.award_id",
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    agency: Mapped[str] = mapped_column(Text, nullable=False)
    multiplier_applied: Mapped[float] = mapped_column(Float, nullable=False)
    data_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spend_delta_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class EntityResolutionRow(Base):
    """Audit log for every entity-resolution attempt.

    One row per ``EntityResolutionService.resolve()`` call. Used to measure
    per-layer accuracy and tune the resolution pipeline over time.
    """

    __tablename__ = "entity_resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    award_id: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        index=True,
        default="",
        doc=(
            "References ContractAwardEvent.award_id; "
            "empty when called outside the pipeline"
        ),
    )
    recipient_name: Mapped[str] = mapped_column(Text, nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    exchange: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    layer: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="static | edgar | llm | none",
    )
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class ContractAwardRow(Base):
    """Persisted USASpending contract award — deduplication and audit trail."""

    __tablename__ = "contract_awards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    award_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    recipient_name: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_uei: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recipient_parent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    awarding_agency: Mapped[str] = mapped_column(Text, nullable=False)
    awarding_sub_agency: Mapped[str | None] = mapped_column(Text, nullable=True)
    award_type: Mapped[str] = mapped_column(String(8), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    naics_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    naics_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    sign_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    last_modified_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    place_of_performance_state: Mapped[str | None] = mapped_column(
        String(4), nullable=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class GovTradeSignalRow(Base):
    """Persisted gov-trade signal — linked to contract awards for clean attribution."""

    __tablename__ = "gov_trade_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    award_id: Mapped[str] = mapped_column(String(256), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    conviction: Mapped[float] = mapped_column(Float, nullable=False)
    suggested_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    materiality_score: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        doc="Adjusted materiality score that drove this signal",
    )
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
    close_after_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
        doc="Auto-close date for PEAD positions; None for non-PEAD orders",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


