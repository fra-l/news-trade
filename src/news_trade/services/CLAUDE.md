# services/ — Business Logic and Persistence Services

Services are pure business logic and data access layers. No agents live here.
All services receive dependencies via constructor injection.

---

## File Map

| File | Class | Purpose |
|---|---|---|
| `database.py` | — | Engine + session factory + `create_tables()` |
| `tables.py` | `Base`, 6 ORM classes | SQLAlchemy table definitions |
| `llm_client.py` | `LLMClient` (Protocol), `AnthropicLLMClient`, `OllamaLLMClient`, `LLMClientFactory` | Two-tier LLM routing; `anthropic` or `ollama` backend |
| `estimates_renderer.py` | `EstimatesRenderer` | Deterministic FMP data → narrative formatter |
| `confidence_scorer.py` | `ConfidenceScorer` | 4-component weighted confidence scoring + gate |
| `stage1_repository.py` | `Stage1Repository` | Stage 1 position CRUD + outcome reflection |
| `session_reporter.py` | `SessionReporter` | Write JSON session reports on exit; read + summarise previous session on startup |
| `event_bus.py` | `EventBus` | Async Redis pub/sub wrapper |
| `telegram_bot.py` | `TelegramBotService` | Telegram operator interface — push notifications (drawdown halt, trade execution), read-only query commands (`/status`, `/portfolio`, `/signals`, `/help`), operator stop command (`/stop`) |

---

## `llm_client.py` — Two-Tier LLM Routing (Pattern B)

```python
factory = LLMClientFactory(settings)
factory.quick   # AnthropicLLMClient(haiku) or OllamaLLMClient(llama3.2:3b) — cheap, fast
factory.deep    # AnthropicLLMClient(sonnet) or OllamaLLMClient(llama3.1:8b) — accurate, slower
```

**Route to `quick`:** event_type classification, ticker extraction, dedup checks, debate rounds,
non-earnings sentiment (M&A, guidance, macro, analyst ratings, EARN_MIXED).
**Route to `deep`:** confidence scoring, debate verdict, EARN_PRE / EARN_BEAT / EARN_MISS sentiment.

`LLMClient.invoke()` is `async`. Pass `response_schema=MyModel` to get structured JSON output.
- **Anthropic** uses tool-use to force the schema.
- **Ollama** uses OpenAI-compatible function calling (`tools` + `tool_choice`) via the `openai` SDK
  pointed at `http://localhost:11434/v1`. Requires a model that supports function calling
  (Llama 3.1+, Qwen 2.5, Mistral).

Every response includes `model_id` and `provider` for provenance.

**Backend selection** is driven by `settings.llm_provider` (`"anthropic"` or `"ollama"`).
`settings.ollama_base_url` overrides the Ollama endpoint (default `http://localhost:11434/v1`).

Adding a third LLM provider requires one new class satisfying `LLMClient` Protocol +
one `case` in `_build_client()`. Zero agent changes required.

---

## `estimates_renderer.py` — Deterministic Narrative Formatter (Pattern C)

```python
renderer = EstimatesRenderer()
narrative = renderer.render(ticker, estimates_data)       # str block for LLM prompt
delta     = renderer.compute_pre_surprise_delta(data)     # float [-1.0, 1.0]
```

**Stateless** — no constructor params, no I/O, no LLM calls. Pre-computes surprise delta
so the LLM validates rather than computes it. Used by `ConfidenceScorer._score_surprise()`.

---

## `confidence_scorer.py` — Confidence Scoring + Gate (Pattern C)

```python
scorer = ConfidenceScorer(settings=settings, renderer=EstimatesRenderer())

score = scorer.score(
    event_type=EventType.EARN_BEAT,
    sentiment=sentiment_result,        # optional
    estimates=estimates_data,          # optional
    earnings_surprise=surprise,        # optional
    analyst_count=12,                  # optional
    source="benzinga",
)

signal = scorer.apply_gate(signal, event_type, score)
# Returns new TradeSignal via model_copy() with:
#   confidence_score=score, passed_confidence_gate=True/False, rejection_reason=...
```

Weights per `EventType` are a module-level dict keyed by the string value (e.g. `"earn_pre"`).
`EARN_MIXED` has all-zero weights and gate 1.01 — **always fails by design** (forces human review).
Default weight row `"_default"` covers any unlisted `EventType`.

**No LLM involvement** — fully deterministic and testable.

---

## `stage1_repository.py` — Stage 1 Positions + Outcome Reflection (Pattern D)

```python
repo = Stage1Repository(session)   # takes sync SQLAlchemy Session
```

**CRUD methods:**

| Method | Behaviour |
|---|---|
| `persist(position)` | Upsert on `(ticker, fiscal_quarter)` — safe to call on re-fired EARN_PRE |
| `load_open(ticker)` | Latest OPEN position for ticker, or `None` |
| `update_status(id, status)` | Raises `ValueError` if id not found |
| `load_expired()` | OPEN positions whose `expected_report_date` < today |
| `load_all_open()` | All OPEN positions — used by RiskManagerAgent concentration check |

**Reflection methods (Pattern D):**

| Method | Behaviour |
|---|---|
| `record_outcome(stage1_id, final_status, eps_pct, price_1d)` | Idempotent (unique constraint on `stage1_id`) |
| `load_historical_outcomes(ticker, lookback_quarters=8)` | Returns `HistoricalOutcomes`; `source='observed'` if ≥4 samples, else `source='fmp'` with `beat_rate=None` |

**Sync only** — the engine and session factory are sync SQLAlchemy. Do not wrap in `async def`.

---

## `tables.py` — ORM Table Definitions

All tables inherit `Base` (DeclarativeBase). `create_tables(settings)` in `database.py`
calls `alembic upgrade head` programmatically — **Alembic is the schema authority**.
Do not call `Base.metadata.create_all()` in production code; it bypasses Alembic.
For tests, use `Base.metadata.create_all(engine)` directly on an in-memory engine (intentional).

| Table | PK Type | Key Design Note |
|---|---|---|
| `news_events` | Integer autoincrement | `event_id` (String) is the dedup key, not `id` |
| `trade_signals` | Integer autoincrement | `signal_id` (String) is the business key |
| `stage1_positions` | **String (UUID)** | UUID set in application code before insert |
| `earnings_outcomes` | Integer autoincrement | `stage1_id` has `unique=True` for idempotency |
| `orders` | Integer autoincrement | `order_id` is the business key; `close_after_date` nullable Date column drives PEAD expiry |

`OrderRow.close_after_date` (nullable `Date`, indexed) is set at insert time by
`ExecutionAgent._log_order()` when the signal carries `horizon_days`. Upserts leave the
column unchanged. `ExecutionAgent.scan_expired_pead()` queries this column daily.

`stage1_positions` is the only table with a non-integer primary key. This is intentional —
the UUID is generated in `OpenStage1Position` before the row is inserted, so the Pydantic
model and the DB row share the same `id` without a round-trip.

DateTime defaults use `default=datetime.utcnow` (callable, not call). `updated_at` uses
`onupdate=datetime.utcnow` plus an explicit assignment in `update_status()` as belt-and-suspenders.

---

## `session_reporter.py` — Session Reports (Write + Read)

```python
reporter = SessionReporter()                        # default dir: data/sessions/
reporter = SessionReporter(sessions_dir=Path("…"))  # custom dir (tests)

# On exit — write JSON audit file
path = reporter.write(settings, session_start, cycle_count, errors, last_state, git_hash)

# On startup — read previous session
previous = reporter.load_latest()          # most recent, or None
previous = reporter.load(Path("…"))        # specific file
reporter.log_startup_summary(previous, current_commit)
```

Files are named `session_YYYYMMDD_HHMMSS.json` (timestamp-sortable). `find_latest()` uses
a lexicographic glob sort — no date parsing needed.

**`write()`** queries `OrderRow`, `TradeSignalRow`, and `OpenStage1PositionRow` for the
session window and writes a JSON dict containing:
`session_start`, `session_end`, `duration_seconds`, `version`, `commit`, `cycles_run`,
`system_halted`, `orders_placed`, `signals` (total/approved/rejected),
`open_stage1_positions`, `errors`.
On DB failure a partial record with an `"error"` key is written so there is always an audit
file on disk.

**`log_startup_summary()`** emits:
- `INFO` with the previous session's key metrics (cycles, orders, signals, open positions,
  version, commit).
- `WARNING` if `system_halted=True` — operator must acknowledge before resuming live trading.
- `WARNING` listing all previous-session errors.
- `INFO` if `commit` differs from `current_commit` — version change since last run.

**CLI flags** (see `main.py`):
- `--resume-session` — load latest session file on startup.
- `--session-file PATH` — load a specific file (implies `--resume-session`).

---

## `database.py` — Engine, Session Factory, and Migrations

```python
engine  = build_engine(settings)
factory = build_session_factory(settings)   # sessionmaker[Session]
create_tables(settings)                     # runs alembic upgrade head — call once at startup
```

`create_tables()` uses the Python Alembic API (`alembic.command.upgrade`) — no subprocess.
`_make_alembic_config(settings)` locates `alembic.ini` relative to `database.py` at
`Path(__file__).parents[3] / "alembic.ini"` (project root).

SQLite parent directories are created automatically by `build_engine()` if they don't exist.

**Schema changes:** edit `tables.py`, then run:
```bash
uv run alembic revision --autogenerate -m "describe_change"
```
Review the generated file in `alembic/versions/`, then commit. The next startup applies it.

**First deploy (existing pre-Alembic database):** run `uv run alembic stamp head` once before
restarting. See `DEPLOY.md` for the full procedure.

For tests, use `create_engine("sqlite:///:memory:")` + `Base.metadata.create_all(engine)`
directly (intentional bypass of Alembic — keeps tests fast and migration-file independent).

---

## `event_bus.py` — Redis Pub/Sub

```python
bus = EventBus(settings)
await bus.publish("news_events", event)
await bus.subscribe("news_events", callback)
```

Used by `NewsIngestorAgent` (publish) and `EarningsCalendarAgent` (publish EARN_PRE synthetic events).
Redis URL from `settings.redis_url`.

---

## `telegram_bot.py` — Operator Observer + Stop Control (Telegram)

```python
bot = TelegramBotService(settings, session_factory, stop_callback=None, get_state=None)
await bot.start(event_bus)   # call once at startup; no-op if token/chat_id unset
await bot.stop()             # call in the finally block
```

`stop_callback` is an optional `Callable[[], None]` invoked when the operator confirms the
stop via the inline button. In `main.py` it sets `shutdown_event` (exits the polling loop)
and `operator_stop_event` (triggers position cleanup in the `finally` block). When `None`,
`/stop` replies "Stop not available." and no buttons are shown.

The stop flow uses two `CallbackQueryHandler` instances registered in `start()`:
`_cb_stop_confirm` (pattern `"^stop_confirm$"`) and `_cb_stop_cancel` (pattern
`"^stop_cancel$"`). Both call `query.answer()` to clear the button spinner and
`query.edit_message_text()` to replace the confirmation message in-place. Auth is checked
via `callback_query.from_user.id` against `settings.telegram_chat_id`.

`get_state` is an optional `Callable[[], dict[str, Any]]` that returns a snapshot dict with
keys `"portfolio"` (`PortfolioState | None`) and `"system_halted"` (`bool`). In `main.py`
this is backed by `_state_ref`, updated after each `run_cycle()` call. When `None` or when
the returned `portfolio` is `None` (before the first cycle), `/status` shows a graceful
"waiting for first cycle" message.

**Disabled** when `settings.telegram_bot_token == ""` or `settings.telegram_chat_id == 0`.
Zero impact on non-Telegram deploys. The trading pipeline runs fully automatically —
the bot never blocks or influences trading decisions.

**Push notifications** (`_redis_listener` background task):

| Channel | Message |
|---|---|
| `system_halted` | "SYSTEM HALTED — drawdown limit breached (drawdown=X). All positions closed." |
| `trade_executed` | "Trade executed: AAPL BUY qty=10\nOrder ID: ...\nStatus: submitted" |

The `trade_executed` event is published by `ExecutionAgent` immediately after each order
is submitted. `EventBus.subscribe()` accepts multiple channel names in one call.

**Commands** (all reject requests from any chat_id other than `settings.telegram_chat_id`):

| Command | Behaviour |
|---|---|
| `/help` | List available commands |
| `/status` | Portfolio equity, daily P&L (+ %), drawdown, cash, buying power; open positions with per-position and total unrealized P&L; pending Stage 1 (EARN_PRE) positions from DB; prominent `*** SYSTEM HALTED ***` warning if halted |
| `/portfolio` | Today's order count (buys/sells split); open Stage 1 positions (ticker, direction, report date, size %); last 10 `OrderRow` entries |
| `/signals [N]` | Today's approved/rejected signal count; last N `TradeSignalRow` entries (default 5, max 20) |
| `/stop` | Sends a confirmation message with two inline buttons: **"Yes, stop & close all"** and **"Cancel"**. Tapping confirm cancels all pending Alpaca orders, closes all open positions, marks Stage 1 positions EXPIRED, then exits the polling loop; sends "Stop complete" when done. Tapping cancel dismisses silently. Both buttons are auth-checked against `telegram_chat_id`. |
