# Deployment Readiness — Paper Trading on a VM

**Status:** Pre-deployment gap analysis
**Scope:** Issues identified by cross-referencing the running code against the architectural
intent. All items below were found by reading `main.py`, `graph/pipeline.py`,
`agents/signal_generator.py`, `agents/risk_manager.py`, `agents/execution.py`,
`providers/market/yfinance.py`, and `config.py` directly.

---

## Issue Index

| # | Title | Severity | Depends on |
|---|---|---|---|
| 28 | Portfolio state never fetched from Alpaca | **P0 — Blocker** | — |
| 29 | `EARN_MIXED` CLOSE signal sends `qty=0` to broker | **P0 — Blocker** | — |
| 30 | `entry_price=None` on all `TradeSignal`s — L3b size cap dead | **P0 — Blocker** | — |
| 31 | `last_poll` never persisted between cycles | **P1 — High** | — |
| 32 | RSS event classification is coarse — two-stage PEAD never fires | **P1 — High** | — |
| 33 | `max_total_positions` is dead/orphaned config | **P2 — Medium** | — |
| 34 | No market hours guard on `ExecutionAgent` | **P2 — Medium** | #28 |
| 35 | SQLite concurrent sessions between pipeline and cron | **P2 — Medium** | — |

---

## P0 — Blockers (must fix before any paper trading)

---

### Issue 28: Portfolio state never fetched from Alpaca

**File:** `main.py`, `graph/pipeline.py`, `agents/risk_manager.py`

**Problem:**

Every pipeline cycle starts with `initial_state: PipelineState = {}`. Nothing in the
pipeline fetches the live Alpaca account state into `state["portfolio"]`.
`RiskManagerAgent.run()` falls back to:

```python
portfolio: PortfolioState = state.get(
    "portfolio", PortfolioState(equity=0.0, cash=0.0)
)
```

With `equity=0.0` and an empty `positions` list, every risk check that depends on
portfolio state is silently disabled:

| Check | Effect of empty portfolio |
|---|---|
| L2a drawdown halt | `0.0 < max_drawdown_pct` → always passes; halt never triggers |
| L2b concentration (`open_count`) | `portfolio.position_count = 0` → Alpaca positions never counted |
| L3b size cap | `equity=0.0` → `_apply_size_cap` short-circuits; no cap enforced |
| L3c direction conflict | `positions=[]` → no conflict ever detected; opposing positions can stack |

**Fix direction:**

Add a `PortfolioFetcherAgent` (or inline call) as the first step of each pipeline cycle.
It should call `TradingClient.get_all_positions()` + `TradingClient.get_account()` via
`asyncio.to_thread`, build a `PortfolioState` from the results, and write it to
`state["portfolio"]`. This agent runs before `NewsIngestorAgent` and always succeeds
(errors produce an empty portfolio + WARNING, never abort the cycle).

**Deliverables:**

- `agents/portfolio_fetcher.py` — new `PortfolioFetcherAgent`
- `providers/base.py` — optional `BrokerProvider` Protocol (or inline Alpaca call)
- `graph/pipeline.py` — wire as first node; add unconditional edge to `NEWS`
- `graph/state.py` — `portfolio` key already exists; no schema change needed
- `tests/test_portfolio_fetcher.py` — mock Alpaca + empty account edge cases

---

### Issue 29: `EARN_MIXED` CLOSE signal sends `qty=0` to Alpaca

**File:** `agents/signal_generator.py:_handle_earn_mixed()`,
`agents/execution.py:_submit_order()`

**Problem:**

`_handle_earn_mixed()` emits:

```python
TradeSignal(
    direction=SignalDirection.CLOSE,
    suggested_qty=0,   # ← intent: close the whole position
    ...
)
```

`ExecutionAgent._submit_order()` passes this directly to
`MarketOrderRequest(qty=0, ...)`, which Alpaca rejects with an API error. The exception
is caught and logged, but the Stage1 position has already been marked `EXITED` in SQLite.
The DB and broker diverge: the system believes the position is closed; Alpaca still holds
it.

**Fix direction:**

For `SignalDirection.CLOSE` signals, `ExecutionAgent._submit_order()` should call
`TradingClient.close_position(symbol)` via `asyncio.to_thread` instead of constructing a
`MarketOrderRequest`. This is already done in `scan_expired_pead()` for PEAD closures —
the same pattern applies here.

Alternatively (simpler): if `suggested_qty == 0`, look up the actual held qty from the
portfolio before submitting:

```python
if signal.direction == SignalDirection.CLOSE and signal.suggested_qty == 0:
    await asyncio.to_thread(self._alpaca.close_position, signal.ticker)
    return ...  # build Order from response
```

**Deliverables:**

- `agents/execution.py` — handle `qty=0` CLOSE signals via `close_position()`
- `tests/test_execution.py` — assert CLOSE with `qty=0` calls `close_position`, not
  `submit_order`

---

### Issue 30: `entry_price=None` on all `TradeSignal`s — L3b size cap never fires

**File:** `agents/signal_generator.py`, `agents/risk_manager.py:_apply_size_cap()`

**Problem:**

Every signal path sets `entry_price=None`:

```python
# generic path (_build_signal):
TradeSignal(..., entry_price=None, ...)

# EARN_PRE path (_handle_earn_pre):
entry_price = market_ctx.latest_close  # used for stop_loss and Stage1 only
TradeSignal(..., entry_price=None, ...)  # ← TradeSignal still None

# EARN_BEAT/MISS path:
TradeSignal(..., entry_price=None, ...)
```

`_apply_size_cap()` short-circuits when `entry_price is None`, so no cap is ever
enforced. Combined with issue #28 (equity=0.0), `_compute_position_size()` produces
unbounded integer share counts:

```python
max(1, int(conviction / max(volatility, 0.01) * 10))
# example: conviction=0.75, vol=0.20 → 37 shares
# TSLA at $250 → $9,250 per position with no portfolio context
```

**Fix direction:**

Set `entry_price=market_ctx.latest_close` on all `TradeSignal` instances in
`_build_signal()`, `_handle_earn_pre()`, and `_handle_earn_post()`. This is the price
used for stop-loss calculations anyway — it should also be on the signal so the risk
layer can enforce the size cap.

Note: this fix only becomes meaningful after issue #28 (equity is populated). Both
should land in the same PR.

**Deliverables:**

- `agents/signal_generator.py` — populate `entry_price=market_ctx.latest_close`
  in all three signal-building paths
- `tests/test_signal_generator.py` — assert `entry_price` is set on emitted signals
- `tests/test_risk_rules.py` — add integration test confirming cap fires end-to-end
  once equity is non-zero

---

## P1 — High (fix before extended paper trading)

---

### Issue 31: `last_poll` never persisted between cycles

**File:** `main.py`, `agents/news_ingestor.py`

**Problem:**

`NewsIngestorAgent.run()` reads `state.get("last_poll")` to bound the news query window,
but never writes it back. `main.py` creates a fresh `initial_state = {}` every cycle.
So `last_poll` is always `None` — every 30-second cycle fetches the full unwindowed
news feed.

Consequences:
- Every cycle re-processes articles already seen; the SQLite dedup prevents re-trading
  but Claude is still called for every article within the provider's default window
- `CLAUDE_DAILY_BUDGET_USD=2.00` burns down much faster than intended
- Under a news burst, the cycle latency can grow as the Claude queue fills

**Fix direction:**

Two changes needed:

1. `NewsIngestorAgent.run()` should include `"last_poll": datetime.now(UTC)` in its
   return dict.

2. `main.py` should carry `last_poll` forward:

```python
last_poll = None
while True:
    initial_state: PipelineState = {"last_poll": last_poll}  # type: ignore
    state = await run_cycle(pipeline, initial_state)
    last_poll = state.get("last_poll")
    await asyncio.sleep(settings.news_poll_interval_sec)
```

**Deliverables:**

- `agents/news_ingestor.py` — write `last_poll` to return dict
- `main.py` — thread `last_poll` across loop iterations
- `tests/test_news_ingestor.py` — assert `last_poll` is present in the return dict

---

### Issue 32: RSS event classification is coarse — two-stage PEAD logic never fires

**File:** `agents/news_ingestor.py`, `agents/signal_generator.py`

**Problem:**

`NewsIngestorAgent` classifies events using the 8-type coarse `EventType` taxonomy
(`EARNINGS`, `MERGER_ACQUISITION`, etc.) via a keyword lookup table. A post-earnings
headline like "Apple beats EPS by 3%, raises guidance" maps to `EventType.EARNINGS`.

`SignalGeneratorAgent._build_signal()` dispatches to the two-stage handlers only on
fine-grained types (`EARN_BEAT`, `EARN_MISS`, `EARN_MIXED`). An `EARNINGS` event falls
through to the generic label-based path. This means:

- The entire two-stage PEAD flow only triggers for `EarningsCalendarAgent`-synthesised
  `EARN_PRE` events
- Real post-earnings news from RSS is handled as a generic sentiment signal — no Stage1
  confirmation, no PEAD horizon, no `stage1_id` linkage
- The highest-value strategy in the system never runs on live RSS news

**Fix direction:**

Two viable approaches:

**Option A (recommended):** Have `ClaudeSentimentProvider` return the fine-grained
`event_type` as part of `SentimentResult`, and have `SentimentAnalystAgent` update the
`NewsEvent.event_type` in state before `SignalGeneratorAgent` runs.

**Option B (simpler):** In `NewsIngestorAgent._classify_event_type()`, add a secondary
pass that inspects quantitative signals in the headline (e.g. "beat", "miss", "topped
estimates", "fell short") to emit fine-grained types directly, without LLM involvement.
Less accurate than Option A but zero added API cost.

**Deliverables (Option A):**

- `models/sentiment.py` — add `classified_event_type: EventType | None` to
  `SentimentResult`
- `providers/sentiment/claude.py` — extract fine-grained event type from LLM response
- `agents/sentiment_analyst.py` — patch `news_event.event_type` in state if the
  provider returned a finer classification
- `tests/test_providers.py` — assert fine-grained type is returned for earnings prompts

---

## P2 — Medium (address before live trading)

---

### Issue 33: `max_total_positions` is dead / orphaned config

**File:** `config.py`

**Problem:**

`config.py` defines both:

```python
max_total_positions: int = Field(default=10, ...)  # never referenced
max_open_positions:  int = Field(default=5,  ...)  # used by RiskManagerAgent
```

`max_total_positions` is never read anywhere in the codebase. A user tuning the system
who sets `MAX_TOTAL_POSITIONS=3` in `.env` would believe they capped exposure at 3
positions when the actual cap is `MAX_OPEN_POSITIONS=5`.

**Fix direction:**

Remove `max_total_positions` from `Settings`. Update `CLAUDE.md` and `.env.example` if
it appears there.

**Deliverables:**

- `config.py` — remove `max_total_positions`
- verify no references in tests or other files

---

### Issue 34: No market hours guard on `ExecutionAgent`

**File:** `agents/execution.py`, `graph/pipeline.py`

**Problem:**

The pipeline runs 24/7 on a 30-second interval. `ExecutionAgent` submits
`time_in_force=DAY` market orders unconditionally. Outside market hours, Alpaca queues
these orders; they execute at the next session open when the triggering catalyst and
market context may be hours or days stale.

Weekend news can generate signals that execute Monday at 09:30 ET using Friday's closing
price and volatility estimate. For fast-moving catalysts (earnings beats, M&A
announcements), the gap between signal generation and execution destroys the edge.

**Fix direction:**

Add a market-hours check at the top of `ExecutionAgent.run()`:

```python
clock = await asyncio.to_thread(self._alpaca.get_clock)
if not clock.is_open:
    self.logger.info("Market closed — skipping order submission (%d signals)", ...)
    return {"orders": [], "errors": []}
```

`CLOSE` / `EARN_MIXED` exit signals should bypass this guard — closing a position should
always be allowed.

This depends on issue #28 (the Alpaca client is already wired in); no new dependency
needed.

**Deliverables:**

- `agents/execution.py` — market hours check at start of `run()`; bypass for CLOSE
- `tests/test_execution.py` — mock `get_clock()` returning closed; assert no orders

---

### Issue 35: SQLite concurrent sessions between pipeline and cron

**File:** `main.py`, `graph/pipeline.py`

**Problem:**

`main.py` creates two separate SQLAlchemy `Session` objects pointing at the same SQLite
file:

- `shared_session` (created in `build_pipeline`) — used by `SignalGeneratorAgent` and
  `RiskManagerAgent` every 30 seconds
- `cron_session` (created in `main.py`) — used by `EarningsCalendarAgent`,
  `ExpiryScanner`, and `pead_exec_agent`

SQLite allows only one writer at a time. If the 07:15 `ExpiryScanner` cron fires while
the pipeline loop is mid-transaction, SQLite raises `OperationalError: database is
locked`. The cron catches and logs the error but the expiry scan silently fails —
leaving stale OPEN Stage1 positions that inflate the concentration check.

**Fix direction:**

Enable WAL (Write-Ahead Logging) mode on engine creation, which allows concurrent
readers and greatly reduces writer-writer contention:

```python
from sqlalchemy import event as sa_event

@sa_event.listens_for(engine, "connect")
def set_wal_mode(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA busy_timeout=5000")  # 5s retry on lock
```

Add this to `database.py:build_engine()` when the dialect is SQLite.

**Deliverables:**

- `services/database.py` — enable WAL + `busy_timeout` for SQLite engines
- `tests/` — no new tests needed; existing tests use in-memory SQLite (unaffected)

---

## Implementation Order

```
P0 (must be parallel, all needed for safe operation):
  #28  PortfolioFetcherAgent           ← no deps; unblocks #29 market-hours and #30 cap
  #29  CLOSE signal qty=0 fix          ← no deps (independent of portfolio)
  #30  entry_price on TradeSignal      ← depends on #28 for cap to be meaningful

P1 (run in either order):
  #31  last_poll persistence           ← no deps; independent cost/dedup improvement
  #32  Fine-grained event classification ← no deps; enables PEAD for live news

P2 (can land anytime, low risk):
  #33  Remove max_total_positions      ← trivial cleanup
  #34  Market hours guard              ← depends on #28 (Alpaca client available)
  #35  SQLite WAL mode                 ← trivial; no app logic change
```

The minimum viable set for starting a supervised paper trading session is **#28 + #29**:
portfolio state populated so risk checks are real, and CLOSE signals that actually close
positions. Issues #30, #31, and #32 should follow within the same week to prevent
runaway sizing and wasted API budget.
