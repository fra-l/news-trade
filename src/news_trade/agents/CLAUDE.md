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
| `RiskManagerAgent` | `risk_manager.py` | **STUB — all methods raise `NotImplementedError`** |
| `ExecutionAgent` | `execution.py` | **Done — Alpaca paper trading integration** |
| `EarningsCalendarAgent` | `earnings_calendar.py` | **Done — daily cron, outside LangGraph pipeline** |
| `ExpiryScanner` | `expiry_scanner.py` | **TODO** |
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
| `SignalGeneratorAgent` | `sentiment_results`, `market_context` | `trade_signals` |
| `RiskManagerAgent` | `trade_signals`, `portfolio` | `approved_signals`, `rejected_signals` |
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

Accepts `llm: LLMClientFactory` at construction time (no other service deps yet).

### What is implemented (Pattern A)

| Method | Purpose |
|---|---|
| `run(state)` | Iterates `sentiment_results`, pairs with `market_context`, calls `_build_signal()`, then `_debate_signal()` for gate-passed signals |
| `_build_signal(sentiment, market_ctx)` | Maps label → direction; computes conviction; applies `min_signal_conviction` threshold; returns `TradeSignal` or `None` |
| `_compute_position_size(ticker, conviction, volatility)` | Volatility-adjusted heuristic: `max(1, int(conviction / max(vol, 0.01) * 10))` |
| `_compute_stop_loss(entry, volatility, direction)` | 2× daily vol proxy offset from entry; LONG → below, SHORT → above |
| `_debate_signal(signal)` | Bull/bear debate gate — skips if `signal_debate_rounds=0` or below `signal_debate_threshold`; applies CONFIRM/REDUCE/REJECT verdict |

Prompt helpers are module-level functions: `_build_bull_prompt`, `_build_bear_prompt`,
`_build_synthesis_prompt`. The synthesis uses `response_schema=_DebateVerdictSchema`
(structured output via tool-use).

### What is NOT yet implemented in `SignalGeneratorAgent`

The following EARN_* logic is deferred to a future PR (requires `ConfidenceScorer` and
`Stage1Repository` injection):
- `EARN_PRE` — size from `historical_beat_rate`, persist `OpenStage1Position`
- `EARN_BEAT/MISS` — load open Stage 1 position, confirm/reverse
- `EARN_MIXED` — emit EXIT signal (ConfidenceScorer gate 1.01 always fails — by design)

See `docs/architecture/event-driven-signal-layer.md §3` for the full decision tree.

## Stub Agents

### `RiskManagerAgent`

Five check layers (fail-fast, in order):
1. `passed_confidence_gate` — reject if False
2. Drawdown halt — reject + set `system_halted=True` if portfolio drawdown ≥ `max_drawdown_pct`
3. Concentration limit — reject if `open_positions >= max_open_positions` (Stage 2 ADD exempt)
4. Pending order conflict — reject if ticker already has a pending order
5. Position size cap — reduce `size_pct` to `max_position_pct` (soft limit, not reject)

Inject `stage1_repo.load_all_open()` for the concentration check.
See `docs/architecture/event-driven-signal-layer.md §7`.

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
