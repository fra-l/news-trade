"""gov-trade entry point — wires services and runs the USASpending polling loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from alpaca.trading.client import TradingClient
from sqlalchemy.orm import Session

from gov_trade.graph.pipeline import build_gov_pipeline_from_settings
from gov_trade.graph.state import GovTradeState
from news_trade import __version__
from news_trade.agents.execution import ExecutionAgent
from news_trade.agents.halt_handler import HaltHandlerAgent
from news_trade.config import (
    GovTradeSettings,
    Settings,
    get_gov_trade_settings,
    get_settings,
)
from news_trade.services.database import build_session_factory, create_tables
from news_trade.services.event_bus import EventBus
from news_trade.services.stage1_repository import Stage1Repository

logger = logging.getLogger("gov_trade")

# data/agency_lda_mapping.json lives at the project root.
_AGENCY_MAPPING_PATH = Path(__file__).parents[2] / "data" / "agency_lda_mapping.json"


def _configure_logging(log_file: str) -> None:
    fmt = "%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s"
    formatter = logging.Formatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setFormatter(formatter)
    root.addHandler(fh)

    logger.info("Logging to %s", Path(log_file).resolve())


def _load_agency_mapping() -> dict[str, list[str]]:
    """Load agency name normalisation map; returns empty dict when file is absent."""
    if not _AGENCY_MAPPING_PATH.exists():
        logger.warning(
            "Agency mapping file not found at %s — "
            "lobbying enrichment will use raw names",
            _AGENCY_MAPPING_PATH,
        )
        return {}
    try:
        with _AGENCY_MAPPING_PATH.open() as fh:
            return json.load(fh)  # type: ignore[no-any-return]
    except Exception as exc:
        logger.warning("Could not load agency mapping: %s", exc)
        return {}


async def _run_cycle(pipeline: object, initial_state: GovTradeState) -> GovTradeState:
    run_name = f"gov-cycle-{datetime.now(tz=UTC).strftime('%Y%m%d-%H%M%S')}"
    result: GovTradeState = await pipeline.ainvoke(  # type: ignore[attr-defined]
        initial_state, config={"run_name": run_name}
    )
    return result


async def main(run_once: bool = False) -> None:
    """Start the gov-trade polling loop."""
    settings: Settings = get_settings()
    gov_settings: GovTradeSettings = get_gov_trade_settings()

    if not gov_settings.gov_trade_enabled:
        logger.warning(
            "GOV_TRADE_ENABLED is false — "
            "set it to true in .env to activate the pipeline. Exiting."
        )
        return

    try:
        git_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        git_hash = "unknown"

    logger.info(
        "gov-trade starting  version=%s  commit=%s  python=%s  db=%s",
        __version__,
        git_hash,
        sys.version.split()[0],
        settings.database_url,
    )

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        if not shutdown_event.is_set():
            logger.info("Shutdown requested — finishing current cycle …")
            shutdown_event.set()

    loop.add_signal_handler(signal.SIGINT, _request_shutdown)
    loop.add_signal_handler(signal.SIGTERM, _request_shutdown)

    logger.info("Applying database migrations …")
    create_tables(settings)

    event_bus = EventBus(settings)
    await event_bus.connect()

    session: Session = build_session_factory(settings)()
    stage1_repo = Stage1Repository(session)

    alpaca = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=True,
    )
    exec_agent = ExecutionAgent(
        settings, event_bus, alpaca_client=alpaca, session=session
    )
    halt_agent = HaltHandlerAgent(
        settings, event_bus, alpaca_client=alpaca, stage1_repo=stage1_repo
    )

    agency_mapping = _load_agency_mapping()
    logger.info("Agency mapping: %d entries loaded", len(agency_mapping))

    logger.info("Building gov-trade LangGraph pipeline …")
    pipeline = build_gov_pipeline_from_settings(
        settings=settings,
        gov_settings=gov_settings,
        event_bus=event_bus,
        stage1_repo=stage1_repo,
        exec_agent=exec_agent,
        halt_agent=halt_agent,
        agency_mapping=agency_mapping,
    )

    poll_seconds = gov_settings.usaspending_poll_interval_minutes * 60
    logger.info(
        "Starting gov-trade loop  poll_interval=%dm",
        gov_settings.usaspending_poll_interval_minutes,
    )

    cycle_count = 0
    try:
        while not shutdown_event.is_set():
            initial_state: GovTradeState = {}
            last_state = await _run_cycle(pipeline, initial_state)
            cycle_count += 1

            orders = last_state.get("orders", [])
            errors = last_state.get("errors", [])
            if orders:
                logger.info("Cycle complete — placed %d order(s)", len(orders))
            else:
                logger.info("Cycle complete — no orders placed")
            for err in errors:
                logger.warning("Pipeline error: %s", err)

            if run_once:
                logger.info("--once flag set — exiting after single cycle")
                break

            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=poll_seconds)
                break  # shutdown_event set during sleep
            except TimeoutError:
                pass  # normal — continue to next cycle

    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        logger.info("Shutting down event bus …")
        await event_bus.close()
        session.close()
        logger.info("gov-trade stopped after %d cycle(s)", cycle_count)


def entrypoint() -> None:
    """Console-script entrypoint — registered as `gov-trade` in pyproject.toml."""
    parser = argparse.ArgumentParser(description="gov-trade pipeline")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pipeline cycle then exit",
    )
    parser.add_argument(
        "--log-file",
        metavar="FILE",
        default="gov_trade.log",
        help="Log file path (default: gov_trade.log). Overwritten on every run.",
    )
    args = parser.parse_args()

    _configure_logging(args.log_file)
    asyncio.run(main(run_once=args.once))


if __name__ == "__main__":
    sys.exit(entrypoint())  # type: ignore[func-returns-value]
