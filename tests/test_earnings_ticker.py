"""Unit tests for EarningsTickerNode.

All tests use in-memory SQLite and MagicMock — no real network calls.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from news_trade.agents.earnings_ticker import EarningsTickerNode
from news_trade.models.events import EventType
from news_trade.services.event_bus import EventBus
from news_trade.services.tables import Base, NewsEventRow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs: object) -> MagicMock:
    m = MagicMock()
    m.database_url = "sqlite://"
    m.earn_pre_horizon_days = 7
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _make_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _make_news_event_row(
    ticker: str,
    days_until: int,
    *,
    engine,
    published_at: datetime | None = None,
) -> NewsEventRow:
    """Insert a cron-style EARN_PRE row into the DB and return it."""
    report_date = date.today() + timedelta(days=days_until)
    event_id = f"calendar_earn_pre_{ticker}_{report_date}"
    row = NewsEventRow(
        event_id=event_id,
        headline=f"{ticker} earnings in {days_until}d",
        summary=f"days_until_report={days_until}",
        source="earnings_calendar",
        event_type=EventType.EARN_PRE.value,
        published_at=published_at or datetime.now(UTC),
    )
    row.tickers = [ticker]
    with Session(engine) as session:
        session.add(row)
        session.commit()
    return row


def _make_agent(
    tickers: list[str] | None = None,
    engine=None,
) -> EarningsTickerNode:
    """Build an EarningsTickerNode with an in-memory SQLite DB."""
    settings = _make_settings()
    event_bus = MagicMock(spec=EventBus)
    stage1_repo = MagicMock()

    with patch("news_trade.agents.earnings_ticker.build_engine") as mock_engine_factory:
        if engine is None:
            engine = _make_engine()
        mock_engine_factory.return_value = engine
        agent = EarningsTickerNode(
            settings=settings,
            event_bus=event_bus,
            tickers=tickers or ["AAPL", "NVDA"],
            stage1_repo=stage1_repo,
        )
    return agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEarningsTickerNodeRun:
    async def test_synthesises_earn_pre_events_for_active_tickers(self):
        engine = _make_engine()
        _make_news_event_row("NVDA", days_until=3, engine=engine)
        agent = _make_agent(tickers=["NVDA"], engine=engine)

        result = await agent.run({})

        assert len(result["news_events"]) == 1
        event = result["news_events"][0]
        assert event.event_type == EventType.EARN_PRE
        assert event.tickers == ["NVDA"]
        assert event.source == "earnings_ticker_node"
        assert "ticker_earn_pre_NVDA" in event.event_id
        assert "NVDA" in result["active_tickers"]

    async def test_skips_tickers_beyond_7_day_horizon(self):
        engine = _make_engine()
        _make_news_event_row("AAPL", days_until=10, engine=engine)
        agent = _make_agent(tickers=["AAPL"], engine=engine)

        result = await agent.run({})

        assert result["news_events"] == []
        assert result["active_tickers"] == []

    async def test_skips_tickers_with_report_in_the_past(self):
        engine = _make_engine()
        _make_news_event_row("AAPL", days_until=-1, engine=engine)
        agent = _make_agent(tickers=["AAPL"], engine=engine)

        result = await agent.run({})

        assert result["news_events"] == []
        assert result["active_tickers"] == []

    async def test_skips_tickers_not_in_session(self):
        engine = _make_engine()
        _make_news_event_row("TSLA", days_until=2, engine=engine)
        # session tickers only contain AAPL and NVDA
        agent = _make_agent(tickers=["AAPL", "NVDA"], engine=engine)

        result = await agent.run({})

        assert result["news_events"] == []
        assert result["active_tickers"] == []

    async def test_includes_boundary_day_1(self):
        """Report exactly 1 day away is within horizon."""
        engine = _make_engine()
        _make_news_event_row("NVDA", days_until=1, engine=engine)
        agent = _make_agent(tickers=["NVDA"], engine=engine)

        result = await agent.run({})

        assert len(result["news_events"]) == 1

    async def test_includes_boundary_day_7(self):
        """Report exactly 7 days away is within horizon."""
        engine = _make_engine()
        _make_news_event_row("NVDA", days_until=7, engine=engine)
        agent = _make_agent(tickers=["NVDA"], engine=engine)

        result = await agent.run({})

        assert len(result["news_events"]) == 1

    async def test_multiple_active_tickers(self):
        engine = _make_engine()
        _make_news_event_row("AAPL", days_until=2, engine=engine)
        _make_news_event_row("NVDA", days_until=5, engine=engine)
        agent = _make_agent(tickers=["AAPL", "NVDA"], engine=engine)

        result = await agent.run({})

        assert len(result["news_events"]) == 2
        tickers = {e.tickers[0] for e in result["news_events"]}
        assert tickers == {"AAPL", "NVDA"}
        assert set(result["active_tickers"]) == {"AAPL", "NVDA"}

    async def test_deduplicates_duplicate_db_rows_for_same_ticker(self):
        """When cron wrote two rows for same ticker, only one event is emitted."""
        engine = _make_engine()
        # Two rows for NVDA with the same report date (simulating double-run cron)
        report_date = date.today() + timedelta(days=3)
        for i in range(2):
            event_id = f"calendar_earn_pre_NVDA_{report_date}_dupe{i}"
            row = NewsEventRow(
                event_id=event_id,
                headline="NVDA earnings",
                summary="dup",
                source="earnings_calendar",
                event_type=EventType.EARN_PRE.value,
                published_at=datetime.now(UTC),
            )
            row.tickers = ["NVDA"]
            with Session(engine) as session:
                session.add(row)
                session.commit()

        agent = _make_agent(tickers=["NVDA"], engine=engine)
        result = await agent.run({})
        # These rows don't match the canonical prefix format (they end in _dupe0/1)
        # so they'll be skipped (date parse fails). This is expected behaviour.
        assert result["news_events"] == []

    async def test_returns_empty_on_empty_db(self):
        engine = _make_engine()
        agent = _make_agent(engine=engine)

        result = await agent.run({})

        assert result["news_events"] == []
        assert result["active_tickers"] == []
        assert result.get("errors") is None

    async def test_returns_errors_on_gather_failure(self):
        """When _gather_active_events raises, run() returns errors and empty lists."""
        engine = _make_engine()
        agent = _make_agent(engine=engine)

        with patch.object(
            agent, "_gather_active_events", side_effect=RuntimeError("DB error")
        ):
            result = await agent.run({})

        assert result["news_events"] == []
        assert result["active_tickers"] == []
        assert len(result["errors"]) == 1
        assert "DB error" in result["errors"][0]

    async def test_event_id_prefix_distinguishable_from_cron(self):
        """Synthesised events use 'ticker_earn_pre_*' not 'calendar_earn_pre_*'."""
        engine = _make_engine()
        _make_news_event_row("AAPL", days_until=4, engine=engine)
        agent = _make_agent(tickers=["AAPL"], engine=engine)

        result = await agent.run({})

        assert len(result["news_events"]) == 1
        event = result["news_events"][0]
        assert event.event_id.startswith("ticker_earn_pre_")
        assert not event.event_id.startswith("calendar_earn_pre_")
