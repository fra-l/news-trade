"""Application entrypoint — initialises services and runs the pipeline loop."""

from __future__ import annotations

import asyncio
import logging
import sys

from news_trade.config import get_settings
from news_trade.graph.pipeline import build_pipeline
from news_trade.graph.state import PipelineState
from news_trade.services.database import create_tables
from news_trade.services.event_bus import EventBus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("news_trade")


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
        await event_bus.close()


def entrypoint() -> None:
    """Console-script entrypoint (see pyproject.toml)."""
    asyncio.run(main())


if __name__ == "__main__":
    sys.exit(entrypoint())  # type: ignore[func-returns-value]
