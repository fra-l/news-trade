# agents/ — LangGraph Agent Implementations

Each agent is a node in the LangGraph pipeline. Agents receive `PipelineState`,
compute their stage, write results back, and return the updated state.

---

## Implementation Status

| Agent | File | Status |
|---|---|---|
| `NewsIngestorAgent` | `news_ingestor.py` | Done |
| `MarketDataAgent` | `market_data.py` | Done |
| `SentimentAnalystAgent` | `sentiment_analyst.py` | Done |
| `SignalGeneratorAgent` | `signal_generator.py` | **Done — Pattern A implemented** |
| `RiskManagerAgent` | `risk_manager.py` | **Done — five-layer fail-fast checks; risk_dry_run mode** |
| `ExecutionAgent` | `execution.py` | **Done — Alpaca paper trading integration** |
| `EarningsCalendarAgent` | `earnings_calendar.py` | **Done — daily cron, outside LangGraph pipeline** |
| `ExpiryScanner` | `expiry_scanner.py` | **Done — daily cron, marks OPEN positions EXPIRED** |
| `OrchestratorAgent` | `orchestrator.py` | Not used — pipeline built via `graph/pipeline.py` directly |

---

## BaseAgent Contract (`base.py`)

```python
class BaseAgent(ABC):
    def __init__(self, settings: Settings, event_bus: EventBus) -> None: ...
    self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def run(self, state: dict) -> dict: ...
```

All agents inherit `BaseAgent`. Additional dependencies (providers, db session,
repositories) are injected in the subclass `__init__` — never fetched from globals.

---

## PipelineState Keys — What Each Agent Reads / Writes

| Agent | Reads | Writes |
|---|---|---|
| `NewsIngestorAgent` | `last_poll` | `news_events`, `errors` |
| `MarketDataAgent` | `news_events` | `market_context` (dict[ticker → MarketSnapshot]) |
| `SentimentAnalystAgent` | `news_events`, `estimates` (optional) | `sentiment_results` |
| `SignalGeneratorAgent` | `sentiment_results`, `market_context`, `news_events` | `trade_signals` |
| `RiskManagerAgent` | `trade_signals`, `portfolio` | `approved_signals`, `rejected_signals`, `system_halted` |
| `ExecutionAgent` | `approved_signals` | `orders` |
| `EarningsCalendarAgent` | — (reads `settings.watchlist`) | `news_events`, `estimates`, `errors` |

`EarningsCalendarAgent` runs **outside** the main pipeline on a daily cron. It publishes
synthetic `EARN_PRE` events to Redis; the pipeline picks them up as regular news.

Full `PipelineState` schema lives in `graph/state.py`.

---

## Pipeline Topology

```
NewsIngestorAgent
    │ news_events empty? → END
    ↓
MarketDataAgent → SentimentAnalystAgent → SignalGeneratorAgent → RiskManagerAgent
                                                                      │ no approved signals? → END
                                                                      ↓
                                                               ExecutionAgent → END
```

Routing logic: `graph/pipeline.py` — `_has_news_events()` and `_has_approved_signals()`.

---

## Adding a New Agent

1. Subclass `BaseAgent` in a new file.
2. Accept provider/service dependencies in `__init__` (not via globals).
3. Read from `state`, write results back, return updated `state`.
4. Register the new node in `graph/pipeline.py` and add it to `build_pipeline()`.
5. Add routing edge if the agent can short-circuit downstream.
6. Raise `NotImplementedError` for unimplemented sub-methods (never silently pass).

---

## Implemented: `SignalGeneratorAgent`

Accepts `llm: LLMClientFactory` and `scorer: ConfidenceScorer` at construction time.

### What is implemented (Pattern A)

| Method | Purpose |
|---|---|
| `run(state)` | Iterates `sentiment_results`, pairs with `market_context`, builds `event_lookup` dict from `news_events`, calls `_build_signal()`, then `_debate_signal()` for gate-passed signals |
| `_build_signal(sentiment, market_ctx, event_lookup)` | Maps label → direction; computes conviction; applies `min_signal_conviction` threshold; calls `scorer.score()` + `scorer.apply_gate()` to set `confidence_score` and `passed_confidence_gate`; returns `TradeSignal` or `None` |
| `_compute_position_size(ticker, conviction, volatility)` | Volatility-adjusted heuristic: `max(1, int(conviction / max(vol, 0.01) * 10))` |
| `_compute_stop_loss(entry, volatility, direction)` | 2× daily vol proxy offset from entry; LONG → below, SHORT → above |
| `_debate_signal(signal)` | Bull/bear debate gate — skips if `signal_debate_rounds=0` or below `signal_debate_threshold`; applies CONFIRM/REDUCE/REJECT verdict |

Prompt helpers are module-level functions: `_build_bull_prompt`, `_build_bear_prompt`,
`_build_synthesis_prompt`. The synthesis uses `response_schema=_DebateVerdictSchema`
(structured output via tool-use).

### EARN_\* two-stage logic (Pattern D) — Done

`Stage1Repository` is now injected at construction time (`stage1_repo: Stage1Repository`).
`_build_signal()` dispatches to three dedicated handlers before the generic label-based path:

| Handler | Trigger | Logic |
|---|---|---|
| `_handle_earn_pre()` | `EARN_PRE` | Loads `load_historical_outcomes(ticker)`; uses `beat_rate` if `source='observed'`, else `settings.earn_default_beat_rate`; skips if outside [0.55, 0.85]; sizes position [0.25–0.40]; persists `OpenStage1Position`; emits LONG/SHORT signal with `stage1_id` |
| `_handle_earn_post()` | `EARN_BEAT` / `EARN_MISS` | Loads `load_open(ticker)`; if agrees → `update_status(CONFIRMED)`, add remaining size; if disagrees → `update_status(REVERSED)`, full reverse; if no Stage 1 → fresh PEAD at 75% |
| `_handle_earn_mixed()` | `EARN_MIXED` | Loads `load_open(ticker)`; if open → `update_status(EXITED)`, emit CLOSE signal with `passed_confidence_gate=True`; if no Stage 1 → return None |

`run()` now also reads `estimates: dict[str, EstimatesData]` from state and passes it to
`_build_signal()`. `_parse_calendar_fields()` (module-level helper) extracts `report_date`
and `fiscal_quarter` from `estimates[ticker]` first, then parses the event headline as
fallback, then defaults to `today+3 / "unknown"`.

EARN_MIXED CLOSE signals bypass the confidence gate (`passed_confidence_gate=True`) because
the gate for EARN_MIXED is 1.01 by design — exiting a position must not be blocked.

See `docs/architecture/event-driven-signal-layer.md §3` for the full decision tree.

## Implemented: `RiskManagerAgent` ✅

Constructor: `settings`, `event_bus`, `stage1_repo: Stage1Repository`.

Five check layers (fail-fast, in order):

| Layer | Check | Action |
|---|---|---|
| L1 | `passed_confidence_gate` | REJECT using `signal.rejection_reason` |
| L2a | `portfolio.max_drawdown_pct >= settings.max_drawdown_pct` (non-EXIT) | REJECT + set `system_halted=True` + publish `SYSTEM_HALTED` to event_bus |
| L2b | `open_count >= settings.max_open_positions` (non-EXIT) | REJECT; `open_count = len(stage1_repo.load_all_open()) + portfolio.position_count` |
| L3a | ticker in `{s.ticker for s in approved_so_far}` | REJECT (within-batch dedup) |
| L3b | position value > `equity * max_position_pct` | WARN only — `suggested_qty` model; no hard reject yet |
| L3c | existing position has opposite direction | REJECT |

`settings.risk_dry_run=True` runs all checks and logs, but moves every signal to
`approved_signals` regardless (calibration mode).

`_evaluate(signal, portfolio, open_count, approved_so_far)` returns
`(passed: bool, reason: str | None, RiskValidation | None)` — call once per signal.

### `ExecutionAgent` — Done

Wraps Alpaca via `alpaca-py`. Constructor receives optional `TradingClient` and `Session`
(both `None`-safe for tests). Translates `approved_signals` into `MarketOrderRequest` objects,
wraps the synchronous Alpaca SDK in `asyncio.to_thread()`, maps responses to internal `Order`
models, and upserts every order to `OrderRow` via the injected `Session`.

| Method | Purpose |
|---|---|
| `run(state)` | Iterates `approved_signals`, calls `_submit_order()` per signal, logs each result |
| `_submit_order(signal, portfolio)` | Builds `MarketOrderRequest`, calls Alpaca, returns `Order` |
| `_sync_order_status(order)` | Polls Alpaca for updated status; updates `filled_qty`, `filled_avg_price` |
| `_cancel_order(order)` | Cancels order on Alpaca; returns updated `Order` with `CANCELLED` status |
| `_log_order(order)` | Upserts `OrderRow` to SQLite; no-op if `session=None` |

Module-level helpers: `_signal_to_order_side()` (maps direction + portfolio → `OrderSide`),
`_alpaca_to_order()` (maps alpaca-py `Order` → internal `Order`; unknown statuses fall back
to `SUBMITTED`).

---

### `EarningsCalendarAgent` ✅ Done

Runs **outside** the LangGraph pipeline on a daily cron (07:00 ET Mon–Fri).

Key dependencies to inject:
- `primary: CalendarProvider` — `FMPCalendarProvider` (preferred; has `eps_estimate` + timing)
- `fallback: CalendarProvider` — `YFinanceCalendarProvider` (no API key, used if FMP fails)
- `engine: Engine` — SQLAlchemy engine for dedup check against `NewsEventRow`

Core logic:
- Scans `today → today + 5 days` for watchlist tickers
- Filters to `entry.is_actionable` (2–5 days ahead)
- Builds `EstimatesData` per ticker via `_build_estimates(entry)` (skips entries where
  `eps_estimate is None`); populates `state["estimates"]` regardless of dedup status
- Deduplicates events via `event_id = f"calendar_earn_pre_{ticker}_{report_date}"`
- Synthesises `NewsEvent(event_type=EARN_PRE, source="earnings_calendar")`
- Publishes to Redis; persists to `NewsEventRow`
- Falls back to `YFinanceCalendarProvider` if primary returns empty or raises

The `estimates` dict in state is consumed by `SentimentAnalystAgent` → `ClaudeSentimentProvider`
to inject `EstimatesRenderer.render()` into the EARN_PRE prompt (Sentiment LLM routing Phase 2).

Use `get_calendar_provider(settings)` from `providers/__init__.py` to obtain the
primary provider. Always pass `YFinanceCalendarProvider()` as the fallback.

See `docs/architecture/event-driven-signal-layer.md §6` for the full specification.

---

### `ExpiryScanner` ✅ Done

Runs alongside `EarningsCalendarAgent` on the daily cron at 07:15 ET Mon-Fri.

Constructor: `settings: Settings`, `event_bus: EventBus`, `stage1_repo: Stage1Repository`

`run(state)`:
1. `stage1_repo.load_expired()` — all OPEN positions whose `expected_report_date < today`
2. For each: `stage1_repo.update_status(pos.id, Stage1Status.EXPIRED)` + WARNING log
3. Returns `{"errors": []}` — does not mutate other pipeline state keys

No network calls. No LLM calls. Pure DB read + write.

Note: `EventBus.publish` requires a `BaseModel`; STAGE1_EXPIRED has no downstream consumer
yet so publishing is omitted. The WARNING log serves as the audit trail.

---

### Cron scheduler wiring (`main.py`) ✅ Done

`main.py` uses `APScheduler` (`AsyncIOScheduler`) to run both cron agents without blocking
the main polling loop:

```
EarningsCalendarAgent  — cron, hour=7, minute=0,  day_of_week="mon-fri", misfire_grace_time=300
ExpiryScanner          — cron, hour=7, minute=15, day_of_week="mon-fri", misfire_grace_time=300
```

Both agents share a dedicated DB engine + session (separate from the pipeline's session).
`scheduler.start()` is called before the `while True` loop; `scheduler.shutdown(wait=False)`
runs in the `finally` block alongside `event_bus.close()`.
