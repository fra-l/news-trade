"""Application entrypoint — initialises services and runs the pipeline loop."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from alpaca.trading.client import TradingClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from news_trade import __version__
from news_trade.agents.earnings_calendar import EarningsCalendarAgent
from news_trade.agents.execution import ExecutionAgent
from news_trade.agents.expiry_scanner import ExpiryScanner
from news_trade.config import Settings, get_settings
from news_trade.graph.pipeline import build_pipeline
from news_trade.graph.state import PipelineState
from news_trade.models.events import EventType, NewsEvent
from news_trade.providers import get_calendar_provider, get_estimates_provider
from news_trade.providers.calendar.yfinance_provider import YFinanceCalendarProvider
from news_trade.services.database import (
    build_engine,
    build_session_factory,
    create_tables,
)
from news_trade.services.event_bus import EventBus
from news_trade.services.session_reporter import SessionReporter
from news_trade.services.stage1_repository import Stage1Repository
from news_trade.services.tables import NewsEventRow
from news_trade.services.telegram_bot import TelegramBotService
from news_trade.services.watchlist_manager import WatchlistManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("news_trade")

# Cron misfire tolerance: if the scheduler wakes up late (e.g. process was
# suspended), still run the job as long as it missed by less than this many seconds.
_MISFIRE_GRACE_SECS = 300


def _load_replay_events(settings: Settings, ticker: str, limit: int) -> list[NewsEvent]:
    """Query the last *limit* stored news events for *ticker* from the DB.

    Used by ``--replay-ticker`` to re-inject already-ingested articles into
    the pipeline without touching the live news provider or the dedup table.
    """
    engine = build_engine(settings)
    with Session(engine) as session:
        rows = (
            session.execute(
                select(NewsEventRow)
                .where(NewsEventRow.tickers_json.contains(f'"{ticker}"'))
                .order_by(NewsEventRow.ingested_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
    if not rows:
        logger.warning("replay: no stored articles found for ticker=%s", ticker)
        return []
    logger.info("replay: loaded %d article(s) for ticker=%s", len(rows), ticker)
    return [
        NewsEvent(
            event_id=row.event_id,
            headline=row.headline,
            summary=row.summary,
            source=row.source,
            url=row.url,
            tickers=row.tickers,
            event_type=EventType(row.event_type),
            published_at=row.published_at,
        )
        for row in rows
    ]


async def run_cycle(pipeline, initial_state: PipelineState) -> PipelineState:
    """Execute a single pipeline cycle and return the resulting state."""
    result = await pipeline.ainvoke(initial_state)
    return result


async def main(
    run_once: bool = False,
    replay_ticker: str | None = None,
    replay_limit: int = 5,
    stop_after: int | None = None,
    session_file: Path | None = None,
) -> None:
    """Start the trading system and loop on the configured poll interval.

    Args:
        session_file: When set, load this session JSON (or the latest one if the
                      path is the sentinel ``Path("__latest__")``) and emit a
                      startup summary before entering the main loop.
    """
    settings = get_settings()

    try:
        git_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        git_hash = "unknown"

    logger.info(
        "news-trade starting  version=%s  commit=%s  python=%s  db=%s",
        __version__,
        git_hash,
        sys.version.split()[0],
        settings.database_url,
    )

    # ------------------------------------------------------------------
    # Previous-session context — log a summary and safety warnings so the
    # operator immediately sees if the last run halted or had errors.
    # ------------------------------------------------------------------
    reporter = SessionReporter()
    if session_file is not None:
        if str(session_file) == "__latest__":
            previous = reporter.load_latest()
            if previous is None:
                logger.warning(
                    "--resume-session: no previous session files found in %s",
                    reporter._dir,
                )
        else:
            try:
                previous = reporter.load(session_file)
            except Exception:
                logger.exception(
                    "--session-file: could not read %s — skipping", session_file
                )
                previous = None
        if previous is not None:
            reporter.log_startup_summary(previous, git_hash)

    # ------------------------------------------------------------------
    # Graceful shutdown — SIGINT (Ctrl+C) and SIGTERM both set this event.
    # The running cycle is allowed to complete before the loop exits.
    # ------------------------------------------------------------------
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        if not shutdown_event.is_set():
            logger.info("Shutdown requested — finishing current cycle …")
            shutdown_event.set()

    loop.add_signal_handler(signal.SIGINT, _request_shutdown)
    loop.add_signal_handler(signal.SIGTERM, _request_shutdown)

    session_start = datetime.now(UTC)

    event_bus = EventBus(settings)
    await event_bus.connect()

    telegram_bot: TelegramBotService | None = None
    if settings.telegram_bot_token and settings.telegram_chat_id:
        telegram_bot = TelegramBotService(settings, build_session_factory(settings))
        await telegram_bot.start(event_bus)

    logger.info("Initialising database …")
    create_tables(settings)

    logger.info("Building LangGraph pipeline …")
    pipeline = build_pipeline(settings, event_bus, telegram_bot=telegram_bot)

    # ------------------------------------------------------------------
    # Cron agents — run outside the LangGraph pipeline on a daily schedule
    # ------------------------------------------------------------------
    cron_engine = build_engine(settings)
    cron_session = build_session_factory(settings)()
    cron_stage1_repo = Stage1Repository(cron_session)

    cron_wl_manager = WatchlistManager(
        settings=settings,
        session=cron_session,
        primary=get_calendar_provider(settings),
        fallback=YFinanceCalendarProvider(),
    )

    earnings_agent = EarningsCalendarAgent(
        settings,
        event_bus,
        primary=get_calendar_provider(settings),
        fallback=YFinanceCalendarProvider(),
        engine=cron_engine,
        estimates_provider=get_estimates_provider(settings),
        watchlist_manager=cron_wl_manager,
    )
    expiry_scanner = ExpiryScanner(settings, event_bus, stage1_repo=cron_stage1_repo)

    cron_alpaca = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=True,
    )
    pead_exec_agent = ExecutionAgent(
        settings,
        event_bus,
        alpaca_client=cron_alpaca,
        session=cron_session,
    )

    scheduler = AsyncIOScheduler(timezone="America/New_York")
    scheduler.add_job(
        earnings_agent.run,
        "cron",
        args=[{}],
        hour=7,
        minute=0,
        day_of_week="mon-fri",
        misfire_grace_time=_MISFIRE_GRACE_SECS,
        id="earnings_calendar",
    )
    scheduler.add_job(
        expiry_scanner.run,
        "cron",
        args=[{}],
        hour=7,
        minute=15,
        day_of_week="mon-fri",
        misfire_grace_time=_MISFIRE_GRACE_SECS,
        id="expiry_scanner",
    )
    scheduler.add_job(
        pead_exec_agent.scan_expired_pead,
        "cron",
        args=[{}],
        hour=9,
        minute=45,
        day_of_week="mon-fri",
        misfire_grace_time=_MISFIRE_GRACE_SECS,
        id="pead_expiry_scanner",
    )
    scheduler.start()
    logger.info(
        "Cron scheduler started (earnings_calendar=07:00 ET, expiry_scanner=07:15 ET, "
        "pead_expiry_scanner=09:45 ET)"
    )

    logger.info(
        "Starting news-trade loop  (watchlist=%s, interval=%ds)",
        settings.watchlist,
        settings.news_poll_interval_sec,
    )

    # --replay-ticker always runs a single cycle.
    if replay_ticker:
        run_once = True

    cycle_count = 0
    session_errors: list[str] = []
    last_state: PipelineState = {}  # type: ignore[typeddict-item]

    try:
        while not shutdown_event.is_set():
            # Operator-requested halt via Telegram /halt command.
            if telegram_bot is not None and telegram_bot.operator_halt:
                logger.info("Telegram operator halt active — skipping cycle")
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(),
                        timeout=settings.news_poll_interval_sec,
                    )
                    break
                except TimeoutError:
                    continue

            if replay_ticker:
                replay_events = _load_replay_events(
                    settings, replay_ticker, replay_limit
                )
                initial_state: PipelineState = {  # type: ignore[typeddict-item]
                    "news_events": replay_events,
                    "replay_mode": True,
                }
            else:
                initial_state = {}  # type: ignore[typeddict-item]

            last_state = await run_cycle(pipeline, initial_state)
            cycle_count += 1
            session_errors.extend(last_state.get("errors", []))

            orders = last_state.get("orders", [])
            if orders:
                logger.info("Cycle complete — placed %d order(s)", len(orders))
            else:
                logger.info("Cycle complete — no orders placed")

            if run_once:
                logger.info("--once flag set — exiting after single cycle")
                break

            if stop_after is not None and cycle_count >= stop_after:
                logger.info("--stop-after %d reached — exiting cleanly", stop_after)
                break

            # Interruptible sleep: wakes immediately when shutdown is requested
            # rather than waiting out the full poll interval.
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=settings.news_poll_interval_sec,
                )
                # shutdown_event was set during sleep — exit the loop
                break
            except TimeoutError:
                pass  # normal timeout — continue to next cycle

    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        logger.info("Shutting down scheduler and event bus …")
        scheduler.shutdown(wait=False)
        if telegram_bot is not None:
            await telegram_bot.stop()
        await event_bus.close()
        reporter.write(
            settings, session_start, cycle_count, session_errors, last_state, git_hash
        )


def entrypoint() -> None:
    """Console-script entrypoint (see pyproject.toml)."""
    parser = argparse.ArgumentParser(description="news-trade pipeline")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pipeline cycle then exit (debug mode)",
    )
    parser.add_argument(
        "--stop-after",
        type=int,
        default=None,
        metavar="N",
        help="Run exactly N pipeline cycles then exit cleanly",
    )
    parser.add_argument(
        "--replay-ticker",
        metavar="TICKER",
        default=None,
        help=(
            "Replay the last stored news articles for TICKER, skipping the live "
            "news provider and dedup check. Implies --once."
        ),
    )
    parser.add_argument(
        "--replay-limit",
        type=int,
        default=5,
        metavar="N",
        help="Number of articles to replay when --replay-ticker is set (default: 5)",
    )
    parser.add_argument(
        "--resume-session",
        action="store_true",
        help=(
            "Load the most recent session JSON from data/sessions/ on startup and "
            "log a summary including any system-halt or error warnings."
        ),
    )
    parser.add_argument(
        "--session-file",
        metavar="PATH",
        default=None,
        help=(
            "Path to a specific session JSON to load on startup. "
            "Implies --resume-session. If omitted with --resume-session, "
            "uses the latest file."
        ),
    )
    args = parser.parse_args()

    session_file: Path | None = None
    if args.session_file:
        session_file = Path(args.session_file)
    elif args.resume_session:
        session_file = Path("__latest__")

    asyncio.run(
        main(
            run_once=args.once,
            replay_ticker=args.replay_ticker,
            replay_limit=args.replay_limit,
            stop_after=args.stop_after,
            session_file=session_file,
        )
    )


if __name__ == "__main__":
    sys.exit(entrypoint())  # type: ignore[func-returns-value]
