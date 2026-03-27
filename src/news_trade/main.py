"""Application entrypoint — initialises services and runs the pipeline loop."""

from __future__ import annotations

import asyncio
import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from news_trade.agents.earnings_calendar import EarningsCalendarAgent
from news_trade.agents.expiry_scanner import ExpiryScanner
from news_trade.config import get_settings
from news_trade.graph.pipeline import build_pipeline
from news_trade.graph.state import PipelineState
from news_trade.providers import get_calendar_provider
from news_trade.providers.calendar.yfinance_provider import YFinanceCalendarProvider
from news_trade.services.database import (
    build_engine,
    build_session_factory,
    create_tables,
)
from news_trade.services.event_bus import EventBus
from news_trade.services.stage1_repository import Stage1Repository
from news_trade.services.tables import Base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("news_trade")

# Cron misfire tolerance: if the scheduler wakes up late (e.g. process was
# suspended), still run the job as long as it missed by less than this many seconds.
_MISFIRE_GRACE_SECS = 300


async def run_cycle(pipeline, initial_state: PipelineState) -> PipelineState:
    """Execute a single pipeline cycle and return the resulting state."""
    result = await pipeline.ainvoke(initial_state)
    return result


async def main() -> None:
    """Start the trading system and loop on the configured poll interval."""
    settings = get_settings()
    event_bus = EventBus(settings)
    await event_bus.connect()

    logger.info("Initialising database …")
    create_tables(settings)

    logger.info("Building LangGraph pipeline …")
    pipeline = build_pipeline(settings, event_bus)

    # ------------------------------------------------------------------
    # Cron agents — run outside the LangGraph pipeline on a daily schedule
    # ------------------------------------------------------------------
    cron_engine = build_engine(settings)
    Base.metadata.create_all(cron_engine)
    cron_session = build_session_factory(settings)()
    cron_stage1_repo = Stage1Repository(cron_session)

    earnings_agent = EarningsCalendarAgent(
        settings,
        event_bus,
        primary=get_calendar_provider(settings),
        fallback=YFinanceCalendarProvider(),
        engine=cron_engine,
    )
    expiry_scanner = ExpiryScanner(settings, event_bus, stage1_repo=cron_stage1_repo)

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
    scheduler.start()
    logger.info(
        "Cron scheduler started (earnings_calendar=07:00 ET, expiry_scanner=07:15 ET)"
    )

    logger.info(
        "Starting news-trade loop  (watchlist=%s, interval=%ds)",
        settings.watchlist,
        settings.news_poll_interval_sec,
    )

    try:
        while True:
            initial_state: PipelineState = {}  # type: ignore[typeddict-item]
            state = await run_cycle(pipeline, initial_state)

            orders = state.get("orders", [])
            if orders:
                logger.info("Cycle complete — placed %d order(s)", len(orders))
            else:
                logger.info("Cycle complete — no orders placed")

            await asyncio.sleep(settings.news_poll_interval_sec)

    except KeyboardInterrupt:
        logger.info("Shutting down …")
    finally:
        scheduler.shutdown(wait=False)
        await event_bus.close()


def entrypoint() -> None:
    """Console-script entrypoint (see pyproject.toml)."""
    asyncio.run(main())


if __name__ == "__main__":
    sys.exit(entrypoint())  # type: ignore[func-returns-value]
