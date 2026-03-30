"""Tests for EarningsCalendarAgent, EarningsCalendarEntry, and calendar providers.

All tests use in-memory SQLite and AsyncMock — no real network calls.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from news_trade.agents.earnings_calendar import (
    EarningsCalendarAgent,
    _build_estimates,
    _make_event_id,
    _synthesise_event,
)
from news_trade.config import Settings
from news_trade.models.calendar import EarningsCalendarEntry, ReportTiming
from news_trade.models.events import EventType
from news_trade.services.tables import Base, NewsEventRow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs: object) -> Settings:
    defaults: dict[str, object] = dict(
        anthropic_api_key="test",
        watchlist=["AAPL", "MSFT"],
    )
    return Settings(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_entry(**kwargs: object) -> EarningsCalendarEntry:
    defaults: dict[str, object] = dict(
        ticker="AAPL",
        report_date=date.today() + timedelta(days=3),
        fiscal_quarter="Q2 2026",
        fiscal_year=2026,
        timing=ReportTiming.PRE_MARKET,
        eps_estimate=1.55,
    )
    return EarningsCalendarEntry(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _make_agent(
    primary: object | None = None,
    fallback: object | None = None,
    engine=None,
    settings: Settings | None = None,
) -> EarningsCalendarAgent:
    if primary is None:
        primary = AsyncMock()
        primary.name = "mock_primary"
        primary.get_upcoming_earnings = AsyncMock(return_value=[])
    if fallback is None:
        fallback = AsyncMock()
        fallback.name = "mock_fallback"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[])
    if engine is None:
        engine = _make_engine()
    if settings is None:
        settings = _make_settings()
    event_bus = AsyncMock()
    event_bus.publish = AsyncMock()
    agent = EarningsCalendarAgent(settings, event_bus, primary, fallback, engine)
    agent._event_bus = event_bus  # expose for assertions
    return agent


# ---------------------------------------------------------------------------
# TestEarningsCalendarEntry
# ---------------------------------------------------------------------------


class TestEarningsCalendarEntry:
    def test_is_actionable_at_2_days(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=2))
        assert entry.is_actionable is True

    def test_is_actionable_at_5_days(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=5))
        assert entry.is_actionable is True

    def test_not_actionable_at_1_day(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=1))
        assert entry.is_actionable is False

    def test_not_actionable_at_6_days(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=6))
        assert entry.is_actionable is False

    def test_not_actionable_in_past(self) -> None:
        entry = _make_entry(report_date=date.today() - timedelta(days=1))
        assert entry.is_actionable is False

    def test_days_until_report(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=4))
        assert entry.days_until_report == 4

    def test_days_until_report_negative_when_past(self) -> None:
        entry = _make_entry(report_date=date.today() - timedelta(days=2))
        assert entry.days_until_report == -2

    def test_serialization_round_trip(self) -> None:
        entry = _make_entry()
        restored = EarningsCalendarEntry.model_validate(entry.model_dump())
        assert restored.ticker == entry.ticker
        assert restored.report_date == entry.report_date

    def test_optional_eps_estimate(self) -> None:
        entry = _make_entry(eps_estimate=None)
        assert entry.eps_estimate is None

    def test_default_timing_unknown(self) -> None:
        entry = EarningsCalendarEntry(
            ticker="AAPL",
            report_date=date.today() + timedelta(days=3),
            fiscal_quarter="Q2 2026",
            fiscal_year=2026,
        )
        assert entry.timing == ReportTiming.UNKNOWN

    def test_is_candidate_at_1_day(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=1))
        assert entry.is_candidate is True

    def test_is_candidate_at_31_days(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=31))
        assert entry.is_candidate is True

    def test_not_candidate_at_0_days(self) -> None:
        entry = _make_entry(report_date=date.today())
        assert entry.is_candidate is False

    def test_not_candidate_at_32_days(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=32))
        assert entry.is_candidate is False

    def test_not_candidate_in_past(self) -> None:
        entry = _make_entry(report_date=date.today() - timedelta(days=1))
        assert entry.is_candidate is False

    def test_candidate_includes_actionable_window(self) -> None:
        """is_candidate is a superset of is_actionable (1-31 vs 2-5 days)."""
        for days in range(2, 6):
            entry = _make_entry(report_date=date.today() + timedelta(days=days))
            assert entry.is_actionable is True
            assert entry.is_candidate is True


# ---------------------------------------------------------------------------
# TestSynthesiseEvent
# ---------------------------------------------------------------------------


class TestSynthesiseEvent:
    def test_event_type_is_earn_pre(self) -> None:
        entry = _make_entry()
        event = _synthesise_event(entry)
        assert event.event_type == EventType.EARN_PRE

    def test_event_id_format(self) -> None:
        entry = _make_entry(ticker="AAPL", report_date=date(2026, 4, 25))
        event = _synthesise_event(entry)
        assert event.event_id == "calendar_earn_pre_AAPL_2026-04-25"

    def test_source_is_earnings_calendar(self) -> None:
        entry = _make_entry()
        event = _synthesise_event(entry)
        assert event.source == "earnings_calendar"

    def test_tickers_contains_ticker(self) -> None:
        entry = _make_entry(ticker="MSFT")
        event = _synthesise_event(entry)
        assert "MSFT" in event.tickers

    def test_headline_contains_quarter_and_date(self) -> None:
        entry = _make_entry(
            ticker="AAPL", fiscal_quarter="Q2 2026", report_date=date(2026, 4, 25)
        )
        event = _synthesise_event(entry)
        assert "Q2 2026" in event.headline
        assert "2026-04-25" in event.headline

    def test_summary_contains_eps_estimate(self) -> None:
        entry = _make_entry(eps_estimate=1.55)
        event = _synthesise_event(entry)
        assert "1.55" in event.summary


# ---------------------------------------------------------------------------
# TestEarningsCalendarAgentHappyPath
# ---------------------------------------------------------------------------


class TestEarningsCalendarAgentHappyPath:
    def setup_method(self) -> None:
        self.engine = _make_engine()
        self.settings = _make_settings()
        _3d = date.today() + timedelta(days=3)
        _4d = date.today() + timedelta(days=4)
        self.entry_aapl = _make_entry(ticker="AAPL", report_date=_3d)
        self.entry_msft = _make_entry(ticker="MSFT", report_date=_4d)

        self.primary = AsyncMock()
        self.primary.name = "fmp_calendar"
        self.primary.get_upcoming_earnings = AsyncMock(
            return_value=[self.entry_aapl, self.entry_msft]
        )
        self.fallback = AsyncMock()
        self.fallback.name = "yfinance_calendar"
        self.fallback.get_upcoming_earnings = AsyncMock(return_value=[])
        self.event_bus = AsyncMock()
        self.event_bus.publish = AsyncMock()

        self.agent = EarningsCalendarAgent(
            self.settings, self.event_bus, self.primary, self.fallback, self.engine
        )

    async def test_returns_two_events(self) -> None:
        result = await self.agent.run({})
        assert len(result["news_events"]) == 2

    async def test_events_are_earn_pre(self) -> None:
        result = await self.agent.run({})
        for event in result["news_events"]:
            assert event.event_type == EventType.EARN_PRE

    async def test_events_persisted_to_sqlite(self) -> None:
        await self.agent.run({})
        with Session(self.engine) as s:
            rows = s.query(NewsEventRow).all()
        assert len(rows) == 2

    async def test_events_published_to_bus(self) -> None:
        await self.agent.run({})
        assert self.event_bus.publish.call_count == 2

    async def test_fallback_not_called_when_primary_succeeds(self) -> None:
        await self.agent.run({})
        self.fallback.get_upcoming_earnings.assert_not_called()

    async def test_no_errors_returned(self) -> None:
        result = await self.agent.run({})
        assert result["errors"] == []

    async def test_errors_from_state_preserved(self) -> None:
        result = await self.agent.run({"errors": ["prior error"]})
        assert "prior error" in result["errors"]


# ---------------------------------------------------------------------------
# TestEarningsCalendarAgentDedup
# ---------------------------------------------------------------------------


class TestEarningsCalendarAgentDedup:
    def setup_method(self) -> None:
        self.engine = _make_engine()
        self.settings = _make_settings()
        _3d = date.today() + timedelta(days=3)
        self.entry = _make_entry(ticker="AAPL", report_date=_3d)

        # Pre-seed the database with the same event_id
        event_id = _make_event_id(self.entry)
        with Session(self.engine) as s:
            s.add(NewsEventRow(
                event_id=event_id,
                headline="pre-existing",
                summary="",
                source="earnings_calendar",
                url="",
                event_type=EventType.EARN_PRE,
                published_at=datetime.utcnow(),
            ))
            s.commit()

        primary = AsyncMock()
        primary.name = "fmp_calendar"
        primary.get_upcoming_earnings = AsyncMock(return_value=[self.entry])
        fallback = AsyncMock()
        fallback.name = "yfinance_calendar"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[])
        self.event_bus = AsyncMock()
        self.event_bus.publish = AsyncMock()
        self.agent = EarningsCalendarAgent(
            self.settings, self.event_bus, primary, fallback, self.engine
        )

    async def test_duplicate_skipped(self) -> None:
        result = await self.agent.run({})
        assert result["news_events"] == []

    async def test_nothing_published_for_duplicate(self) -> None:
        await self.agent.run({})
        self.event_bus.publish.assert_not_called()

    async def test_sqlite_row_count_unchanged(self) -> None:
        await self.agent.run({})
        with Session(self.engine) as s:
            count = s.query(NewsEventRow).count()
        assert count == 1


# ---------------------------------------------------------------------------
# TestEarningsCalendarAgentFallback
# ---------------------------------------------------------------------------


class TestEarningsCalendarAgentFallback:
    def setup_method(self) -> None:
        self.engine = _make_engine()
        self.settings = _make_settings()
        _2d = date.today() + timedelta(days=2)
        self.fallback_entry = _make_entry(ticker="MSFT", report_date=_2d)
        self.event_bus = AsyncMock()
        self.event_bus.publish = AsyncMock()

    async def test_fallback_called_when_primary_empty(self) -> None:
        primary = AsyncMock()
        primary.name = "fmp_calendar"
        primary.get_upcoming_earnings = AsyncMock(return_value=[])
        fallback = AsyncMock()
        fallback.name = "yfinance_calendar"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[self.fallback_entry])

        agent = EarningsCalendarAgent(
            self.settings, self.event_bus, primary, fallback, self.engine
        )
        result = await agent.run({})
        assert len(result["news_events"]) == 1
        fallback.get_upcoming_earnings.assert_called_once()

    async def test_fallback_called_when_primary_raises(self) -> None:
        primary = AsyncMock()
        primary.name = "fmp_calendar"
        primary.get_upcoming_earnings = AsyncMock(side_effect=RuntimeError("FMP down"))
        fallback = AsyncMock()
        fallback.name = "yfinance_calendar"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[self.fallback_entry])

        agent = EarningsCalendarAgent(
            self.settings, self.event_bus, primary, fallback, self.engine
        )
        result = await agent.run({})
        assert len(result["news_events"]) == 1
        fallback.get_upcoming_earnings.assert_called_once()

    async def test_both_providers_fail_returns_empty(self) -> None:
        primary = AsyncMock()
        primary.name = "fmp_calendar"
        primary.get_upcoming_earnings = AsyncMock(side_effect=RuntimeError("FMP down"))
        fallback = AsyncMock()
        fallback.name = "yfinance_calendar"
        fallback.get_upcoming_earnings = AsyncMock(
            side_effect=RuntimeError("yfinance down")
        )

        agent = EarningsCalendarAgent(
            self.settings, self.event_bus, primary, fallback, self.engine
        )
        result = await agent.run({})
        assert result["news_events"] == []


# ---------------------------------------------------------------------------
# TestEarningsCalendarAgentWindowFiltering
# ---------------------------------------------------------------------------


class TestEarningsCalendarAgentWindowFiltering:
    def setup_method(self) -> None:
        self.engine = _make_engine()
        self.settings = _make_settings()
        self.event_bus = AsyncMock()
        self.event_bus.publish = AsyncMock()

    async def test_entry_at_1_day_not_emitted(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=1))
        primary = AsyncMock()
        primary.name = "fmp_calendar"
        primary.get_upcoming_earnings = AsyncMock(return_value=[entry])
        fallback = AsyncMock()
        fallback.name = "yfinance_calendar"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[])

        agent = EarningsCalendarAgent(
            self.settings, self.event_bus, primary, fallback, self.engine
        )
        result = await agent.run({})
        assert result["news_events"] == []
        self.event_bus.publish.assert_not_called()

    async def test_entry_at_6_days_not_emitted(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=6))
        primary = AsyncMock()
        primary.name = "fmp_calendar"
        primary.get_upcoming_earnings = AsyncMock(return_value=[entry])
        fallback = AsyncMock()
        fallback.name = "yfinance_calendar"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[])

        agent = EarningsCalendarAgent(
            self.settings, self.event_bus, primary, fallback, self.engine
        )
        result = await agent.run({})
        assert result["news_events"] == []


# ---------------------------------------------------------------------------
# TestBuildEstimates
# ---------------------------------------------------------------------------


class TestBuildEstimates:
    """Unit tests for the _build_estimates() module-level helper."""

    def test_returns_none_when_no_eps_estimate(self) -> None:
        entry = _make_entry(eps_estimate=None)
        assert _build_estimates(entry) is None

    def test_returns_estimates_data_when_eps_available(self) -> None:
        from news_trade.models.surprise import EstimatesData

        entry = _make_entry(ticker="AAPL", eps_estimate=2.50)
        result = _build_estimates(entry)
        assert isinstance(result, EstimatesData)

    def test_eps_estimate_value_preserved(self) -> None:
        entry = _make_entry(eps_estimate=3.14)
        result = _build_estimates(entry)
        assert result is not None
        assert result.eps_estimate == 3.14

    def test_eps_low_and_high_equal_estimate(self) -> None:
        entry = _make_entry(eps_estimate=1.80)
        result = _build_estimates(entry)
        assert result is not None
        assert result.eps_low == 1.80
        assert result.eps_high == 1.80

    def test_fiscal_period_from_fiscal_quarter(self) -> None:
        entry = _make_entry(fiscal_quarter="Q2 2026")
        result = _build_estimates(entry)
        assert result is not None
        assert result.fiscal_period == "Q2 2026"

    def test_report_date_preserved(self) -> None:
        rd = date.today() + timedelta(days=3)
        entry = _make_entry(report_date=rd)
        result = _build_estimates(entry)
        assert result is not None
        assert result.report_date == rd

    def test_optional_fields_are_none(self) -> None:
        entry = _make_entry(eps_estimate=1.00)
        result = _build_estimates(entry)
        assert result is not None
        assert result.historical_beat_rate is None
        assert result.mean_eps_surprise is None
        assert result.eps_trailing_mean is None


# ---------------------------------------------------------------------------
# TestEarningsCalendarAgentEstimatesState
# ---------------------------------------------------------------------------


class TestEarningsCalendarAgentEstimatesState:
    """Verify EarningsCalendarAgent populates estimates in returned state."""

    def setup_method(self) -> None:
        self.engine = _make_engine()
        self.settings = _make_settings()
        self.event_bus = AsyncMock()
        self.event_bus.publish = AsyncMock()

    async def test_estimates_populated_for_entries_with_eps(self) -> None:
        entry = _make_entry(ticker="AAPL", eps_estimate=2.50)
        primary = AsyncMock()
        primary.name = "fmp_calendar"
        primary.get_upcoming_earnings = AsyncMock(return_value=[entry])
        fallback = AsyncMock()
        fallback.name = "yfinance_calendar"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[])

        agent = EarningsCalendarAgent(
            self.settings, self.event_bus, primary, fallback, self.engine
        )
        result = await agent.run({})
        assert "AAPL" in result["estimates"]
        assert result["estimates"]["AAPL"].eps_estimate == 2.50

    async def test_estimates_empty_for_entries_without_eps(self) -> None:
        entry = _make_entry(ticker="AAPL", eps_estimate=None)
        primary = AsyncMock()
        primary.name = "fmp_calendar"
        primary.get_upcoming_earnings = AsyncMock(return_value=[entry])
        fallback = AsyncMock()
        fallback.name = "yfinance_calendar"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[])

        agent = EarningsCalendarAgent(
            self.settings, self.event_bus, primary, fallback, self.engine
        )
        result = await agent.run({})
        assert result["estimates"] == {}

    async def test_estimates_populated_even_when_event_is_duplicate(self) -> None:
        """Estimates in state even if the EARN_PRE event was already ingested."""
        from news_trade.models.events import EventType

        entry = _make_entry(ticker="AAPL", eps_estimate=1.55)
        event_id = _make_event_id(entry)

        # Pre-seed the database so the event is a duplicate
        with Session(self.engine) as s:
            from news_trade.services.tables import NewsEventRow

            s.add(NewsEventRow(
                event_id=event_id,
                headline="pre-existing",
                summary="",
                source="earnings_calendar",
                url="",
                event_type=EventType.EARN_PRE,
                published_at=date.today(),
            ))
            s.commit()

        primary = AsyncMock()
        primary.name = "fmp_calendar"
        primary.get_upcoming_earnings = AsyncMock(return_value=[entry])
        fallback = AsyncMock()
        fallback.name = "yfinance_calendar"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[])

        agent = EarningsCalendarAgent(
            self.settings, self.event_bus, primary, fallback, self.engine
        )
        result = await agent.run({})
        # Event is deduped (not published), but estimates should still be available
        assert result["news_events"] == []
        assert "AAPL" in result["estimates"]
        assert result["estimates"]["AAPL"].eps_estimate == 1.55


# ---------------------------------------------------------------------------
# TestEarningsCalendarAgentEstimatesProvider
# ---------------------------------------------------------------------------


class TestEarningsCalendarAgentEstimatesProvider:
    """Verify that estimates_provider populates historical_beat_rate."""

    def setup_method(self) -> None:
        self.engine = _make_engine()
        self.settings = _make_settings()
        self.event_bus = AsyncMock()
        self.event_bus.publish = AsyncMock()

    def _make_primary(self, entries: list) -> AsyncMock:
        primary = AsyncMock()
        primary.name = "fmp_calendar"
        primary.get_upcoming_earnings = AsyncMock(return_value=entries)
        return primary

    def _make_fallback(self) -> AsyncMock:
        fallback = AsyncMock()
        fallback.name = "yfinance_calendar"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[])
        return fallback

    async def test_beat_rate_populated_when_provider_returns_value(self) -> None:
        entry = _make_entry(ticker="AAPL", eps_estimate=2.50)
        estimates_provider = AsyncMock()
        estimates_provider.get_historical_beat_rate = AsyncMock(return_value=0.72)

        agent = EarningsCalendarAgent(
            self.settings,
            self.event_bus,
            self._make_primary([entry]),
            self._make_fallback(),
            self.engine,
            estimates_provider=estimates_provider,
        )
        result = await agent.run({})
        assert result["estimates"]["AAPL"].historical_beat_rate == pytest.approx(0.72)

    async def test_beat_rate_none_when_no_estimates_provider(self) -> None:
        entry = _make_entry(ticker="AAPL", eps_estimate=2.50)

        agent = EarningsCalendarAgent(
            self.settings,
            self.event_bus,
            self._make_primary([entry]),
            self._make_fallback(),
            self.engine,
            estimates_provider=None,
        )
        result = await agent.run({})
        assert result["estimates"]["AAPL"].historical_beat_rate is None

    async def test_beat_rate_none_when_provider_returns_none(self) -> None:
        entry = _make_entry(ticker="AAPL", eps_estimate=2.50)
        estimates_provider = AsyncMock()
        estimates_provider.get_historical_beat_rate = AsyncMock(return_value=None)

        agent = EarningsCalendarAgent(
            self.settings,
            self.event_bus,
            self._make_primary([entry]),
            self._make_fallback(),
            self.engine,
            estimates_provider=estimates_provider,
        )
        result = await agent.run({})
        assert result["estimates"]["AAPL"].historical_beat_rate is None

    async def test_estimates_provider_exception_is_swallowed(self) -> None:
        """A failing EstimatesProvider should not abort the run."""
        entry = _make_entry(ticker="AAPL", eps_estimate=2.50)
        estimates_provider = AsyncMock()
        estimates_provider.get_historical_beat_rate = AsyncMock(
            side_effect=RuntimeError("FMP quota exceeded")
        )

        agent = EarningsCalendarAgent(
            self.settings,
            self.event_bus,
            self._make_primary([entry]),
            self._make_fallback(),
            self.engine,
            estimates_provider=estimates_provider,
        )
        # Must not raise
        result = await agent.run({})
        assert "AAPL" in result["estimates"]
        assert result["estimates"]["AAPL"].historical_beat_rate is None

    async def test_provider_called_once_per_ticker(self) -> None:
        entry = _make_entry(ticker="AAPL", eps_estimate=2.50)
        estimates_provider = AsyncMock()
        estimates_provider.get_historical_beat_rate = AsyncMock(return_value=0.68)

        agent = EarningsCalendarAgent(
            self.settings,
            self.event_bus,
            self._make_primary([entry]),
            self._make_fallback(),
            self.engine,
            estimates_provider=estimates_provider,
        )
        await agent.run({})
        estimates_provider.get_historical_beat_rate.assert_called_once_with("AAPL")
