# ADR-001: Dynamic Watchlist Selection via Earnings Calendar

**Status:** Accepted
**Date:** 2026-03-30

---

## Context

The system monitors a fixed set of tickers defined in the `WATCHLIST` environment variable
(default: `["AAPL","MSFT","GOOGL","AMZN","TSLA"]`). Operators must manually edit `.env`
and restart the process to change which tickers are active.

The pipeline's highest-value signal types — `EARN_PRE`, `EARN_BEAT`, `EARN_MISS` — depend
on tickers being present in the watchlist before their earnings date. A static list means
operators either over-provision (monitoring tickers with no imminent catalyst) or miss
companies with upcoming earnings that were never added.

---

## Decision

Replace the static watchlist as the live source of truth with a **database-backed selection**
driven by a monthly earnings calendar scan. The static `settings.watchlist` is retained as
a **fallback** for when no DB selection exists.

### Key design choices

1. **Operator-driven, not fully automatic** — the system surfaces candidates; a human
   confirms which tickers to activate. Full automation (auto-add any company reporting in
   30 days) was considered and rejected: it would silently expand the universe to hundreds
   of tickers, inflating API costs and risk exposure.

2. **SQLite persistence, not in-memory** — the selection survives process restarts and is
   visible to all agents without coordination overhead.

3. **No pipeline restart required** — `WatchlistManager.get_active_watchlist()` reads from
   SQLite on each call. Agents see the updated list on the next polling cycle after the
   operator runs the CLI.

4. **`settings.watchlist` kept as fallback** — removing it would break first-run and
   environments where the CLI has never been used. It remains the seed value until a DB
   selection exists.

---

## Consequences

### Positive
- Watchlist stays aligned with the earnings calendar automatically each month.
- Operators no longer need to edit `.env` or restart the service.
- EARN_PRE signals are generated for the right tickers at the right time.
- Phase 2 (per-ticker assessment) can be added to the CLI without any pipeline changes.

### Negative / trade-offs
- Adds a new service (`WatchlistManager`), a new ORM table, and a new CLI entrypoint.
- Three agents (`NewsIngestorAgent`, `SentimentAnalystAgent`, `EarningsCalendarAgent`) gain
  a new constructor dependency — slightly more wiring in `pipeline.py` and `main.py`.
- If the operator never runs the CLI, behaviour is identical to today (fallback to
  `settings.watchlist`). The new capability is opt-in.

---

## Implementation Plan

See [`dynamic-watchlist-spec.md`](./dynamic-watchlist-spec.md) for the full file-by-file
change log, interface sketches, and CLI behaviour.

### Summary of changes

| File | Type | Change |
|---|---|---|
| `services/watchlist_manager.py` | New | Core service — scan, load, save, get |
| `cli/select_watchlist.py` | New | Interactive CLI for operators |
| `services/tables.py` | Modified | Add `WatchlistSelectionRow` ORM table |
| `models/calendar.py` | Modified | Add `is_candidate` computed field (1–31 day window) |
| `agents/news_ingestor.py` | Modified | Inject `WatchlistManager`; replace 2 watchlist reads |
| `agents/sentiment_analyst.py` | Modified | Inject `WatchlistManager`; replace 1 watchlist read |
| `agents/earnings_calendar.py` | Modified | Inject `WatchlistManager`; replace 1 watchlist read |
| `graph/pipeline.py` | Modified | Construct + inject `WatchlistManager` |
| `main.py` | Modified | Construct `WatchlistManager`; wire into cron agents |
| `config.py` | Unchanged | `watchlist` field kept as fallback seed |

---

## Alternatives Considered

| Alternative | Reason rejected |
|---|---|
| Fully automatic universe expansion (all companies reporting in 30 days) | Uncontrolled cost and risk growth; no human oversight |
| File-based selection (write tickers to a `.txt`) | Less robust than SQLite; harder to query; no timestamp audit trail |
| Reload `settings.watchlist` from `.env` at runtime | `.env` is not designed as a live config file; requires process signals or polling |
| Separate microservice / admin UI | Over-engineered for current scale; a CLI is sufficient |

---

## Phase 2 — Per-Ticker Assessment

When added, `WatchlistManager.scan_candidates()` will enrich each `EarningsCalendarEntry`
with a `TickerAssessment` (sector, 52-week return, analyst consensus, historical beat rate
from `Stage1Repository`). This data is only surfaced in the CLI to inform the operator's
choice — no pipeline agents change.
