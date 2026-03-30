"""Interactive watchlist selector CLI.

Usage::

    uv run select-watchlist

Scans the next 30 days of earnings via the configured CalendarProvider,
displays a numbered table of upcoming reporters, and prompts the operator
to pick which tickers to activate.  The selection is persisted to SQLite;
the running pipeline picks it up on the next cycle — no restart needed.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta

from news_trade.config import get_settings
from news_trade.providers import get_calendar_provider
from news_trade.providers.calendar.yfinance_provider import YFinanceCalendarProvider
from news_trade.services.database import build_engine, build_session_factory
from news_trade.services.tables import Base
from news_trade.services.watchlist_manager import WatchlistManager

_SCAN_DAYS = 30
_COL_WIDTHS = (4, 8, 14, 14, 8)  # #, Ticker, Report date, Timing, EPS est
_HEADER_SEP = "─" * (sum(_COL_WIDTHS) + len(_COL_WIDTHS) * 2 + 1)


def _fmt_row(
    num: int | str, ticker: str, report_date: date | str, timing: str, eps: str
) -> str:
    return (
        f" {num:<{_COL_WIDTHS[0]}}"
        f" {ticker:<{_COL_WIDTHS[1]}}"
        f" {report_date!s:<{_COL_WIDTHS[2]}}"
        f" {timing:<{_COL_WIDTHS[3]}}"
        f" {eps:<{_COL_WIDTHS[4]}}"
    )


async def main() -> None:
    """Entry point for ``select-watchlist``."""
    settings = get_settings()

    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    session = build_session_factory(settings)()

    manager = WatchlistManager(
        settings=settings,
        session=session,
        primary=get_calendar_provider(settings),
        fallback=YFinanceCalendarProvider(),
    )

    today = date.today()
    to_date = today + timedelta(days=_SCAN_DAYS)

    print(f"\nScanning earnings calendar: {today} → {to_date}")
    print("Fetching from provider …\n")

    candidates = await manager.scan_candidates(today, to_date)

    if not candidates:
        print(
            "No upcoming earnings found in the next 30 days"
            " for the configured watchlist."
        )
        print(f"Static watchlist: {settings.watchlist}")
        session.close()
        return

    print(
        f"Found {len(candidates)} companies reporting"
        f" in the next {_SCAN_DAYS} days:\n"
    )

    header = _fmt_row("#", "Ticker", "Report date", "Timing", "EPS est")
    print(header)
    print(_HEADER_SEP)
    for i, entry in enumerate(candidates, start=1):
        eps_str = (
            f"{entry.eps_estimate:.2f}"
            if entry.eps_estimate is not None
            else "n/a"
        )
        print(_fmt_row(i, entry.ticker, entry.report_date, str(entry.timing), eps_str))

    print()
    current = manager.load_selected()
    if current:
        print(f"Current selection: {', '.join(current)}")
    else:
        print(
            "No saved selection — active watchlist is from .env:"
            f" {settings.watchlist}"
        )

    print()
    raw = input(
        "Enter numbers to include (comma-separated),"
        " or ENTER to keep current selection:\n> "
    ).strip()

    if not raw:
        if current:
            print(f"\nNo change. Active watchlist: {', '.join(current)}")
        else:
            print(f"\nNo change. Using static watchlist: {settings.watchlist}")
        session.close()
        return

    chosen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            idx = int(part)
        except ValueError:
            print(f"Invalid number: {part!r} — ignoring", file=sys.stderr)
            continue
        if idx < 1 or idx > len(candidates):
            print(f"Number out of range: {idx} — ignoring", file=sys.stderr)
            continue
        chosen.append(candidates[idx - 1].ticker)

    if not chosen:
        print("\nNo valid numbers entered — watchlist unchanged.", file=sys.stderr)
        session.close()
        return

    manager.save_selection(chosen)
    print(f"\nSaved {len(chosen)} tickers: {', '.join(chosen)}")
    print("Active watchlist updated — no restart required.")
    session.close()


def entrypoint() -> None:
    """Console-script entrypoint (see pyproject.toml)."""
    asyncio.run(main())


if __name__ == "__main__":
    entrypoint()
