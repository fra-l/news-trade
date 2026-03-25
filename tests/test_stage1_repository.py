"""Tests for Stage1Repository.

All tests use in-memory SQLite so no external database is required.
The session is recreated fresh for each test class via setup_method().
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.models.positions import OpenStage1Position, Stage1Status
from news_trade.services.stage1_repository import Stage1Repository
from news_trade.services.tables import Base, EarningsOutcomeRow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return factory()


def _make_position(**kwargs: object) -> OpenStage1Position:
    defaults: dict[str, object] = dict(
        id=str(uuid.uuid4()),
        ticker="AAPL",
        direction="long",
        size_pct=0.33,
        entry_price=175.00,
        opened_at=datetime(2026, 3, 20, 14, 30),
        expected_report_date=date.today() + timedelta(days=4),
        fiscal_quarter="Q2 2026",
        historical_beat_rate=0.72,
    )
    return OpenStage1Position(**(defaults | kwargs))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestPersist
# ---------------------------------------------------------------------------


class TestPersist:
    def setup_method(self) -> None:
        self.session = _make_session()
        self.repo = Stage1Repository(self.session)

    def test_insert_new_position(self) -> None:
        pos = _make_position()
        self.repo.persist(pos)
        loaded = self.repo.load_open(pos.ticker)
        assert loaded is not None
        assert loaded.id == pos.id
        assert loaded.ticker == pos.ticker

    def test_upsert_updates_open_position(self) -> None:
        pos = _make_position(entry_price=150.00, size_pct=0.25)
        self.repo.persist(pos)

        updated = _make_position(
            id=str(uuid.uuid4()),  # different id — upsert key is ticker+quarter
            ticker=pos.ticker,
            fiscal_quarter=pos.fiscal_quarter,
            entry_price=160.00,
            size_pct=0.30,
            historical_beat_rate=0.75,
        )
        self.repo.persist(updated)

        loaded = self.repo.load_open(pos.ticker)
        assert loaded is not None
        # Mutable fields updated
        assert loaded.entry_price == 160.00
        assert loaded.size_pct == 0.30
        assert loaded.historical_beat_rate == 0.75
        # Original id preserved (upsert targets existing row)
        assert loaded.id == pos.id

    def test_upsert_skips_closed_position(self) -> None:
        pos = _make_position()
        self.repo.persist(pos)
        self.repo.update_status(pos.id, Stage1Status.CONFIRMED)

        # Re-fire same ticker+quarter should not overwrite
        refired = _make_position(
            ticker=pos.ticker,
            fiscal_quarter=pos.fiscal_quarter,
            entry_price=999.00,
        )
        self.repo.persist(refired)  # should log warning and return

        # Status is still confirmed, entry_price unchanged
        from news_trade.services.tables import OpenStage1PositionRow

        row = self.session.get(OpenStage1PositionRow, pos.id)
        assert row is not None
        assert row.status == "confirmed"
        assert row.entry_price == pos.entry_price  # not 999.00

    def test_different_quarter_inserts_new_row(self) -> None:
        pos1 = _make_position(fiscal_quarter="Q2 2026")
        pos2 = _make_position(fiscal_quarter="Q3 2026")
        self.repo.persist(pos1)
        self.repo.persist(pos2)

        from news_trade.services.tables import OpenStage1PositionRow

        rows = self.session.query(OpenStage1PositionRow).all()
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# TestLoadOpen
# ---------------------------------------------------------------------------


class TestLoadOpen:
    def setup_method(self) -> None:
        self.session = _make_session()
        self.repo = Stage1Repository(self.session)

    def test_returns_none_when_empty(self) -> None:
        assert self.repo.load_open("AAPL") is None

    def test_returns_open_position(self) -> None:
        pos = _make_position()
        self.repo.persist(pos)
        loaded = self.repo.load_open(pos.ticker)
        assert loaded is not None
        assert loaded.id == pos.id

    def test_returns_none_for_confirmed_position(self) -> None:
        pos = _make_position()
        self.repo.persist(pos)
        self.repo.update_status(pos.id, Stage1Status.CONFIRMED)
        assert self.repo.load_open(pos.ticker) is None

    def test_returns_most_recent_when_multiple_open(self) -> None:
        older = _make_position(
            fiscal_quarter="Q1 2026",
            opened_at=datetime(2026, 1, 1),
        )
        newer = _make_position(
            fiscal_quarter="Q2 2026",
            opened_at=datetime(2026, 3, 1),
        )
        self.repo.persist(older)
        self.repo.persist(newer)

        loaded = self.repo.load_open("AAPL")
        assert loaded is not None
        assert loaded.id == newer.id


# ---------------------------------------------------------------------------
# TestUpdateStatus
# ---------------------------------------------------------------------------


class TestUpdateStatus:
    def setup_method(self) -> None:
        self.session = _make_session()
        self.repo = Stage1Repository(self.session)

    def _insert(self) -> OpenStage1Position:
        pos = _make_position()
        self.repo.persist(pos)
        return pos

    def test_updates_to_confirmed(self) -> None:
        pos = self._insert()
        self.repo.update_status(pos.id, Stage1Status.CONFIRMED)
        from news_trade.services.tables import OpenStage1PositionRow

        row = self.session.get(OpenStage1PositionRow, pos.id)
        assert row is not None
        assert row.status == "confirmed"

    def test_updates_to_reversed(self) -> None:
        pos = self._insert()
        self.repo.update_status(pos.id, Stage1Status.REVERSED)
        from news_trade.services.tables import OpenStage1PositionRow

        row = self.session.get(OpenStage1PositionRow, pos.id)
        assert row is not None
        assert row.status == "reversed"

    def test_updates_to_expired(self) -> None:
        pos = self._insert()
        self.repo.update_status(pos.id, Stage1Status.EXPIRED)
        from news_trade.services.tables import OpenStage1PositionRow

        row = self.session.get(OpenStage1PositionRow, pos.id)
        assert row is not None
        assert row.status == "expired"

    def test_raises_value_error_for_unknown_id(self) -> None:
        with pytest.raises(ValueError, match="No stage1 position"):
            self.repo.update_status("nonexistent-id", Stage1Status.CONFIRMED)

    def test_updated_at_is_at_least_as_recent(self) -> None:
        pos = self._insert()
        from news_trade.services.tables import OpenStage1PositionRow

        row_before = self.session.get(OpenStage1PositionRow, pos.id)
        assert row_before is not None
        original_updated_at = row_before.updated_at

        self.repo.update_status(pos.id, Stage1Status.CONFIRMED)

        self.session.expire(row_before)
        row_after = self.session.get(OpenStage1PositionRow, pos.id)
        assert row_after is not None
        assert row_after.updated_at >= original_updated_at


# ---------------------------------------------------------------------------
# TestLoadExpired
# ---------------------------------------------------------------------------


class TestLoadExpired:
    def setup_method(self) -> None:
        self.session = _make_session()
        self.repo = Stage1Repository(self.session)

    def test_empty_when_none(self) -> None:
        assert self.repo.load_expired() == []

    def test_returns_past_report_date_positions(self) -> None:
        pos = _make_position(expected_report_date=date.today() - timedelta(days=1))
        self.repo.persist(pos)
        expired = self.repo.load_expired()
        assert len(expired) == 1
        assert expired[0].id == pos.id

    def test_excludes_future_positions(self) -> None:
        pos = _make_position(expected_report_date=date.today() + timedelta(days=3))
        self.repo.persist(pos)
        assert self.repo.load_expired() == []

    def test_excludes_already_closed(self) -> None:
        pos = _make_position(expected_report_date=date.today() - timedelta(days=1))
        self.repo.persist(pos)
        self.repo.update_status(pos.id, Stage1Status.CONFIRMED)
        assert self.repo.load_expired() == []


# ---------------------------------------------------------------------------
# TestLoadAllOpen
# ---------------------------------------------------------------------------


class TestLoadAllOpen:
    def setup_method(self) -> None:
        self.session = _make_session()
        self.repo = Stage1Repository(self.session)

    def test_empty_when_none(self) -> None:
        assert self.repo.load_all_open() == []

    def test_returns_all_open_across_tickers(self) -> None:
        pos_aapl = _make_position(ticker="AAPL")
        pos_msft = _make_position(ticker="MSFT")
        self.repo.persist(pos_aapl)
        self.repo.persist(pos_msft)
        all_open = self.repo.load_all_open()
        assert {p.ticker for p in all_open} == {"AAPL", "MSFT"}

    def test_excludes_non_open(self) -> None:
        pos = _make_position()
        self.repo.persist(pos)
        self.repo.update_status(pos.id, Stage1Status.EXPIRED)
        assert self.repo.load_all_open() == []


# ---------------------------------------------------------------------------
# TestRecordOutcome
# ---------------------------------------------------------------------------


class TestRecordOutcome:
    def setup_method(self) -> None:
        self.session = _make_session()
        self.repo = Stage1Repository(self.session)

    def _insert_and_close(
        self, status: Stage1Status = Stage1Status.CONFIRMED
    ) -> OpenStage1Position:
        pos = _make_position()
        self.repo.persist(pos)
        self.repo.update_status(pos.id, status)
        return pos

    def test_records_confirmed_outcome(self) -> None:
        pos = self._insert_and_close(Stage1Status.CONFIRMED)
        self.repo.record_outcome(pos.id, Stage1Status.CONFIRMED, 3.5, 2.1)

        rows = self.session.query(EarningsOutcomeRow).all()
        assert len(rows) == 1
        assert rows[0].final_status == "confirmed"
        assert rows[0].eps_surprise_pct == pytest.approx(3.5)
        assert rows[0].price_move_1d == pytest.approx(2.1)

    def test_records_reversed_outcome(self) -> None:
        pos = self._insert_and_close(Stage1Status.REVERSED)
        self.repo.record_outcome(pos.id, Stage1Status.REVERSED, -4.2, -3.0)

        rows = self.session.query(EarningsOutcomeRow).all()
        assert rows[0].final_status == "reversed"

    def test_raises_for_unknown_stage1_id(self) -> None:
        with pytest.raises(ValueError, match="No stage1 position"):
            self.repo.record_outcome("bad-id", Stage1Status.CONFIRMED, None, None)

    def test_idempotent_second_call_is_noop(self) -> None:
        pos = self._insert_and_close()
        self.repo.record_outcome(pos.id, Stage1Status.CONFIRMED, 1.0, 0.5)
        # Second call should not raise — silently ignored
        self.repo.record_outcome(pos.id, Stage1Status.CONFIRMED, 9.9, 9.9)

        rows = self.session.query(EarningsOutcomeRow).all()
        assert len(rows) == 1
        assert rows[0].eps_surprise_pct == pytest.approx(1.0)  # first values preserved

    def test_null_fields_stored_correctly(self) -> None:
        pos = self._insert_and_close(Stage1Status.EXPIRED)
        self.repo.record_outcome(pos.id, Stage1Status.EXPIRED, None, None)

        rows = self.session.query(EarningsOutcomeRow).all()
        assert rows[0].eps_surprise_pct is None
        assert rows[0].price_move_1d is None


# ---------------------------------------------------------------------------
# TestLoadHistoricalOutcomes
# ---------------------------------------------------------------------------


def _insert_outcome(
    session: Session,
    ticker: str,
    report_date: date,
    final_status: str,
    eps: float | None = None,
    price: float | None = None,
    stage1_id: str | None = None,
) -> None:
    """Directly insert an EarningsOutcomeRow (bypasses Stage1Repository)."""
    row = EarningsOutcomeRow(
        ticker=ticker,
        report_date=report_date,
        stage1_id=stage1_id,
        final_status=final_status,
        eps_surprise_pct=eps,
        price_move_1d=price,
    )
    session.add(row)
    session.commit()


class TestLoadHistoricalOutcomes:
    def setup_method(self) -> None:
        self.session = _make_session()
        self.repo = Stage1Repository(self.session)

    def _outcome(
        self,
        status: str = "confirmed",
        days_ago: int = 90,
        eps: float | None = None,
        price: float | None = None,
    ) -> None:
        _insert_outcome(
            self.session,
            ticker="AAPL",
            report_date=date.today() - timedelta(days=days_ago),
            final_status=status,
            eps=eps,
            price=price,
        )

    def test_empty_returns_fmp_source(self) -> None:
        result = self.repo.load_historical_outcomes("AAPL")
        assert result.source == "fmp"
        assert result.beat_rate is None
        assert result.sample_size == 0

    def test_below_min_sample_returns_fmp(self) -> None:
        # 3 confirmed — below the minimum of 4
        for i in range(3):
            self._outcome("confirmed", days_ago=90 * (i + 1))
        result = self.repo.load_historical_outcomes("AAPL")
        assert result.source == "fmp"
        assert result.beat_rate is None
        assert result.sample_size == 3

    def test_above_min_sample_returns_observed(self) -> None:
        for i in range(4):
            self._outcome("confirmed", days_ago=90 * (i + 1))
        result = self.repo.load_historical_outcomes("AAPL")
        assert result.source == "observed"
        assert result.beat_rate == pytest.approx(1.0)

    def test_beat_rate_correct(self) -> None:
        # 3 confirmed, 1 reversed → 3/4 = 0.75
        for i in range(3):
            self._outcome("confirmed", days_ago=90 * (i + 1))
        self._outcome("reversed", days_ago=90 * 4)
        result = self.repo.load_historical_outcomes("AAPL")
        assert result.source == "observed"
        assert result.beat_rate == pytest.approx(0.75)

    def test_expired_excluded_from_beat_rate(self) -> None:
        # 2 confirmed, 2 expired → total=2, below min sample
        for i in range(2):
            self._outcome("confirmed", days_ago=90 * (i + 1))
        for i in range(2):
            self._outcome("expired", days_ago=90 * (i + 3))
        result = self.repo.load_historical_outcomes("AAPL")
        # total = confirmed + reversed = 2, below min of 4 → fmp
        assert result.source == "fmp"
        assert result.sample_size == 2

    def test_mean_eps_surprise_computed(self) -> None:
        for eps_val in [2.0, 4.0]:
            self._outcome("confirmed", days_ago=90, eps=eps_val)
        for i in range(2):
            self._outcome("confirmed", days_ago=90 * (i + 2), eps=None)
        result = self.repo.load_historical_outcomes("AAPL")
        # mean of [2.0, 4.0] = 3.0 (None values excluded)
        assert result.mean_eps_surprise == pytest.approx(3.0)

    def test_mean_eps_surprise_none_when_all_null(self) -> None:
        for i in range(4):
            self._outcome("confirmed", days_ago=90 * (i + 1), eps=None)
        result = self.repo.load_historical_outcomes("AAPL")
        assert result.mean_eps_surprise is None

    def test_mean_price_move_1d_computed(self) -> None:
        for price_val in [1.0, 3.0]:
            self._outcome("confirmed", days_ago=90, price=price_val)
        for i in range(2):
            self._outcome("confirmed", days_ago=90 * (i + 2))
        result = self.repo.load_historical_outcomes("AAPL")
        assert result.mean_price_move_1d == pytest.approx(2.0)

    def test_lookback_limits_rows(self) -> None:
        # Insert 10 confirmed outcomes; request only 8
        for i in range(10):
            self._outcome("confirmed", days_ago=90 * (i + 1))
        result = self.repo.load_historical_outcomes("AAPL", lookback_quarters=8)
        # 8 rows returned → sample_size=8 → source=observed
        assert result.sample_size == 8

    def test_sample_size_on_fmp_fallback(self) -> None:
        # 2 observed outcomes — reported back even when falling back to fmp
        for i in range(2):
            self._outcome("confirmed", days_ago=90 * (i + 1))
        result = self.repo.load_historical_outcomes("AAPL")
        assert result.source == "fmp"
        assert result.sample_size == 2


# ---------------------------------------------------------------------------
# TestRoundTrip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def setup_method(self) -> None:
        self.session = _make_session()
        self.repo = Stage1Repository(self.session)

    def test_persist_load_open_roundtrip(self) -> None:
        pos = _make_position(
            size_pct=0.35,
            entry_price=200.00,
            historical_beat_rate=0.80,
        )
        self.repo.persist(pos)
        loaded = self.repo.load_open(pos.ticker)
        assert loaded is not None
        assert loaded.id == pos.id
        assert loaded.ticker == pos.ticker
        assert loaded.direction == pos.direction
        assert loaded.size_pct == pytest.approx(pos.size_pct)
        assert loaded.entry_price == pytest.approx(pos.entry_price)
        assert loaded.historical_beat_rate == pytest.approx(pos.historical_beat_rate)
        assert loaded.fiscal_quarter == pos.fiscal_quarter
        assert loaded.status == Stage1Status.OPEN
        assert isinstance(loaded.days_to_report, int)

    def test_persist_update_status_load_expired(self) -> None:
        pos = _make_position(expected_report_date=date.today() - timedelta(days=1))
        self.repo.persist(pos)
        # Before status update, position is in load_expired()
        expired = self.repo.load_expired()
        assert len(expired) == 1

        self.repo.update_status(pos.id, Stage1Status.EXPIRED)
        # After marking expired, no longer returned by load_expired()
        assert self.repo.load_expired() == []

    def test_record_outcome_then_load_historical(self) -> None:
        positions = []
        for i in range(5):
            pos = _make_position(
                fiscal_quarter=f"Q{i + 1} 2025",
                expected_report_date=date.today() - timedelta(days=90 * (i + 1)),
            )
            self.repo.persist(pos)
            self.repo.update_status(pos.id, Stage1Status.CONFIRMED)
            self.repo.record_outcome(
                pos.id, Stage1Status.CONFIRMED, float(i), float(i) * 0.5
            )
            positions.append(pos)

        result = self.repo.load_historical_outcomes("AAPL")
        assert result.source == "observed"
        assert result.beat_rate == pytest.approx(1.0)
        assert result.sample_size == 5
        assert result.mean_eps_surprise is not None
        assert result.mean_price_move_1d is not None
