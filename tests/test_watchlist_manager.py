"""Unit tests for WatchlistManager.

All tests use in-memory SQLite and AsyncMock — no real network calls.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.models.calendar import EarningsCalendarEntry, ReportTiming
from news_trade.services.tables import Base, WatchlistSelectionRow
from news_trade.services.watchlist_manager import WatchlistManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(watchlist: list[str] | None = None) -> MagicMock:
    settings = MagicMock()
    settings.watchlist = watchlist or ["AAPL", "MSFT", "GOOGL"]
    return settings


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_entry(**kwargs: object) -> EarningsCalendarEntry:
    defaults: dict[str, object] = dict(
        ticker="AAPL",
        report_date=date.today() + timedelta(days=10),
        fiscal_quarter="Q2 2026",
        fiscal_year=2026,
        timing=ReportTiming.POST_MARKET,
        eps_estimate=1.55,
    )
    return EarningsCalendarEntry(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_manager(
    settings: MagicMock | None = None,
    session: Session | None = None,
    primary: object | None = None,
    fallback: object | None = None,
) -> WatchlistManager:
    if settings is None:
        settings = _make_settings()
    if session is None:
        session = _make_session()
    if primary is None:
        primary = AsyncMock()
        primary.name = "mock_primary"
        primary.get_upcoming_earnings = AsyncMock(return_value=[])
    if fallback is None:
        fallback = AsyncMock()
        fallback.name = "mock_fallback"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[])
    return WatchlistManager(
        settings=settings,  # type: ignore[arg-type]
        session=session,
        primary=primary,  # type: ignore[arg-type]
        fallback=fallback,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# TestGetActiveWatchlist
# ---------------------------------------------------------------------------


class TestGetActiveWatchlist:
    def setup_method(self) -> None:
        self.session = _make_session()
        self.settings = _make_settings(watchlist=["AAPL", "MSFT"])
        self.manager = _make_manager(settings=self.settings, session=self.session)

    def test_returns_static_watchlist_when_no_db_entry(self) -> None:
        result = self.manager.get_active_watchlist()
        assert result == ["AAPL", "MSFT"]

    def test_returns_saved_tickers_when_selection_exists(self) -> None:
        self.manager.save_selection(["NVDA", "META"])
        result = self.manager.get_active_watchlist()
        assert result == ["NVDA", "META"]

    def test_saved_selection_overrides_static_watchlist(self) -> None:
        self.manager.save_selection(["TSLA"])
        result = self.manager.get_active_watchlist()
        assert "AAPL" not in result
        assert result == ["TSLA"]

    def test_returns_most_recent_row_when_multiple_exist(self) -> None:
        self.manager.save_selection(["AAPL"])
        self.manager.save_selection(["MSFT", "NVDA"])
        result = self.manager.get_active_watchlist()
        assert result == ["MSFT", "NVDA"]


# ---------------------------------------------------------------------------
# TestSaveAndLoad
# ---------------------------------------------------------------------------


class TestSaveAndLoad:
    def setup_method(self) -> None:
        self.session = _make_session()
        self.manager = _make_manager(session=self.session)

    def test_load_selected_empty_before_any_save(self) -> None:
        assert self.manager.load_selected() == []

    def test_save_and_load_round_trip(self) -> None:
        self.manager.save_selection(["AAPL", "TSLA"])
        assert self.manager.load_selected() == ["AAPL", "TSLA"]

    def test_saving_again_appends_new_row(self) -> None:
        self.manager.save_selection(["AAPL"])
        self.manager.save_selection(["MSFT"])
        # Both rows should exist in the DB
        rows = self.session.query(WatchlistSelectionRow).all()
        assert len(rows) == 2

    def test_load_returns_most_recent_after_multiple_saves(self) -> None:
        self.manager.save_selection(["AAPL"])
        self.manager.save_selection(["MSFT"])
        assert self.manager.load_selected() == ["MSFT"]

    def test_save_preserves_order(self) -> None:
        tickers = ["NVDA", "AAPL", "META", "TSLA"]
        self.manager.save_selection(tickers)
        assert self.manager.load_selected() == tickers

    def test_save_empty_list(self) -> None:
        self.manager.save_selection([])
        # Empty list is saved; load_selected returns [] which is falsy,
        # so get_active_watchlist falls back to settings.watchlist.
        assert self.manager.load_selected() == []


# ---------------------------------------------------------------------------
# TestScanCandidates
# ---------------------------------------------------------------------------


class TestScanCandidates:
    def setup_method(self) -> None:
        self.settings = _make_settings(watchlist=["AAPL", "MSFT"])
        self.session = _make_session()

    async def test_returns_candidate_entries(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=15))
        primary = AsyncMock()
        primary.name = "mock_primary"
        primary.get_upcoming_earnings = AsyncMock(return_value=[entry])
        manager = _make_manager(
            settings=self.settings, session=self.session, primary=primary
        )
        from_dt = date.today()
        to_dt = from_dt + timedelta(days=30)
        result = await manager.scan_candidates(from_dt, to_dt)
        assert len(result) == 1
        assert result[0].ticker == "AAPL"

    async def test_filters_out_non_candidate_entries(self) -> None:
        """Entries with days_until_report=0 or >31 are not candidates."""
        today_entry = _make_entry(report_date=date.today())  # 0 days
        far_entry = _make_entry(
            ticker="MSFT",
            report_date=date.today() + timedelta(days=35),  # 35 days
        )
        primary = AsyncMock()
        primary.name = "mock_primary"
        primary.get_upcoming_earnings = AsyncMock(return_value=[today_entry, far_entry])
        manager = _make_manager(
            settings=self.settings, session=self.session, primary=primary
        )
        from_dt = date.today()
        to_dt = from_dt + timedelta(days=30)
        result = await manager.scan_candidates(from_dt, to_dt)
        assert result == []

    async def test_falls_back_to_fallback_when_primary_empty(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=10))
        primary = AsyncMock()
        primary.name = "mock_primary"
        primary.get_upcoming_earnings = AsyncMock(return_value=[])
        fallback = AsyncMock()
        fallback.name = "mock_fallback"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[entry])
        manager = _make_manager(
            settings=self.settings,
            session=self.session,
            primary=primary,
            fallback=fallback,
        )
        from_dt = date.today()
        to_dt = from_dt + timedelta(days=30)
        result = await manager.scan_candidates(from_dt, to_dt)
        assert len(result) == 1

    async def test_falls_back_when_primary_raises(self) -> None:
        entry = _make_entry(report_date=date.today() + timedelta(days=10))
        primary = AsyncMock()
        primary.name = "mock_primary"
        primary.get_upcoming_earnings = AsyncMock(side_effect=RuntimeError("API down"))
        fallback = AsyncMock()
        fallback.name = "mock_fallback"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[entry])
        manager = _make_manager(
            settings=self.settings,
            session=self.session,
            primary=primary,
            fallback=fallback,
        )
        from_dt = date.today()
        to_dt = from_dt + timedelta(days=30)
        result = await manager.scan_candidates(from_dt, to_dt)
        assert len(result) == 1

    async def test_returns_empty_when_both_providers_fail(self) -> None:
        primary = AsyncMock()
        primary.name = "mock_primary"
        primary.get_upcoming_earnings = AsyncMock(side_effect=RuntimeError("API down"))
        fallback = AsyncMock()
        fallback.name = "mock_fallback"
        fallback.get_upcoming_earnings = AsyncMock(
            side_effect=RuntimeError("also down")
        )
        manager = _make_manager(
            settings=self.settings,
            session=self.session,
            primary=primary,
            fallback=fallback,
        )
        from_dt = date.today()
        to_dt = from_dt + timedelta(days=30)
        result = await manager.scan_candidates(from_dt, to_dt)
        assert result == []

    async def test_results_sorted_by_report_date(self) -> None:
        entries = [
            _make_entry(ticker="MSFT", report_date=date.today() + timedelta(days=20)),
            _make_entry(ticker="AAPL", report_date=date.today() + timedelta(days=5)),
            _make_entry(ticker="NVDA", report_date=date.today() + timedelta(days=12)),
        ]
        primary = AsyncMock()
        primary.name = "mock_primary"
        primary.get_upcoming_earnings = AsyncMock(return_value=entries)
        manager = _make_manager(
            settings=self.settings, session=self.session, primary=primary
        )
        from_dt = date.today()
        to_dt = from_dt + timedelta(days=30)
        result = await manager.scan_candidates(from_dt, to_dt)
        dates = [e.report_date for e in result]
        assert dates == sorted(dates)

    async def test_broad_scan_passes_empty_tickers_to_provider(self) -> None:
        # scan_candidates performs a broad scan by passing [] so FMP returns all
        # companies in the window (not just the current watchlist).
        primary = AsyncMock()
        primary.name = "mock_primary"
        primary.get_upcoming_earnings = AsyncMock(return_value=[])
        manager = _make_manager(
            settings=self.settings, session=self.session, primary=primary
        )
        await manager.scan_candidates(date.today(), date.today() + timedelta(days=30))
        call_args = primary.get_upcoming_earnings.call_args
        assert call_args[0][0] == []

    async def test_falls_back_to_watchlist_on_broad_scan_error(self) -> None:
        # When FMPBroadScanError is raised (free-tier 403), scan_candidates retries
        # with the static watchlist tickers.
        from news_trade.providers.calendar.fmp import FMPBroadScanError

        primary = AsyncMock()
        primary.name = "mock_primary"
        # First call (broad scan) raises; second call (watchlist tickers) returns []
        primary.get_upcoming_earnings = AsyncMock(
            side_effect=[FMPBroadScanError("403 free tier"), []]
        )
        manager = _make_manager(
            settings=self.settings,
            session=self.session,
            primary=primary,
        )
        await manager.scan_candidates(date.today(), date.today() + timedelta(days=30))
        assert primary.get_upcoming_earnings.call_count == 2
        second_call_tickers = primary.get_upcoming_earnings.call_args_list[1][0][0]
        assert set(second_call_tickers) == {"AAPL", "MSFT"}


# ---------------------------------------------------------------------------
# TestWatchlistManagerAgentInjection
# ---------------------------------------------------------------------------


class TestWatchlistManagerAgentInjection:
    """Verify that agents use WatchlistManager.get_active_watchlist() instead of
    accessing settings.watchlist directly."""

    async def test_news_ingestor_uses_watchlist_manager(self) -> None:
        from unittest.mock import patch

        from sqlalchemy import create_engine

        from news_trade.agents.news_ingestor import NewsIngestorAgent

        settings = MagicMock()
        settings.watchlist = ["STATIC"]
        settings.database_url = "sqlite://"
        event_bus = AsyncMock()
        mock_provider = AsyncMock()
        mock_provider.name = "mock"
        mock_provider.fetch = AsyncMock(return_value=[])

        wlm = MagicMock()
        wlm.get_active_watchlist.return_value = ["DYNAMIC"]

        with patch("news_trade.agents.news_ingestor.build_engine") as mock_engine_f:
            engine = create_engine("sqlite://")
            mock_engine_f.return_value = engine
            agent = NewsIngestorAgent(
                settings, event_bus, provider=mock_provider,
                watchlist_manager=wlm,
            )

        await agent.run({})

        # Provider must have been called with the DYNAMIC list, not STATIC
        call_kwargs = mock_provider.fetch.call_args
        assert call_kwargs[1]["tickers"] == ["DYNAMIC"]

    async def test_sentiment_analyst_uses_watchlist_manager(self) -> None:
        from datetime import datetime

        from news_trade.agents.sentiment_analyst import SentimentAnalystAgent
        from news_trade.models.events import NewsEvent

        settings = MagicMock()
        settings.news_keyword_prefilter = True
        event_bus = AsyncMock()
        mock_provider = AsyncMock()
        mock_provider.name = "mock"
        mock_provider.analyse_batch = AsyncMock(return_value=[])

        # Only DYNAMIC is in watchlist; STATIC ticker should be filtered out
        wlm = MagicMock()
        wlm.get_active_watchlist.return_value = ["DYNAMIC"]

        agent = SentimentAnalystAgent(
            settings, event_bus, provider=mock_provider, watchlist_manager=wlm
        )

        dynamic_event = NewsEvent(
            event_id="evt-1",
            headline="Dynamic ticker news",
            source="test",
            tickers=["DYNAMIC"],
            published_at=datetime.utcnow(),
        )
        static_event = NewsEvent(
            event_id="evt-2",
            headline="Static ticker news",
            source="test",
            tickers=["STATIC"],
            published_at=datetime.utcnow(),
        )

        await agent.run({"news_events": [dynamic_event, static_event]})

        # analyse_batch should only have received the DYNAMIC event
        call_args = mock_provider.analyse_batch.call_args
        events_passed = call_args[0][0]
        assert len(events_passed) == 1
        assert events_passed[0].event_id == "evt-1"

    async def test_earnings_calendar_uses_watchlist_manager(self) -> None:
        from sqlalchemy import create_engine

        from news_trade.agents.earnings_calendar import EarningsCalendarAgent

        settings = MagicMock()
        settings.watchlist = ["STATIC"]
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock()

        primary = AsyncMock()
        primary.name = "mock_primary"
        primary.get_upcoming_earnings = AsyncMock(return_value=[])
        fallback = AsyncMock()
        fallback.name = "mock_fallback"
        fallback.get_upcoming_earnings = AsyncMock(return_value=[])

        engine = create_engine("sqlite:///:memory:")
        from news_trade.services.tables import Base as _Base
        _Base.metadata.create_all(engine)

        wlm = MagicMock()
        wlm.get_active_watchlist.return_value = ["DYNAMIC"]

        agent = EarningsCalendarAgent(
            settings, event_bus, primary, fallback, engine,
            watchlist_manager=wlm,
        )

        await agent.run({})

        # Provider should have been called with ["DYNAMIC"], not ["STATIC"]
        call_args = primary.get_upcoming_earnings.call_args
        assert call_args[0][0] == ["DYNAMIC"]
