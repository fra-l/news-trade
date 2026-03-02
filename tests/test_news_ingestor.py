"""Unit tests for NewsIngestorAgent."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from news_trade.agents.news_ingestor import (
    NewsIngestorAgent,
    _classify_event_type,
    _parse_dt,
)
from news_trade.models.events import EventType, NewsEvent

NOW = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def agent():
    settings = MagicMock()
    settings.news_provider = "benzinga"
    settings.benzinga_api_key = "test-key"
    settings.polygon_api_key = "test-key"
    settings.watchlist = ["AAPL", "MSFT", "NVDA"]
    settings.database_url = "sqlite://"  # in-memory SQLite — no file needed

    event_bus = AsyncMock()

    with patch("news_trade.agents.news_ingestor.build_engine") as mock_engine_factory:
        from sqlalchemy import create_engine

        engine = create_engine("sqlite://")
        mock_engine_factory.return_value = engine
        a = NewsIngestorAgent(settings, event_bus)

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
            ("Microsoft Acquisition of Gaming Studio Confirmed", EventType.MERGER_ACQUISITION),
            ("Fed Signals Rate Hike Amid Rising Inflation", EventType.MACRO),
            ("Apple Raises Guidance for Next Quarter", EventType.GUIDANCE),
            ("Goldman Sachs Analyst Upgrade of NVDA to Overweight", EventType.ANALYST_RATING),
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
        before = datetime.now(timezone.utc)
        dt = _parse_dt("")
        after = datetime.now(timezone.utc)
        assert before <= dt <= after

    def test_invalid_string_returns_now(self):
        before = datetime.now(timezone.utc)
        dt = _parse_dt("not-a-date")
        after = datetime.now(timezone.utc)
        assert before <= dt <= after


# ---------------------------------------------------------------------------
# _matches_watchlist
# ---------------------------------------------------------------------------


class TestMatchesWatchlist:
    def test_match_found(self, agent):
        assert agent._matches_watchlist(["AAPL", "GOOG"]) is True

    def test_no_match(self, agent):
        assert agent._matches_watchlist(["GOOG", "AMZN"]) is False

    def test_empty_tickers(self, agent):
        assert agent._matches_watchlist([]) is False

    def test_exact_match(self, agent):
        assert agent._matches_watchlist(["NVDA"]) is True


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
# run() — end-to-end with mocked HTTP
# ---------------------------------------------------------------------------


BENZINGA_RESPONSE = [
    {
        "id": "bz-1",
        "title": "Apple Reports Record Earnings",
        "teaser": "AAPL beats EPS estimates",
        "url": "https://example.com/1",
        "stocks": [{"name": "AAPL"}],
        "created": "Mon, 02 Mar 2026 10:00:00 +0000",
    },
    {
        "id": "bz-2",
        "title": "Google Reports Earnings",
        "teaser": "GOOG beats EPS",
        "url": "https://example.com/2",
        "stocks": [{"name": "GOOG"}],  # not on watchlist
        "created": "Mon, 02 Mar 2026 10:05:00 +0000",
    },
]

POLYGON_RESPONSE = {
    "results": [
        {
            "id": "pg-1",
            "title": "NVIDIA GPU Demand Surges",
            "description": "NVDA sees record demand",
            "article_url": "https://example.com/3",
            "tickers": ["NVDA"],
            "published_utc": "2026-03-02T10:00:00Z",
        }
    ]
}


class TestRun:
    async def test_run_benzinga_returns_watchlist_events(self, agent):
        mock_resp = MagicMock()
        mock_resp.json.return_value = BENZINGA_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await agent.run({})

        events = result["news_events"]
        # Only AAPL is on the watchlist
        assert len(events) == 1
        assert events[0].tickers == ["AAPL"]
        assert events[0].event_id == "bz-1"

    async def test_run_polygon_returns_events(self, agent):
        agent.settings.news_provider = "polygon"
        mock_resp = MagicMock()
        mock_resp.json.return_value = POLYGON_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await agent.run({})

        events = result["news_events"]
        assert len(events) == 1
        assert events[0].event_id == "pg-1"

    async def test_run_deduplicates_on_second_call(self, agent):
        mock_resp = MagicMock()
        mock_resp.json.return_value = BENZINGA_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            first = await agent.run({})
            second = await agent.run({})

        assert len(first["news_events"]) == 1
        assert len(second["news_events"]) == 0  # already persisted

    async def test_run_returns_errors_on_http_failure(self, agent):
        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.ConnectError("connection refused")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await agent.run({})

        assert result["news_events"] == []
        assert len(result["errors"]) == 1

    async def test_run_publishes_to_event_bus(self, agent):
        mock_resp = MagicMock()
        mock_resp.json.return_value = BENZINGA_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await agent.run({})

        agent.event_bus.publish.assert_awaited_once()
        call_args = agent.event_bus.publish.call_args
        assert call_args[0][0] == "news_events"
        assert isinstance(call_args[0][1], NewsEvent)
