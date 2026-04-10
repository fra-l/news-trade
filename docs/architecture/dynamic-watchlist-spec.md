# Dynamic Watchlist Selection — Architecture Spec

**Status:** Done
**Date:** 2026-03-30
**Implemented:** commit `c748597` on branch `claude/review-next-feature-4TiO9`

---

## Problem Statement

The current watchlist is static: a `list[str]` set in `.env` (default `["AAPL","MSFT","GOOGL","AMZN","TSLA"]`).
It never changes unless the operator manually edits the environment and restarts the process.
This means the system cannot self-organise around the earnings calendar — the highest-signal
events in the pipeline are EARN_PRE/BEAT/MISS, but only for tickers someone remembered to add.

The goal is to let operators scan the next ~30 days of earnings reports and interactively
pick which tickers to activate, without touching `.env` or restarting the process.

---

## Proposed Design

### High-Level Flow

```
[CLI: select_watchlist.py]
        │
        │  scan next 30 days via CalendarProvider
        ▼
  List of EarningsCalendarEntry candidates
        │
        │  operator picks tickers (numbered prompt)
        ▼
  WatchlistManager.save_selection()
        │
        │  persists to SQLite (WatchlistSelectionRow)
        ▼
  Pipeline reads WatchlistManager.get_active_watchlist()
  on every cycle — no restart needed
```

### `settings.watchlist` role after the change

`settings.watchlist` is **not removed**. It becomes a fallback:

```python
def get_active_watchlist(self) -> list[str]:
    selected = self.load_selected()   # reads latest snapshot from SQLite
    if selected:
        return selected
    return self.settings.watchlist    # fallback: static .env value
```

This means:
- **First run / no DB entry yet** → static watchlist from `.env` is used unchanged.
- **After CLI selection** → DB snapshot takes over; `.env` value is bypassed.
- **DB selection cleared** → system automatically reverts to static list.

---

## File-by-File Change Log

### New files

| File | Purpose |
|---|---|
| `src/news_trade/services/watchlist_manager.py` | Core service: scan candidates, load/save selection, expose `get_active_watchlist()` |
| `src/news_trade/cli/select_watchlist.py` | Interactive CLI: prints candidate list, prompts operator to pick, calls `save_selection()` |

### Modified files

| File | Lines affected | Change |
|---|---|---|
| `src/news_trade/models/calendar.py` | `is_actionable` block (~L47–55) | Add `is_candidate` computed field: `1 <= days_until_report <= 31` for the broad monthly scan. `is_actionable` (2–5 day gate for EARN_PRE synthesis) is unchanged. |
| `src/news_trade/services/tables.py` | End of file | Add `WatchlistSelectionRow` ORM table (columns: `id`, `tickers` JSON, `saved_at` datetime). `load_selected()` reads the most recent row. |
| `src/news_trade/agents/news_ingestor.py` | L63, L100 | Replace `self.settings.watchlist` with `self._watchlist_manager.get_active_watchlist()`. Inject `WatchlistManager` in `__init__`. |
| `src/news_trade/agents/sentiment_analyst.py` | L38 | Same injection + replacement as above. |
| `src/news_trade/agents/earnings_calendar.py` | L65 | Same injection + replacement as above. |
| `src/news_trade/graph/pipeline.py` | Agent construction block | Construct one `WatchlistManager` instance; pass it into `NewsIngestorAgent`, `SentimentAnalystAgent`, and `EarningsCalendarAgent`. |
| `src/news_trade/main.py` | Settings + agent wiring | Construct `WatchlistManager` before the pipeline; wire into cron agents. |

### Unchanged files

| File | Why untouched |
|---|---|
| `providers/calendar/fmp.py` | Already accepts arbitrary `from_date`/`to_date` — no window is hardcoded |
| `providers/calendar/yfinance_provider.py` | Same as FMP |
| `agents/market_data.py` | Derives tickers from `news_events`, not directly from watchlist |
| `agents/signal_generator.py` | Operates on `sentiment_results` — no direct watchlist read |
| `agents/risk_manager.py` | No watchlist dependency |
| `agents/execution.py` | No watchlist dependency |
| `graph/state.py` | No state fields change |
| `config.py` | `watchlist` field kept as-is (fallback seed) |

---

## `WatchlistManager` Interface (sketch)

```python
class WatchlistManager:
    def __init__(
        self,
        settings: Settings,
        session: Session,
        primary: CalendarProvider,
        fallback: CalendarProvider,
    ) -> None: ...

    async def scan_candidates(
        self, from_date: date, to_date: date
    ) -> list[EarningsCalendarEntry]:
        """Return all entries where is_candidate=True in the given window."""

    def load_selected(self) -> list[str]:
        """Read the most recent saved selection from SQLite. Empty list if none."""

    def save_selection(self, tickers: list[str]) -> None:
        """Persist a new selection snapshot to SQLite."""

    def get_active_watchlist(self) -> list[str]:
        """Return saved selection, or settings.watchlist if no selection exists."""
```

---

## `select_watchlist.py` CLI behaviour (sketch)

```
$ uv run select-watchlist

Scanning earnings calendar: 2026-03-30 → 2026-04-30
Found 12 companies reporting in the next 30 days:

 #   Ticker  Report date   Timing        EPS est
─────────────────────────────────────────────────
 1   AAPL    2026-04-28    POST_MARKET    1.62
 2   MSFT    2026-04-23    POST_MARKET    2.94
 3   GOOGL   2026-04-22    POST_MARKET    2.01
 4   META    2026-04-22    POST_MARKET    5.25
 5   AMZN    2026-04-30    POST_MARKET    1.37
 6   NVDA    2026-04-25    POST_MARKET    0.89
...

Enter numbers to include (comma-separated), or ENTER to keep current selection:
> 1,2,4,6

Saved 4 tickers: AAPL, MSFT, META, NVDA
Active watchlist updated — no restart required.
```

---

## Phase 2 — Per-Ticker Assessment (future)

Not in scope for the initial implementation. When added, `scan_candidates()` will optionally
enrich each `EarningsCalendarEntry` with a `TickerAssessment` object displayed as an extra
column (or expandable detail) in the CLI:

| Field | Source |
|---|---|
| Sector / industry | yfinance `Ticker.info` |
| 52-week return | `MarketDataProvider` |
| Analyst consensus | yfinance or Massive |
| Historical beat rate | `Stage1Repository.load_historical_outcomes()` |
| Avg EPS surprise % | `FMPEstimatesProvider` |

No pipeline agents change for Phase 2 — assessment data is only used by the CLI to inform
the operator's selection decision.
