"""Unit tests for ExpiryScanner."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.agents.expiry_scanner import ExpiryScanner
from news_trade.config import Settings
from news_trade.models.positions import OpenStage1Position, Stage1Status
from news_trade.services.stage1_repository import Stage1Repository
from news_trade.services.tables import Base

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        anthropic_api_key="test-key",
        llm_provider="anthropic",
        llm_quick_model="claude-haiku-4-5-20251001",
        llm_deep_model="claude-sonnet-4-6",
    )
    return Settings(**(defaults | kwargs))


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_position(ticker: str = "AAPL", days_ago: int = 1) -> OpenStage1Position:
    """Create a position whose report date has already passed."""
    return OpenStage1Position(
        id=str(uuid.uuid4()),
        ticker=ticker,
        direction="long",
        size_pct=0.33,
        entry_price=175.0,
        opened_at=datetime.utcnow() - timedelta(days=days_ago + 5),
        expected_report_date=date.today() - timedelta(days=days_ago),
        fiscal_quarter="Q2 2026",
        historical_beat_rate=0.72,
    )


def _make_scanner(
    expired_positions: list[OpenStage1Position] | None = None,
) -> tuple[ExpiryScanner, MagicMock]:
    settings = _make_settings()
    event_bus = MagicMock()
    stage1_repo = MagicMock(spec=Stage1Repository)
    stage1_repo.load_expired.return_value = expired_positions or []
    scanner = ExpiryScanner(
        settings=settings, event_bus=event_bus, stage1_repo=stage1_repo
    )
    return scanner, stage1_repo


# ---------------------------------------------------------------------------
# TestExpiryScannerNoExpired
# ---------------------------------------------------------------------------


class TestExpiryScannerNoExpired:
    @pytest.mark.asyncio
    async def test_no_expired_positions_returns_empty_errors(self):
        scanner, _repo = _make_scanner(expired_positions=[])
        result = await scanner.run({})
        assert result == {"errors": []}

    @pytest.mark.asyncio
    async def test_no_expired_positions_no_update_calls(self):
        scanner, repo = _make_scanner(expired_positions=[])
        await scanner.run({})
        repo.update_status.assert_not_called()


# ---------------------------------------------------------------------------
# TestExpiryScannerWithExpired
# ---------------------------------------------------------------------------


class TestExpiryScannerWithExpired:
    def setup_method(self) -> None:
        self.pos1 = _make_position("AAPL", days_ago=1)
        self.pos2 = _make_position("MSFT", days_ago=2)
        self.scanner, self.repo = _make_scanner([self.pos1, self.pos2])

    @pytest.mark.asyncio
    async def test_all_expired_positions_are_updated(self):
        await self.scanner.run({})
        assert self.repo.update_status.call_count == 2
        self.repo.update_status.assert_any_call(self.pos1.id, Stage1Status.EXPIRED)
        self.repo.update_status.assert_any_call(self.pos2.id, Stage1Status.EXPIRED)

    @pytest.mark.asyncio
    async def test_returns_empty_errors(self):
        result = await self.scanner.run({})
        assert result == {"errors": []}

    @pytest.mark.asyncio
    async def test_logs_warning_per_expired_position(self):
        with patch.object(self.scanner.logger, "warning") as mock_warn:
            await self.scanner.run({})
        assert mock_warn.call_count == 2

    @pytest.mark.asyncio
    async def test_state_passthrough_does_not_modify_other_keys(self):
        state = {"trade_signals": ["existing"], "orders": []}
        result = await self.scanner.run(state)
        # ExpiryScanner only writes "errors" — it must not overwrite other keys
        assert "trade_signals" not in result
        assert "orders" not in result


# ---------------------------------------------------------------------------
# TestExpiryScannerWithRealRepo
# ---------------------------------------------------------------------------


class TestExpiryScannerWithRealRepo:
    """Integration-style test using in-memory SQLite + real Stage1Repository."""

    def setup_method(self) -> None:
        session = _make_session()
        self.repo = Stage1Repository(session)

        # Persist two positions: one expired, one not.
        self.expired_pos = _make_position("AAPL", days_ago=1)
        future_pos = OpenStage1Position(
            id=str(uuid.uuid4()),
            ticker="MSFT",
            direction="long",
            size_pct=0.30,
            entry_price=400.0,
            opened_at=datetime.utcnow(),
            expected_report_date=date.today() + timedelta(days=3),
            fiscal_quarter="Q2 2026",
            historical_beat_rate=0.65,
        )
        self.repo.persist(self.expired_pos)
        self.repo.persist(future_pos)

        settings = _make_settings()
        event_bus = MagicMock()
        self.scanner = ExpiryScanner(
            settings=settings, event_bus=event_bus, stage1_repo=self.repo
        )

    @pytest.mark.asyncio
    async def test_only_expired_position_is_marked(self):
        await self.scanner.run({})
        # Expired position should now have EXPIRED status.
        pos = self.repo.load_open(self.expired_pos.ticker)
        assert pos is None  # load_open only returns OPEN positions

    @pytest.mark.asyncio
    async def test_future_position_stays_open(self):
        await self.scanner.run({})
        pos = self.repo.load_open("MSFT")
        assert pos is not None
        assert pos.status == Stage1Status.OPEN
