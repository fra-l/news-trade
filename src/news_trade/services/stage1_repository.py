"""Stage1Repository — all SQLite access for Stage 1 pre-earnings positions
and Pattern D earnings outcome recording.

No agent touches ORM rows directly.  All DB access for Stage 1 positions
and earnings outcomes goes through this class.

The repository is synchronous (sync SQLAlchemy Session) to match the rest
of the persistence layer.  Async agents call these methods directly; the
fast SQLite reads do not meaningfully block the event loop.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from statistics import mean
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from news_trade.models.outcomes import HistoricalOutcomes
from news_trade.models.positions import OpenStage1Position, Stage1Status
from news_trade.services.tables import EarningsOutcomeRow, OpenStage1PositionRow

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)

# Minimum number of CONFIRMED + REVERSED outcomes before switching from FMP
# data to own-system observed beat rates.  Below this threshold the sample
# is too small to be statistically meaningful.
_MIN_OBSERVED_SAMPLE: int = 4


class Stage1Repository:
    """Persistence layer for OpenStage1Position and EarningsOutcomeRow records.

    Inject a synchronous SQLAlchemy ``Session`` at construction time.  The
    session's lifecycle (begin / commit / rollback) is managed here.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Stage 1 position CRUD
    # ------------------------------------------------------------------

    def persist(self, position: OpenStage1Position) -> None:
        """Upsert a Stage 1 position keyed on (ticker, fiscal_quarter).

        - Not found → INSERT new row.
        - Found, status='open' → UPDATE mutable fields (entry_price, size_pct,
          historical_beat_rate).  Handles re-fired EARN_PRE for same quarter.
        - Found, status != 'open' → log WARNING and return without modification.
          Prevents overwriting a position that has already been resolved.
        """
        existing = (
            self._session.query(OpenStage1PositionRow)
            .filter_by(ticker=position.ticker, fiscal_quarter=position.fiscal_quarter)
            .first()
        )

        if existing is not None:
            if existing.status != Stage1Status.OPEN.value:
                _logger.warning(
                    "persist() called for %s %s but existing row has status=%s"
                    " -- skipping",
                    position.ticker,
                    position.fiscal_quarter,
                    existing.status,
                )
                return
            # Update mutable fields on the open position
            existing.entry_price = position.entry_price
            existing.size_pct = position.size_pct
            existing.historical_beat_rate = position.historical_beat_rate
            existing.updated_at = datetime.utcnow()
            _logger.debug(
                "persist() updated open position %s %s",
                position.ticker,
                position.fiscal_quarter,
            )
        else:
            row = _to_row(position)
            self._session.add(row)
            _logger.debug(
                "persist() inserted new position %s %s id=%s",
                position.ticker,
                position.fiscal_quarter,
                position.id,
            )

        self._session.commit()

    def load_open(self, ticker: str) -> OpenStage1Position | None:
        """Return the most-recently-opened OPEN position for *ticker*, or None."""
        row = (
            self._session.query(OpenStage1PositionRow)
            .filter_by(ticker=ticker, status=Stage1Status.OPEN.value)
            .order_by(OpenStage1PositionRow.opened_at.desc())
            .first()
        )
        if row is None:
            return None
        return _from_row(row)

    def update_status(self, id: str, status: Stage1Status) -> None:
        """Update the status of a Stage 1 position by its UUID.

        Raises ``ValueError`` if no row with the given *id* exists.
        Sets ``updated_at`` explicitly as belt-and-suspenders alongside the
        ORM ``onupdate`` trigger.
        """
        row = self._session.get(OpenStage1PositionRow, id)
        if row is None:
            raise ValueError(f"No stage1 position with id={id!r}")
        row.status = status.value
        row.updated_at = datetime.utcnow()
        self._session.commit()

    def load_expired(self) -> list[OpenStage1Position]:
        """Return all OPEN positions whose expected_report_date is in the past."""
        today = date.today()
        rows = (
            self._session.query(OpenStage1PositionRow)
            .filter(
                OpenStage1PositionRow.status == Stage1Status.OPEN.value,
                OpenStage1PositionRow.expected_report_date < today,
            )
            .all()
        )
        return [_from_row(r) for r in rows]

    def load_all_open(self) -> list[OpenStage1Position]:
        """Return all positions currently in OPEN status.

        Used by RiskManagerAgent for concentration-limit checks.
        """
        rows = (
            self._session.query(OpenStage1PositionRow)
            .filter_by(status=Stage1Status.OPEN.value)
            .all()
        )
        return [_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Pattern D — outcome recording and reflection
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        stage1_id: str,
        final_status: Stage1Status,
        eps_surprise_pct: float | None,
        price_move_1d: float | None,
    ) -> None:
        """Record the resolution of a Stage 1 position as an earnings outcome.

        Idempotent: if an ``EarningsOutcomeRow`` already exists for
        *stage1_id*, the duplicate insert is silently ignored (``UNIQUE``
        constraint on ``stage1_id``).

        Raises ``ValueError`` if no ``OpenStage1PositionRow`` with *stage1_id*
        exists — callers must persist the position before recording its outcome.
        """
        stage1_row = self._session.get(OpenStage1PositionRow, stage1_id)
        if stage1_row is None:
            raise ValueError(f"No stage1 position with id={stage1_id!r}")

        outcome = EarningsOutcomeRow(
            ticker=stage1_row.ticker,
            report_date=stage1_row.expected_report_date,
            stage1_id=stage1_id,
            final_status=final_status.value,
            eps_surprise_pct=eps_surprise_pct,
            price_move_1d=price_move_1d,
        )
        try:
            self._session.add(outcome)
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            _logger.warning(
                "record_outcome() called twice for stage1_id=%s — ignoring duplicate",
                stage1_id,
            )

    def load_historical_outcomes(
        self,
        ticker: str,
        lookback_quarters: int = 8,
    ) -> HistoricalOutcomes:
        """Return aggregated earnings outcomes for *ticker*.

        Queries the last *lookback_quarters* ``EarningsOutcomeRow`` records
        ordered by ``report_date DESC``.

        Beat rate is computed over CONFIRMED + REVERSED outcomes only;
        EXPIRED rows are excluded from the denominator (they indicate no data
        quality, not a directional miss).

        If ``total < _MIN_OBSERVED_SAMPLE`` (default 4), returns
        ``source='fmp'`` with ``beat_rate=None`` as a signal that the caller
        should fetch FMP historical data instead.
        """
        rows = (
            self._session.query(EarningsOutcomeRow)
            .filter_by(ticker=ticker)
            .order_by(EarningsOutcomeRow.report_date.desc())
            .limit(lookback_quarters)
            .all()
        )

        if not rows:
            return HistoricalOutcomes(source="fmp", beat_rate=None, sample_size=0)

        beats = sum(
            1 for r in rows if r.final_status == Stage1Status.CONFIRMED.value
        )
        misses = sum(
            1 for r in rows if r.final_status == Stage1Status.REVERSED.value
        )
        total = beats + misses

        # Compute optional aggregate metrics (guard against empty sequences)
        eps_values = [
            r.eps_surprise_pct for r in rows if r.eps_surprise_pct is not None
        ]
        price_values = [r.price_move_1d for r in rows if r.price_move_1d is not None]
        mean_eps = mean(eps_values) if eps_values else None
        mean_price = mean(price_values) if price_values else None

        if total < _MIN_OBSERVED_SAMPLE:
            return HistoricalOutcomes(
                source="fmp",
                beat_rate=None,
                sample_size=total,
                mean_eps_surprise=mean_eps,
                mean_price_move_1d=mean_price,
            )

        return HistoricalOutcomes(
            source="observed",
            beat_rate=beats / total,
            sample_size=total,
            mean_eps_surprise=mean_eps,
            mean_price_move_1d=mean_price,
        )


# ------------------------------------------------------------------
# Private conversion helpers
# ------------------------------------------------------------------


def _to_row(position: OpenStage1Position) -> OpenStage1PositionRow:
    """Convert a Pydantic OpenStage1Position to its ORM counterpart."""
    return OpenStage1PositionRow(
        id=position.id,
        ticker=position.ticker,
        direction=position.direction,
        size_pct=position.size_pct,
        entry_price=position.entry_price,
        opened_at=position.opened_at,
        expected_report_date=position.expected_report_date,
        fiscal_quarter=position.fiscal_quarter,
        historical_beat_rate=position.historical_beat_rate,
        status=position.status.value,
        updated_at=datetime.utcnow(),
    )


def _from_row(row: OpenStage1PositionRow) -> OpenStage1Position:
    """Convert an ORM row to a frozen Pydantic OpenStage1Position.

    ``updated_at`` is a DB-only audit field and is not included in the
    Pydantic model.  ``days_to_report`` is a computed field derived from
    ``expected_report_date`` at access time.
    """
    return OpenStage1Position(
        id=row.id,
        ticker=row.ticker,
        direction=row.direction,
        size_pct=row.size_pct,
        entry_price=row.entry_price,
        opened_at=row.opened_at,
        expected_report_date=row.expected_report_date,
        fiscal_quarter=row.fiscal_quarter,
        historical_beat_rate=row.historical_beat_rate,
        status=Stage1Status(row.status),
    )
