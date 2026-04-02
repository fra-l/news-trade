"""Session reporting — write and read JSON session summaries.

``SessionReporter`` encapsulates:
- Writing a compact audit JSON to ``data/sessions/`` on process exit.
- Reading a previous session JSON on startup (latest or a specific file)
  so the operator gets an immediate context summary and safety warnings.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from news_trade import __version__
from news_trade.config import Settings
from news_trade.graph.state import PipelineState
from news_trade.services.database import build_engine
from news_trade.services.tables import OpenStage1PositionRow, OrderRow, TradeSignalRow

logger = logging.getLogger(__name__)


class SessionReporter:
    """Read and write JSON session summaries under *sessions_dir*.

    The filename pattern ``session_YYYYMMDD_HHMMSS.json`` is timestamp-sortable,
    so ``find_latest()`` uses a simple lexicographic sort — no date parsing needed.

    Args:
        sessions_dir: Directory where session JSON files are stored.
                      Defaults to ``data/sessions`` relative to the working directory.
    """

    def __init__(self, sessions_dir: Path = Path("data/sessions")) -> None:
        self._dir = sessions_dir

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(
        self,
        settings: Settings,
        session_start: datetime,
        cycle_count: int,
        errors: list[str],
        last_state: PipelineState,
        git_hash: str,
    ) -> Path:
        """Write a JSON session summary and return its path.

        Queries orders, signals, and open Stage 1 positions created since
        *session_start*.  On DB failure writes a minimal partial record so
        there is always *some* audit file on disk.
        """
        session_end = datetime.now(UTC)
        # DB timestamps are naive UTC — strip tzinfo for comparison.
        session_start_naive = session_start.replace(tzinfo=None)

        try:
            engine = build_engine(settings)
            with Session(engine) as db:
                order_rows = (
                    db.execute(
                        select(OrderRow).where(
                            OrderRow.created_at >= session_start_naive
                        )
                    )
                    .scalars()
                    .all()
                )
                signal_rows = (
                    db.execute(
                        select(TradeSignalRow).where(
                            TradeSignalRow.created_at >= session_start_naive
                        )
                    )
                    .scalars()
                    .all()
                )
                open_positions = (
                    db.execute(
                        select(OpenStage1PositionRow).where(
                            OpenStage1PositionRow.status == "open"
                        )
                    )
                    .scalars()
                    .all()
                )

            report: dict[str, Any] = {
                "session_start": session_start.isoformat(),
                "session_end": session_end.isoformat(),
                "duration_seconds": round(
                    (session_end - session_start.replace(tzinfo=UTC)).total_seconds(),
                    1,
                ),
                "version": __version__,
                "commit": git_hash,
                "cycles_run": cycle_count,
                "system_halted": last_state.get("system_halted", False),
                "orders_placed": [
                    {
                        "order_id": o.order_id,
                        "ticker": o.ticker,
                        "side": o.side,
                        "qty": o.qty,
                        "status": o.status,
                        "submitted_at": (
                            o.submitted_at.isoformat() if o.submitted_at else None
                        ),
                    }
                    for o in order_rows
                ],
                "signals": {
                    "total": len(signal_rows),
                    "approved": sum(1 for s in signal_rows if s.approved),
                    "rejected": sum(1 for s in signal_rows if not s.approved),
                },
                "open_stage1_positions": [
                    {
                        "id": p.id,
                        "ticker": p.ticker,
                        "direction": p.direction,
                        "size_pct": p.size_pct,
                        "expected_report_date": p.expected_report_date.isoformat(),
                        "fiscal_quarter": p.fiscal_quarter,
                        "status": p.status,
                    }
                    for p in open_positions
                ],
                "errors": errors,
            }
        except Exception:
            logger.exception("Failed to build session report — writing partial record")
            report = {
                "session_start": session_start.isoformat(),
                "session_end": session_end.isoformat(),
                "cycles_run": cycle_count,
                "version": __version__,
                "commit": git_hash,
                "error": "report generation failed — see logs",
            }

        self._dir.mkdir(parents=True, exist_ok=True)
        ts = session_start.strftime("%Y%m%d_%H%M%S")
        out_path = self._dir / f"session_{ts}.json"
        out_path.write_text(json.dumps(report, indent=2))
        logger.info("Session report written → %s", out_path)
        return out_path

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def find_latest(self) -> Path | None:
        """Return the path of the most recent session JSON, or ``None``."""
        candidates = sorted(self._dir.glob("session_*.json"))
        return candidates[-1] if candidates else None

    def load(self, path: Path) -> dict[str, Any]:
        """Parse and return the session JSON at *path*."""
        return json.loads(path.read_text())  # type: ignore[no-any-return]

    def load_latest(self) -> dict[str, Any] | None:
        """Load the most recent session JSON, or return ``None`` if none exist."""
        path = self.find_latest()
        return self.load(path) if path is not None else None

    # ------------------------------------------------------------------
    # Startup summary
    # ------------------------------------------------------------------

    def log_startup_summary(
        self, previous: dict[str, Any], current_commit: str
    ) -> None:
        """Emit startup log lines summarising the *previous* session.

        Called early in ``main()`` when ``--resume-session`` / ``--session-file``
        is set.  Emits a WARNING if the previous session ended with a system halt
        or recorded errors so the operator cannot miss it.
        """
        session_start = previous.get("session_start", "unknown")
        duration = previous.get("duration_seconds", "?")
        cycles = previous.get("cycles_run", "?")
        orders = len(previous.get("orders_placed", []))
        signals = previous.get("signals", {})
        open_pos = len(previous.get("open_stage1_positions", []))
        prev_version = previous.get("version", "?")
        prev_commit = previous.get("commit", "?")
        system_halted = previous.get("system_halted", False)
        prev_errors: list[str] = previous.get("errors", [])

        logger.info(
            "Previous session: start=%s  duration=%ss  cycles=%s  "
            "orders=%d  signals(total/approved/rejected)=%s/%s/%s  "
            "open_stage1=%d  version=%s  commit=%s",
            session_start,
            duration,
            cycles,
            orders,
            signals.get("total", "?"),
            signals.get("approved", "?"),
            signals.get("rejected", "?"),
            open_pos,
            prev_version,
            prev_commit,
        )

        if system_halted:
            logger.warning(
                "PREVIOUS SESSION ENDED WITH SYSTEM HALT — "
                "drawdown guard or halt handler was triggered. "
                "Review logs before resuming live trading."
            )

        if prev_errors:
            logger.warning(
                "Previous session recorded %d error(s):", len(prev_errors)
            )
            for err in prev_errors:
                logger.warning("  • %s", err)

        if (
            prev_commit != "unknown"
            and current_commit != "unknown"
            and prev_commit != current_commit
        ):
            logger.info(
                "Version change since last session: %s → %s",
                prev_commit,
                current_commit,
            )
