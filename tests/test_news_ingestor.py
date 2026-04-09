"""Unit tests for NewsIngestorAgent."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from news_trade.agents.news_ingestor import (
    NewsIngestorAgent,
    _classify_event_type,
    _parse_dt,
)
from news_trade.models.events import EventType, NewsEvent

NOW = datetime(2026, 3, 2, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_news_event(event_id: str = "bz-1", ticker: str = "AAPL") -> NewsEvent:
    return NewsEvent(
        event_id=event_id,
        headline="Apple Reports Record Earnings",
        source="benzinga",
        tickers=[ticker],
        published_at=NOW,
    )


@pytest.fixture()
def mock_provider():
    provider = AsyncMock()
    provider.name = "mock"
    provider.fetch = AsyncMock(return_value=[])
    return provider


_TICKERS = ["AAPL", "MSFT", "NVDA"]


@pytest.fixture()
def agent(mock_provider):
    settings = MagicMock()
    settings.database_url = "sqlite://"  # in-memory SQLite — no file needed
    settings.news_keyword_prefilter = True

    event_bus = AsyncMock()

    with patch("news_trade.agents.news_ingestor.build_engine") as mock_engine_factory:
        from sqlalchemy import create_engine

        from news_trade.services.tables import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        mock_engine_factory.return_value = engine
        a = NewsIngestorAgent(
            settings, event_bus, provider=mock_provider,
            tickers=_TICKERS,
        )

    return a


# ---------------------------------------------------------------------------
# _classify_event_type
# ---------------------------------------------------------------------------


class TestClassifyEventType:
    @pytest.mark.parametrize(
        "headline,expected",
        [
            ("AAPL Reports Record Earnings and EPS Beat", EventType.EARNINGS),
            ("FDA Approves New Drug for Alzheimer's", EventType.FDA_APPROVAL),
            (
                "Microsoft Acquisition of Gaming Studio Confirmed",
                EventType.MERGER_ACQUISITION,
            ),
            ("Fed Signals Rate Hike Amid Rising Inflation", EventType.MACRO),
            ("Apple Raises Guidance for Next Quarter", EventType.GUIDANCE),
            (
                "Goldman Sachs Analyst Upgrade of NVDA to Overweight",
                EventType.ANALYST_RATING,
            ),
            ("Tesla Files 10-K with SEC", EventType.SEC_FILING),
            ("Company Announces New Product Line", EventType.OTHER),
        ],
    )
    def test_classification(self, headline, expected):
        assert _classify_event_type(headline) == expected

    def test_case_insensitive(self):
        assert _classify_event_type("APPLE EARNINGS BEAT") == EventType.EARNINGS

    def test_empty_headline_returns_other(self):
        assert _classify_event_type("") == EventType.OTHER


# ---------------------------------------------------------------------------
# _parse_dt
# ---------------------------------------------------------------------------


class TestParseDt:
    def test_iso8601_with_z(self):
        dt = _parse_dt("2026-03-02T12:00:00Z")
        assert dt.year == 2026
        assert dt.month == 3

    def test_iso8601_with_offset(self):
        dt = _parse_dt("2026-03-02T12:00:00+00:00")
        assert dt.year == 2026

    def test_rfc2822(self):
        dt = _parse_dt("Mon, 02 Mar 2026 12:00:00 +0000")
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 2

    def test_empty_string_returns_now(self):
        before = datetime.now(UTC)
        dt = _parse_dt("")
        after = datetime.now(UTC)
        assert before <= dt <= after

    def test_invalid_string_returns_now(self):
        before = datetime.now(UTC)
        dt = _parse_dt("not-a-date")
        after = datetime.now(UTC)
        assert before <= dt <= after


# ---------------------------------------------------------------------------
# _matches_tickers
# ---------------------------------------------------------------------------


class TestMatchesTickers:
    def test_match_found(self, agent):
        assert agent._matches_tickers(["AAPL", "GOOG"]) is True

    def test_no_match(self, agent):
        assert agent._matches_tickers(["GOOG", "AMZN"]) is False

    def test_empty_tickers(self, agent):
        assert agent._matches_tickers([]) is False

    def test_exact_match(self, agent):
        assert agent._matches_tickers(["NVDA"]) is True


# ---------------------------------------------------------------------------
# _is_duplicate
# ---------------------------------------------------------------------------


class TestIsDuplicate:
    def test_not_duplicate_on_empty_db(self, agent):
        from sqlalchemy.orm import Session

        with Session(agent._engine) as session:
            assert agent._is_duplicate("new-event-1", session) is False

    def test_duplicate_after_persist(self, agent):
        from sqlalchemy.orm import Session

        event = NewsEvent(
            event_id="dup-1",
            headline="Test",
            source="benzinga",
            published_at=NOW,
        )
        with Session(agent._engine) as session:
            agent._persist(event, session)
            session.commit()

        with Session(agent._engine) as session:
            assert agent._is_duplicate("dup-1", session) is True

    def test_different_event_id_not_duplicate(self, agent):
        from sqlalchemy.orm import Session

        event = NewsEvent(
            event_id="event-a",
            headline="Test A",
            source="benzinga",
            published_at=NOW,
        )
        with Session(agent._engine) as session:
            agent._persist(event, session)
            session.commit()

        with Session(agent._engine) as session:
            assert agent._is_duplicate("event-b", session) is False


# ---------------------------------------------------------------------------
# run() — end-to-end with mocked provider
# ---------------------------------------------------------------------------


class TestRun:
    async def test_run_returns_session_ticker_events(self, agent, mock_provider):
        aapl_event = _make_news_event("bz-1", "AAPL")
        goog_event = _make_news_event("bz-2", "GOOG")  # not a session ticker
        mock_provider.fetch.return_value = [aapl_event, goog_event]

        result = await agent.run({})

        events = result["news_events"]
        assert len(events) == 1
        assert events[0].event_id == "bz-1"

    async def test_run_deduplicates_on_second_call(self, agent, mock_provider):
        event = _make_news_event("bz-1", "AAPL")
        mock_provider.fetch.return_value = [event]

        first = await agent.run({})
        second = await agent.run({})

        assert len(first["news_events"]) == 1
        assert len(second["news_events"]) == 0  # already persisted

    async def test_run_returns_errors_on_provider_failure(self, agent, mock_provider):
        mock_provider.fetch.side_effect = RuntimeError("provider error")

        result = await agent.run({})

        assert result["news_events"] == []
        assert len(result["errors"]) == 1

    async def test_run_publishes_to_event_bus(self, agent, mock_provider):
        event = _make_news_event("bz-1", "AAPL")
        mock_provider.fetch.return_value = [event]

        await agent.run({})

        agent.event_bus.publish.assert_awaited_once()
        call_args = agent.event_bus.publish.call_args
        assert call_args[0][0] == "news_events"
        assert isinstance(call_args[0][1], NewsEvent)

    async def test_run_passes_tickers_to_provider(self, agent, mock_provider):
        mock_provider.fetch.return_value = []

        await agent.run({})

        mock_provider.fetch.assert_awaited_once()
        call_kwargs = mock_provider.fetch.call_args
        assert call_kwargs[1]["tickers"] == _TICKERS

    async def test_run_returns_empty_on_no_events(self, agent, mock_provider):
        mock_provider.fetch.return_value = []

        result = await agent.run({})

        assert result["news_events"] == []

    async def test_run_replay_mode_passes_events_through(self, agent, mock_provider):
        """When replay_mode=True the agent must return the pre-loaded events without
        touching the live provider or the dedup table."""
        event = _make_news_event("replay-1", "AAPL")
        state = {"replay_mode": True, "news_events": [event]}

        result = await agent.run(state)

        assert result["news_events"] == [event]
        mock_provider.fetch.assert_not_awaited()

    async def test_run_replay_mode_empty_events(self, agent, mock_provider):
        """replay_mode=True with no pre-loaded events returns an empty list."""
        state = {"replay_mode": True}

        result = await agent.run(state)

        assert result["news_events"] == []
        mock_provider.fetch.assert_not_awaited()
