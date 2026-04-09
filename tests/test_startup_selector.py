"""Unit tests for StartupSelector.

All tests mock yfinance and the CalendarProvider — no real network calls.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from news_trade.cli.startup_selector import (
    StartupSelector,
    _auto_select,
    _fmt_cap,
    _fmt_eps,
)
from news_trade.models.calendar import EarningsCalendarEntry, ReportTiming

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs: object) -> MagicMock:
    m = MagicMock()
    m.small_cap_max_market_cap_usd = 2_000_000_000
    m.max_startup_tickers = 5
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _make_entry(
    ticker: str = "XYZ",
    days: int = 3,
    eps: float | None = 1.23,
) -> EarningsCalendarEntry:
    return EarningsCalendarEntry(
        ticker=ticker,
        report_date=date.today() + timedelta(days=days),
        fiscal_quarter="Q1 2026",
        fiscal_year=2026,
        timing=ReportTiming.PRE_MARKET,
        eps_estimate=eps,
    )


def _make_provider(entries: list[EarningsCalendarEntry]) -> AsyncMock:
    provider = AsyncMock()
    provider.name = "mock_calendar"
    provider.get_upcoming_earnings = AsyncMock(return_value=entries)
    return provider


# ---------------------------------------------------------------------------
# fetch_candidates
# ---------------------------------------------------------------------------


class TestFetchCandidates:
    async def test_returns_entries_below_cap_ceiling(self):
        entries = [_make_entry("SMALL", days=3), _make_entry("BIG", days=4)]
        settings = _make_settings(small_cap_max_market_cap_usd=1_000_000_000)
        provider = _make_provider(entries)
        selector = StartupSelector(settings, provider)

        # SMALL has $500M cap, BIG has $5B cap
        caps = {"SMALL": 500_000_000, "BIG": 5_000_000_000}

        to_date = date.today() + timedelta(days=14)
        with patch.object(
            selector, "_fetch_market_caps", new=AsyncMock(return_value=caps)
        ):
            result = await selector.fetch_candidates(date.today(), to_date)

        tickers = [e.ticker for e, _ in result]
        assert "SMALL" in tickers
        assert "BIG" not in tickers

    async def test_includes_unknown_market_cap_entries(self):
        entries = [_make_entry("UNK", days=2)]
        settings = _make_settings()
        provider = _make_provider(entries)
        selector = StartupSelector(settings, provider)

        to_date = date.today() + timedelta(days=14)
        unk_caps: dict[str, int | None] = {"UNK": None}
        with patch.object(
            selector, "_fetch_market_caps", new=AsyncMock(return_value=unk_caps)
        ):
            result = await selector.fetch_candidates(date.today(), to_date)

        assert len(result) == 1
        assert result[0][0].ticker == "UNK"
        assert result[0][1] is None

    async def test_sorted_by_report_date(self):
        entries = [
            _make_entry("C", days=5), _make_entry("A", days=1), _make_entry("B", days=3)
        ]
        settings = _make_settings()
        provider = _make_provider(entries)
        selector = StartupSelector(settings, provider)

        caps = {"A": 0, "B": 0, "C": 0}
        to_date = date.today() + timedelta(days=14)
        with patch.object(
            selector, "_fetch_market_caps", new=AsyncMock(return_value=caps)
        ):
            result = await selector.fetch_candidates(date.today(), to_date)

        tickers = [e.ticker for e, _ in result]
        assert tickers == ["A", "B", "C"]

    async def test_deduplicates_by_ticker(self):
        """Only earliest entry is kept when provider returns same ticker twice."""
        entry1 = _make_entry("AAPL", days=2)
        entry2 = _make_entry("AAPL", days=5)
        provider = _make_provider([entry1, entry2])
        settings = _make_settings()
        selector = StartupSelector(settings, provider)

        to_date = date.today() + timedelta(days=14)
        caps = {"AAPL": 500_000_000}
        with patch.object(
            selector, "_fetch_market_caps", new=AsyncMock(return_value=caps)
        ):
            result = await selector.fetch_candidates(date.today(), to_date)

        assert len(result) == 1
        assert result[0][0].report_date == entry1.report_date  # earliest kept

    async def test_returns_empty_on_provider_failure(self):
        provider = AsyncMock()
        provider.name = "failing"
        provider.get_upcoming_earnings = AsyncMock(
            side_effect=RuntimeError("network error")
        )
        settings = _make_settings()
        selector = StartupSelector(settings, provider)

        to_date = date.today() + timedelta(days=14)
        result = await selector.fetch_candidates(date.today(), to_date)

        assert result == []

    async def test_returns_empty_when_no_entries(self):
        provider = _make_provider([])
        settings = _make_settings()
        selector = StartupSelector(settings, provider)

        to_date = date.today() + timedelta(days=14)
        result = await selector.fetch_candidates(date.today(), to_date)

        assert result == []


# ---------------------------------------------------------------------------
# prompt_selection — non-interactive mode
# ---------------------------------------------------------------------------


class TestPromptSelectionNonInteractive:
    async def test_auto_selects_top_n_when_not_tty(self):
        candidates = [
            (_make_entry("A", days=1), 100_000_000),
            (_make_entry("B", days=2), 200_000_000),
            (_make_entry("C", days=3), 300_000_000),
            (_make_entry("D", days=4), 400_000_000),
            (_make_entry("E", days=5), 500_000_000),
            (_make_entry("F", days=6), 600_000_000),
        ]
        settings = _make_settings(max_startup_tickers=3)
        provider = AsyncMock()
        selector = StartupSelector(settings, provider)

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            result = await selector.prompt_selection(candidates)

        assert result == ["A", "B", "C"]

    async def test_auto_selects_all_when_limit_is_minus_one(self):
        candidates = [(_make_entry(str(i), days=i + 1), None) for i in range(10)]
        settings = _make_settings(max_startup_tickers=-1)
        provider = AsyncMock()
        selector = StartupSelector(settings, provider)

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            result = await selector.prompt_selection(candidates)

        assert len(result) == 10

    async def test_returns_empty_on_no_candidates(self):
        settings = _make_settings()
        selector = StartupSelector(settings, AsyncMock())

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            result = await selector.prompt_selection([])

        assert result == []


# ---------------------------------------------------------------------------
# prompt_selection — interactive mode
# ---------------------------------------------------------------------------


class TestPromptSelectionInteractive:
    async def test_enter_selects_top_n(self):
        labels = ["A", "B", "C", "D", "E", "F"]
        candidates = [(_make_entry(t, days=i + 1), None) for i, t in enumerate(labels)]
        settings = _make_settings(max_startup_tickers=3)
        selector = StartupSelector(settings, AsyncMock())

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.return_value = "\n"
            result = await selector.prompt_selection(candidates)

        assert result == ["A", "B", "C"]

    async def test_user_picks_specific_numbers(self):
        labels = ["A", "B", "C", "D"]
        candidates = [(_make_entry(t, days=i + 1), None) for i, t in enumerate(labels)]
        settings = _make_settings(max_startup_tickers=5)
        selector = StartupSelector(settings, AsyncMock())

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.return_value = "2, 4\n"
            result = await selector.prompt_selection(candidates)

        assert result == ["B", "D"]

    async def test_enforces_max_startup_tickers(self):
        """Selecting more than the limit silently truncates to limit."""
        candidates = [(_make_entry(str(i), days=i + 1), None) for i in range(6)]
        settings = _make_settings(max_startup_tickers=2)
        selector = StartupSelector(settings, AsyncMock())

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.return_value = "1, 2, 3, 4\n"
            result = await selector.prompt_selection(candidates)

        assert len(result) == 2

    async def test_invalid_input_falls_back_to_top_n(self):
        candidates = [
            (_make_entry(t, days=i + 1), None) for i, t in enumerate(["A", "B", "C"])
        ]
        settings = _make_settings(max_startup_tickers=2)
        selector = StartupSelector(settings, AsyncMock())

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.return_value = "abc, xyz\n"
            result = await selector.prompt_selection(candidates)

        assert result == ["A", "B"]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestFormatHelpers:
    def test_fmt_cap_none(self):
        assert _fmt_cap(None) == "?"

    def test_fmt_cap_billions(self):
        assert _fmt_cap(1_500_000_000) == "$1.5B"

    def test_fmt_cap_millions(self):
        assert _fmt_cap(250_000_000) == "$250M"

    def test_fmt_eps_none(self):
        assert _fmt_eps(None) == "—"

    def test_fmt_eps_positive(self):
        assert _fmt_eps(1.23) == "1.23"


class TestAutoSelect:
    def test_respects_limit(self):
        candidates = [(_make_entry(str(i)), None) for i in range(10)]
        assert len(_auto_select(candidates, 3)) == 3

    def test_unlimited(self):
        candidates = [(_make_entry(str(i)), None) for i in range(10)]
        assert len(_auto_select(candidates, -1)) == 10

    def test_limit_larger_than_list(self):
        candidates = [(_make_entry("A"), None)]
        assert len(_auto_select(candidates, 5)) == 1
