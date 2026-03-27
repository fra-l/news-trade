# services/ — Business Logic and Persistence Services

Services are pure business logic and data access layers. No agents live here.
All services receive dependencies via constructor injection.

---

## File Map

| File | Class | Purpose |
|---|---|---|
| `database.py` | — | Engine + session factory + `create_tables()` |
| `tables.py` | `Base`, 5 ORM classes | SQLAlchemy table definitions |
| `llm_client.py` | `LLMClient` (Protocol), `AnthropicLLMClient`, `LLMClientFactory` | Two-tier LLM routing |
| `estimates_renderer.py` | `EstimatesRenderer` | Deterministic FMP data → narrative formatter |
| `confidence_scorer.py` | `ConfidenceScorer` | 4-component weighted confidence scoring + gate |
| `stage1_repository.py` | `Stage1Repository` | Stage 1 position CRUD + outcome reflection |
| `event_bus.py` | `EventBus` | Async Redis pub/sub wrapper |

---

## `llm_client.py` — Two-Tier LLM Routing (Pattern B)

```python
factory = LLMClientFactory(settings)
factory.quick   # AnthropicLLMClient(haiku) — cheap, fast
factory.deep    # AnthropicLLMClient(sonnet) — accurate, slower
```

**Route to `quick`:** event_type classification, ticker extraction, dedup checks, debate rounds,
non-earnings sentiment (M&A, guidance, macro, analyst ratings, EARN_MIXED).
**Route to `deep`:** confidence scoring, debate verdict, EARN_PRE / EARN_BEAT / EARN_MISS sentiment.

`LLMClient.invoke()` is `async`. Pass `response_schema=MyModel` to get structured JSON output
(Anthropic tool-use internally). Every response includes `model_id` and `provider` for provenance.

Adding a second LLM provider requires one new class satisfying `LLMClient` Protocol +
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
calls `Base.metadata.create_all()` — no Alembic, safe to call on every startup.

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

## `database.py` — Session Factory

```python
engine  = build_engine(settings)
factory = build_session_factory(settings)   # sessionmaker[Session]
create_tables(settings)                     # idempotent — safe at startup
```

SQLite parent directories are created automatically if they don't exist.
For tests, use `create_engine("sqlite:///:memory:")` + `Base.metadata.create_all(engine)`.

---

## `event_bus.py` — Redis Pub/Sub

```python
bus = EventBus(settings)
await bus.publish("news_events", event)
await bus.subscribe("news_events", callback)
```

Used by `NewsIngestorAgent` (publish) and `EarningsCalendarAgent` (publish EARN_PRE synthetic events).
Redis URL from `settings.redis_url`.
