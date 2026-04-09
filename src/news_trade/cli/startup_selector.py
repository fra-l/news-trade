"""StartupSelector — interactive small-cap earnings ticker selection at startup.

Run once at the start of each ``news-trade`` session.  Fetches the earnings
calendar for the next 14 days, filters to small-cap companies by market cap,
and asks the operator which tickers to analyse this session.

In non-interactive mode (no TTY — CI, piped input, ``--once`` in automation)
the top-N tickers by nearest report date are selected automatically.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date
from typing import TYPE_CHECKING

import yfinance as yf  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from news_trade.config import Settings
    from news_trade.models.calendar import EarningsCalendarEntry
    from news_trade.providers.base import CalendarProvider

logger = logging.getLogger(__name__)

# Semaphore cap for concurrent yfinance market-cap lookups
_MARKET_CAP_CONCURRENCY = 20

# Scan window used by main.py; exposed here so tests can import it
SCAN_DAYS = 14


class StartupSelector:
    """Fetch small-cap earnings candidates and prompt the operator to select tickers."""

    def __init__(self, settings: Settings, calendar_provider: CalendarProvider) -> None:
        self._settings = settings
        self._calendar = calendar_provider

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_candidates(
        self,
        from_date: date,
        to_date: date,
    ) -> list[tuple[EarningsCalendarEntry, int | None]]:
        """Return small-cap earnings entries in [from_date, to_date].

        Each element is ``(entry, market_cap_usd | None)``.
        Entries with unknown market cap are included (shown with "?" in the table).
        Entries whose market cap exceeds the configured ceiling are excluded.
        Results are sorted by ``entry.report_date`` ascending.
        """
        logger.info(
            "Fetching earnings calendar %s → %s via %s",
            from_date, to_date, self._calendar.name,
        )
        try:
            entries = await self._calendar.get_upcoming_earnings([], from_date, to_date)
        except Exception as exc:
            logger.warning("Calendar provider failed: %s — no candidates", exc)
            return []

        if not entries:
            logger.info("No earnings found in window %s → %s", from_date, to_date)
            return []

        # Deduplicate by ticker — keep earliest report date per ticker
        unique: dict[str, EarningsCalendarEntry] = {}
        for e in entries:
            if e.ticker not in unique or e.report_date < unique[e.ticker].report_date:
                unique[e.ticker] = e
        deduped = list(unique.values())

        logger.info("Fetching market cap for %d tickers…", len(deduped))
        cap_map = await self._fetch_market_caps([e.ticker for e in deduped])

        ceiling = self._settings.small_cap_max_market_cap_usd
        result: list[tuple[EarningsCalendarEntry, int | None]] = []
        for entry in deduped:
            cap = cap_map.get(entry.ticker)
            if cap is not None and cap > ceiling:
                continue  # too large — exclude
            result.append((entry, cap))

        result.sort(key=lambda t: t[0].report_date)
        logger.info(
            "Found %d small-cap candidates (ceiling $%sB)",
            len(result),
            ceiling / 1_000_000_000,
        )
        return result

    async def prompt_selection(
        self,
        candidates: list[tuple[EarningsCalendarEntry, int | None]],
    ) -> list[str]:
        """Display candidates and return the operator-selected ticker list.

        In non-interactive mode, auto-selects the first ``max_startup_tickers``
        by nearest report date (all if limit is -1).
        """
        limit = self._settings.max_startup_tickers

        if not candidates:
            logger.warning(
                "No small-cap earnings candidates found — running with no tickers."
            )
            return []

        _print_table(candidates)

        if not sys.stdin.isatty():
            chosen = _auto_select(candidates, limit)
            tickers = [e.ticker for e, _ in chosen]
            logger.info(
                "Non-interactive mode: auto-selected %d ticker(s): %s",
                len(tickers),
                tickers,
            )
            print(
                f"\n[non-interactive] Auto-selected {len(tickers)} ticker(s): "
                + ", ".join(tickers)
            )
            return tickers

        # Interactive prompt
        limit_str = str(limit) if limit != -1 else "unlimited"
        print(
            f"\nLimit: {limit_str} ticker(s). "
            "Enter numbers (comma-separated) or ENTER to take top-"
            + (str(limit) if limit != -1 else "all")
            + ":"
        )
        print("> ", end="", flush=True)

        try:
            raw = sys.stdin.readline().strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""

        if not raw:
            chosen = _auto_select(candidates, limit)
            tickers = [e.ticker for e, _ in chosen]
            print(f"Selected top-{len(tickers)}: {', '.join(tickers)}")
            return tickers

        selected: list[tuple[EarningsCalendarEntry, int | None]] = []
        for part in raw.split(","):
            part = part.strip()
            if not part.isdigit():
                continue
            idx = int(part) - 1
            if 0 <= idx < len(candidates):
                entry_cap = candidates[idx]
                if entry_cap not in selected:
                    selected.append(entry_cap)

        if not selected:
            print("No valid selection — falling back to top-N.")
            selected = _auto_select(candidates, limit)

        # Enforce limit
        if limit != -1 and len(selected) > limit:
            print(
                f"Warning: selected {len(selected)} but limit is {limit}; "
                f"truncating to first {limit}."
            )
            selected = selected[:limit]

        tickers = [e.ticker for e, _ in selected]
        print(f"Selected {len(tickers)} ticker(s): {', '.join(tickers)}")
        return tickers

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_market_caps(self, tickers: list[str]) -> dict[str, int | None]:
        """Fetch market cap for each ticker concurrently via yfinance."""
        sem = asyncio.Semaphore(_MARKET_CAP_CONCURRENCY)

        async def _one(ticker: str) -> tuple[str, int | None]:
            async with sem:
                try:
                    cap = await asyncio.to_thread(_get_market_cap, ticker)
                    return ticker, cap
                except Exception as exc:
                    logger.debug("market cap lookup failed for %s: %s", ticker, exc)
                    return ticker, None

        results = await asyncio.gather(*[_one(t) for t in tickers])
        return dict(results)


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _get_market_cap(ticker: str) -> int | None:
    """Synchronous yfinance market-cap lookup (runs in a thread pool)."""
    info = yf.Ticker(ticker).info
    cap = info.get("marketCap")
    if cap is None:
        return None
    return int(cap)


def _auto_select(
    candidates: list[tuple[EarningsCalendarEntry, int | None]],
    limit: int,
) -> list[tuple[EarningsCalendarEntry, int | None]]:
    """Return the first ``limit`` candidates (all if limit is -1)."""
    if limit == -1:
        return list(candidates)
    return list(candidates[:limit])


def _fmt_cap(cap: int | None) -> str:
    """Format market cap as a human-readable string."""
    if cap is None:
        return "?"
    b = cap / 1_000_000_000
    if b >= 1:
        return f"${b:.1f}B"
    m = cap / 1_000_000
    return f"${m:.0f}M"


def _fmt_eps(eps: float | None) -> str:
    return f"{eps:.2f}" if eps is not None else "—"


def _print_table(candidates: list[tuple[EarningsCalendarEntry, int | None]]) -> None:
    header = (
        f"{'#':>3}  {'Ticker':<7}  {'Report':<12}  "
        f"{'Timing':<12}  {'EPS est':>7}  {'Mkt Cap':>8}"
    )
    sep = "─" * len(header)
    print(f"\n{sep}")
    print(header)
    print(sep)
    for i, (entry, cap) in enumerate(candidates, 1):
        timing = entry.timing.value if entry.timing else "UNKNOWN"
        print(
            f"{i:>3}  {entry.ticker:<7}  {entry.report_date!s:<12}  "
            f"{timing:<12}  {_fmt_eps(entry.eps_estimate):>7}  {_fmt_cap(cap):>8}"
        )
    print(sep)
