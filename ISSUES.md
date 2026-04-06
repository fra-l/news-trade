# Issues & Phase Tracking

---

## ~~Issue 3: Add typed MarketSnapshot Pydantic model~~ ✅ Done

**Priority:** P1 — Should-have
**Depends on:** None
**Labels:** `models`, `typing`

Implemented in `src/news_trade/models/market.py`.  Phase 0 extended the
model with two additional optional fields:

- `atr_14d: float | None` — 14-day Average True Range in dollars
- `relative_volume: float | None` — today's volume divided by 20-day average volume

`PipelineState.market_context` is typed as `dict[str, MarketSnapshot]`.

---

## ~~Issue 4: Add unit tests for Pydantic models and pipeline graph~~ ✅ Done

**Priority:** P0 — Must-have
**Depends on:** #3 (MarketSnapshot)
**Labels:** `testing`

Implemented in commit `a7d022b`:

- `tests/test_models.py` — 44 tests covering all Pydantic models (including new `atr_14d` / `relative_volume` fields)
- `tests/test_pipeline.py` — 10 tests for `build_pipeline()` and routing helpers
- `tests/test_risk_rules.py` — 39 tests covering all five check layers, `run()` integration, Stage 2 ADD exemption, and L3b size cap
- `tests/test_providers.py` — 25 tests for Protocol compliance, factory functions, `KeywordSentimentProvider` logic, and `Settings` enums

Total: 504 passing tests.

---

## ~~Issue 5: Implement NewsIngestorAgent end-to-end~~ ✅ Done

**Priority:** P1 — Should-have
**Depends on:** None (ORM and async event bus are already implemented)
**Labels:** `agent`, `feature`

Phase 0 refactored `NewsIngestorAgent` to accept an injected `NewsProvider`
instead of calling Benzinga/Polygon directly.  Provider-specific HTTP logic
lives in `providers/news/benzinga.py` and `providers/news/rss.py`.

- `run()` — delegates fetch to `self._provider`, deduplicates, persists, publishes
- `_is_duplicate()`, `_matches_watchlist()`, `_persist()` — unchanged
- `_classify_event_type()` / `_parse_dt()` — module-level helpers retained for backward compatibility
- `tests/test_news_ingestor.py` — 27 tests updated to use a mock provider fixture

---

## ~~Issue 7: Add `docker-compose.yml` for Redis~~ ✅ Done

**Priority:** P2 — Nice-to-have
**Depends on:** None
**Labels:** `infrastructure`, `dx`

Implemented in commit `a13a504`:

- `docker-compose.yml` — Redis 7-alpine service on port 6379

---

## ~~Issue 8: Add `py.typed` marker~~ ✅ Done

**Priority:** P2 — Nice-to-have
**Depends on:** None
**Labels:** `typing`

Implemented: `src/news_trade/py.typed` (empty PEP 561 marker file) so downstream
consumers get type-checking support.

---

## ~~Issue 9: Add GitHub Actions CI workflow~~ ✅ Done

**Priority:** P2 — Nice-to-have
**Depends on:** #4 (tests exist)
**Labels:** `ci`, `dx`

Implemented in commit `a7d022b` as `.github/workflows/tests.yml`:
runs `uv sync --extra dev` + `uv run pytest tests/ -v` on every pull request.

---

## ~~Phase 0: Provider Abstraction Layer~~ ✅ Done

**Commit:** `e6efcc9`
**Branch:** `claude/provider-abstraction-layer-XED3B`
**Labels:** `architecture`, `refactor`, `feature`

Establishes a provider abstraction layer so the pipeline can swap between
free-tier and premium data sources via configuration, without touching agent logic.

### Deliverables

| # | Task | Files |
|---|------|-------|
| 1 | `atr_14d` + `relative_volume` on `MarketSnapshot` | `models/market.py` |
| 2 | `NewsProvider`, `MarketDataProvider`, `SentimentProvider` Protocols | `providers/base.py` |
| 3 | RSS, Benzinga news providers | `providers/news/rss.py`, `providers/news/benzinga.py` |
| 4 | yfinance, Polygon free, Polygon paid market providers | `providers/market/` |
| 5 | Claude (with budget cap), keyword sentiment providers | `providers/sentiment/` |
| 6 | Provider factory functions | `providers/__init__.py` |
| 7 | `NewsProviderType`, `MarketDataProviderType`, `SentimentProviderType` enums | `config.py` |
| 8 | Cost-control settings (`claude_daily_budget_usd`, `sentiment_dry_run`, `news_keyword_prefilter`) | `config.py`, `.env.example` |
| 9 | Agent DI refactor — `NewsIngestorAgent`, `MarketDataAgent`, `SentimentAnalystAgent` | `agents/` |
| 10 | Pipeline wiring via factory | `graph/pipeline.py` |
| 11 | 25 new provider + settings tests | `tests/test_providers.py` |

### Design decisions

- **Protocols over ABCs** — structural subtyping; providers need no inheritance
- **Factory with `match/case`** — three injection points; no DI framework needed
- **Daily budget cap** — `ClaudeSentimentProvider` tracks per-day token spend and falls back to neutral when the cap is hit
- **Keyword pre-filter** — `SentimentAnalystAgent` strips non-watchlist events before the Claude call to reduce cost
- **Default stack is free-tier** — `NEWS_PROVIDER=rss`, `MARKET_DATA_PROVIDER=yfinance`, `SENTIMENT_PROVIDER=claude`

---

## ~~Pattern B: LLM Client Abstraction Layer (deep/quick split)~~ ✅ Done

**Priority:** P1 — High
**Depends on:** Phase 0 Provider Layer
**Labels:** `architecture`, `feature`, `cost-control`

Implemented in commit `e7eee33` on branch `claude/review-trading-spec-vmi3B`.

Introduces a provider-agnostic `LLMClient` Protocol and `LLMClientFactory` that
routes calls to a cheap quick tier (Haiku) or an accurate deep tier (Sonnet),
reducing Anthropic API spend for high-throughput tasks.

### Deliverables

| # | Task | Files |
|---|------|-------|
| 1 | `LLMResponse`, `LLMClient` Protocol, `AnthropicLLMClient`, `LLMClientFactory` | `services/llm_client.py` |
| 2 | `llm_provider`, `llm_quick_model`, `llm_deep_model` settings | `config.py` |
| 3 | `provider` field on `SentimentResult` | `models/sentiment.py` |
| 4 | `model_id` + `provider` fields on `TradeSignal` | `models/signals.py` |
| 5 | Refactor `ClaudeSentimentProvider` to accept `LLMClient`; propagate provenance to all results | `providers/sentiment/claude.py` |
| 6 | Wire `LLMClientFactory.deep` into `ClaudeSentimentProvider` factory | `providers/__init__.py` |
| 7 | 19 unit tests | `tests/test_llm_client.py` |

### Design decisions

- **`LLMClient.invoke()` is async** — matches the project's async-first convention; the spec pseudocode used `def` but that was pseudocode only
- **`LLMResponse` exposes `input_tokens` / `output_tokens`** — required so `ClaudeSentimentProvider` can continue its existing daily budget tracking logic
- **Budget tracking stays in `ClaudeSentimentProvider`** — cost control is a domain concern of the sentiment provider, not a generic LLM client concern
- **`ClaudeSentimentProvider` accepts `LLMClient` (not `LLMClientFactory`)** — it always uses the deep client; the factory chooses the tier at the injection point in `providers/__init__.py`
- **Structured output via tool-use** — `AnthropicLLMClient` uses Anthropic tool-use JSON extraction when `response_schema` is provided; consistent with the Claude API's reliable structured-output pattern

### Completed (from spec §3.4 checklist)

- Steps 6–8 (previously deferred): `ClaudeSentimentProvider` now accepts `LLMClientFactory`
  directly and selects the tier per event inside `_select_client()` — deep (Sonnet) for
  `EARN_PRE/BEAT/MISS/EARNINGS`, quick (Haiku) for all other types. `OrchestratorAgent` is
  unused (pipeline built via `graph/pipeline.py`). `SentimentAnalystAgent` is unchanged —
  routing lives entirely inside the provider. All implemented in the Sentiment LLM Routing
  phase (see `docs/architecture/sentiment-llm-routing-spec.md`).

---

---

## ~~Pattern A: Bull/Bear Debate in SignalGeneratorAgent~~ ✅ Done

**Priority:** P1 — High
**Depends on:** Pattern B (LLMClientFactory)
**Labels:** `architecture`, `feature`, `cost-control`

Implemented in commit `ea99350` on branch `claude/review-trading-spec-kRPmw`.

Implements the full `SignalGeneratorAgent` (replacing the stub) and adds an optional
bull/bear LLM debate gate for high-confidence signals. Disabled by default
(`signal_debate_rounds=0`) to keep API costs flat during development.

### Deliverables

| # | Task | Files |
|---|------|-------|
| 1 | `DebateRound`, `DebateVerdict`, `DebateResult` models | `models/signals.py` |
| 2 | `debate_result: DebateResult | None` field on `TradeSignal` | `models/signals.py` |
| 3 | `signal_debate_rounds`, `signal_debate_model`, `signal_debate_threshold` settings | `config.py` |
| 4 | `SignalGeneratorAgent.run()` — pairs sentiment with market context, emits `TradeSignal` | `agents/signal_generator.py` |
| 5 | `_build_signal()` — label→direction mapping, conviction threshold, qty/stop-loss | `agents/signal_generator.py` |
| 6 | `_compute_position_size()`, `_compute_stop_loss()` | `agents/signal_generator.py` |
| 7 | `_debate_signal()` — bull/bear rounds (quick model) + synthesis verdict (deep model) | `agents/signal_generator.py` |
| 8 | Prompt helpers: `_build_bull_prompt`, `_build_bear_prompt`, `_build_synthesis_prompt` | `agents/signal_generator.py` |
| 9 | Wire `LLMClientFactory` into `SignalGeneratorAgent` in pipeline | `graph/pipeline.py` |
| 10 | 9 model tests (`TestDebateModels`) | `tests/test_models.py` |
| 11 | 22 agent tests across 4 classes | `tests/test_signal_generator.py` |

### Design decisions

- **`signal_debate_rounds=0` default** — feature off by default; no API spend change until
  explicitly enabled in `.env`
- **Two threshold guards** — debate skipped if disabled OR if `confidence_score` is below
  `signal_debate_threshold`; the second guard prevents cheap debate calls on weak signals
- **Verdict applied via `model_copy()`** — `TradeSignal` is mutable; REDUCE halves qty,
  REJECT flips `passed_confidence_gate=False` and sets `rejection_reason`
- **EARN_PRE / EARN_BEAT / EARN_MISS logic deferred** — requires `ConfidenceScorer` and
  `Stage1Repository` injection; deferred to a follow-up PR to keep this PR focused

---

## ~~Issues #10, #11, #12: EarningsCalendarAgent — model, providers, agent~~ ✅ Done

**Priority:** P1 — High
**Depends on:** Stage1Repository (Pattern D — done), NewsEvent, EventType.EARN_PRE
**Labels:** `agent`, `feature`, `calendar`

Implements the earnings calendar integration specified in
`docs/architecture/event-driven-signal-layer.md §6`.

### Deliverables

| # | Task | Files |
|---|------|-------|
| 10 | `EarningsCalendarEntry` model + `ReportTiming` StrEnum | `models/calendar.py` |
| 11 | `FMPCalendarProvider` + `YFinanceCalendarProvider` | `providers/calendar/fmp.py`, `providers/calendar/yfinance_provider.py` |
| 12 | `EarningsCalendarAgent` with dedup guard + primary/fallback chain | `agents/earnings_calendar.py` |
| — | `CalendarProvider` Protocol | `providers/base.py` |
| — | `get_calendar_provider()` factory | `providers/__init__.py` |
| — | `fmp_api_key` setting | `config.py` |
| — | 31 unit tests | `tests/test_earnings_calendar.py` |

### Design decisions

- **Primary/fallback chain** — `FMPCalendarProvider` is preferred (has `eps_estimate` and `timing`).
  If it returns empty or raises, the agent falls back to `YFinanceCalendarProvider` transparently.
- **Lazy imports** — `aiohttp` and `yfinance` are imported inside methods so missing stubs do
  not break the import graph when those libraries are absent.
- **Dedup via NewsEventRow** — the same SQLite table used by `NewsIngestorAgent`; identical
  `event_id` format ensures EARN_PRE events fired by calendar and by real news don't duplicate.
- **`is_actionable` window 2–5 days** — below 2: IV already elevated; above 5: signal decays.
- **Cron wiring in `main.py` (issue #13) is out of scope** — covered as a separate task.

---

---

## ~~Dynamic Watchlist Selection~~ ✅ Done

**Priority:** P1 — High
**Depends on:** Phase 0 Provider Layer (CalendarProvider), Pattern D (Stage1Repository for Phase 2)
**Branch:** `claude/review-next-feature-4TiO9`
**Labels:** `feature`, `dx`, `operators`

Adds runtime watchlist management so operators can scan the next 30 days of earnings via an
interactive CLI and activate tickers without editing `.env` or restarting the process.

### Deliverables

| # | Task | Files |
|---|------|-------|
| 1 | `is_candidate` computed field (1–31 day window) | `models/calendar.py` |
| 2 | `WatchlistSelectionRow` ORM table | `services/tables.py` |
| 3 | `WatchlistManager` — scan, load, save, get_active_watchlist | `services/watchlist_manager.py` |
| 4 | `select-watchlist` interactive CLI | `cli/select_watchlist.py`, `cli/__init__.py` |
| 5 | `select-watchlist` entry point | `pyproject.toml` |
| 6 | `WatchlistManager` injection in 3 agents | `agents/news_ingestor.py`, `agents/sentiment_analyst.py`, `agents/earnings_calendar.py` |
| 7 | Pipeline + main wiring | `graph/pipeline.py`, `main.py` |
| 8 | 18 unit tests + 6 model tests + agent injection tests | `tests/test_watchlist_manager.py`, `tests/test_earnings_calendar.py`, `tests/test_news_ingestor.py` |

### Design decisions

- **Append-only rows** — `save_selection()` never overwrites; each CLI run adds a new `WatchlistSelectionRow`. Audit trail preserved; `load_selected()` reads the most-recent row.
- **`settings.watchlist` as fallback** — behaviour is identical to before if the CLI is never run. The new capability is fully opt-in.
- **`watchlist_manager` optional in `EarningsCalendarAgent`** — backward-compatible default `None`; falls back to `settings.watchlist`. Required in `NewsIngestorAgent` and `SentimentAnalystAgent` (always injected in pipeline wiring).
- **Separate sessions** — `pipeline.py` creates a dedicated `wl_session` for `WatchlistManager` (independent of `shared_session` used by `Stage1Repository`). `main.py` shares `cron_session`.

---

---

## Issue 28: Agent self-learning — close the feedback loop beyond EARN_PRE

**Priority:** P2 — Nice-to-have
**Depends on:** Pattern D (Stage1Repository), Pattern A (DebateResult on TradeSignal), ExecutionAgent (OrderRow)
**Labels:** `feature`, `learning`, `architecture`

The system already has one self-learning loop (Pattern D): `Stage1Repository` records
EARN_PRE outcomes and, after ≥4 quarters per ticker, replaces static FMP beat rates
with the system's own observed beat rate.  All other signal types and scoring components
are fully static — weights and gates are hand-tuned constants that never update.

### Gaps to close

| Gap | Location | Impact |
|---|---|---|
| `ConfidenceScorer` weights (`_WEIGHTS`) are hardcoded constants | `services/confidence_scorer.py` | Surprise/sentiment/coverage/source weights never adapt based on realised P&L |
| `ConfidenceScorer` gate thresholds (`_GATES`) are hardcoded | `services/confidence_scorer.py` | Gate thresholds are hand-tuned; no data-driven calibration over time |
| `_SOURCE_SCORES` are static | `services/confidence_scorer.py` | A source proven consistently wrong (e.g. Reddit) is never demoted automatically |
| Non-earnings signals have no outcome recording | No equivalent of `record_outcome()` for M&A, guidance, regulatory, etc. | P&L data for these event types is never persisted for future use |
| Pattern A debate verdicts are audit-only | `TradeSignal.debate_result` | CONFIRM/REDUCE/REJECT verdicts are stored but never fed back to improve future debate prompts or thresholds |
| `ConfidenceScorer.score()` has no memory of which score ranges led to profitable trades | `services/confidence_scorer.py` | Gate thresholds remain static regardless of observed hit-rate per score bucket |

### Proposed approach (high level)

1. **Generic outcome table** — extend `EarningsOutcomeRow` (or add a new `SignalOutcomeRow`)
   to record final P&L, direction correctness, and confidence score for every filled order,
   keyed on `signal_id`.  `ExecutionAgent` writes the row when an order closes.

2. **Per-event-type hit-rate tracking** — after N outcomes per `EventType`, compute the
   empirical precision (correct direction / total) per confidence bucket (e.g. 0.5–0.6,
   0.6–0.7, …).  Feed this into a gate calibration step (e.g. shift the gate up/down by
   0.02 per period).

3. **Source credibility decay** — compute per-source precision over a rolling window; update
   `_SOURCE_SCORES` from DB at startup (DB values override the hardcoded defaults).

4. **Debate verdict calibration** — track the P&L of signals that received REDUCE or REJECT
   verdicts (compared to what would have happened without the debate) to assess whether the
   debate gate is adding value and tune `signal_debate_threshold`.

### Out of scope for this issue

- Full online gradient-based learning (would require a different architecture)
- Model fine-tuning or prompt optimisation via RL
- Changing the LangGraph pipeline structure

---

## Dependency graph

```
#3 MarketSnapshot ✅ ──► #4 Tests ✅
#4 Tests ✅ ───────────► #9 CI ✅
#5 NewsIngestorAgent ✅ (no remaining deps — ORM and event bus done)
#7 docker-compose ✅    (independent)
#8 py.typed ✅          (independent)
Phase 0 Provider Layer ✅ (depends on #3, #5)
Pattern B ✅ ──────────► Pattern A ✅
#10 EarningsCalendarEntry ✅ ──► #11 Calendar providers ✅ ──► #12 EarningsCalendarAgent ✅
Dynamic Watchlist ✅    (depends on Phase 0 CalendarProvider + Phase 0 tables)
```

All patterns (A, B, C, D) and all issues (#10–#27) resolved. Full pipeline
operational end-to-end. Dynamic watchlist selection complete (Phase 1).
Phase 2 (per-ticker assessment in CLI) is the only remaining planned enhancement.

---

## Issue 29: Safety Mechanism — Intelligent Halt Guard

**Priority:** P1 — High
**Depends on:** `PortfolioFetcherAgent` (branch `claude/add-safety-mechanism-tlLMF` — ✅ done)
**Branch:** TBD (continuation of `claude/add-safety-mechanism-tlLMF`)
**Labels:** `feature`, `safety`, `risk`

### Summary

The system needs a smarter halt guard that can distinguish between two loss scenarios:

| Scenario | Correct response |
|---|---|
| Market-driven drawdown (correlated with broad sell-off; positions are coherent) | **Do not halt** — hold positions, let the thesis play out |
| System malfunction (loop, calibration drift, parameter instability) | **Halt immediately** — cancel orders, close all positions |

The current single-threshold hard stop (`max_drawdown_pct = 0.03`) cannot tell these
apart and will prematurely close healthy PEAD positions during ordinary market volatility.

---

### Open Question 1: Is 3% too tight?

**Yes, almost certainly.** The current 3% default was written for an intraday assumption
but the system holds positions for up to `PEAD_HORIZON_DAYS` (default 5) calendar days.
Normal single-day volatility for individual equities is easily ±2–3%.

Candidate values to discuss:

| Mode | Suggested `max_drawdown_pct` | Rationale |
|---|---|---|
| Intraday only (no PEAD) | 2–3% | Standard day-trading stop |
| Mixed (intraday + PEAD swing) | 6–8% | Covers normal 2–3× sigma moves |
| Position investing | 10–15% | Allows full earnings cycle to play out |

**The case for 10%:** A news-driven earnings trade that is directionally correct can
easily see a 5–7% counter-move intraday before the thesis resolves.  Closing at 3%
would systematically destroy P&L on trades that would have recovered.

**Decision needed:** What is the primary holding-period assumption for this system?
Once settled, `max_drawdown_pct` should be updated in `config.py` accordingly.

---

### Open Question 2: Proposed Two-Threshold Architecture

Instead of a single threshold, use two:

```
soft threshold (e.g. 2%)   → trigger LLM safety review → verdict decides
hard threshold (e.g. 8%)   → immediate halt, no LLM involved
```

**Simple circuit breakers (no LLM, always-on)** — catch obvious malfunctions cheaply:

| Check | Trigger condition | Action |
|---|---|---|
| Signal burst | `len(trade_signals) > N` in one cycle | Auto-halt |
| Signal homogeneity | All signals same ticker + direction + identical confidence score | Trigger LLM review |
| Loss velocity | Drawdown exceeds soft threshold in a single cycle | Trigger LLM review |

**LLM safety review (triggered by soft threshold or circuit breakers):**

A `SafetyReviewAgent` sends the LLM:
- Last N trade signals (ticker, direction, conviction, reasoning text, P&L outcome)
- Portfolio loss context (`daily_pnl`, `max_drawdown_pct`)
- Broad market context (e.g. SPY % change — a single `MarketSnapshot` suffices)

The LLM returns a structured verdict:

| Verdict | Meaning | Action |
|---|---|---|
| `MARKET_CONDITIONS` | Losses are market-correlated; signals are coherent and diverse | Override soft threshold; continue |
| `PARAMETER_INSTABILITY` | Confidence scores uniformly at floor; calibration drift detected | Halt; Telegram alert |
| `MALFUNCTION` | Repetitive signals, identical reasoning text, loop pattern detected | Halt; Telegram alert |

Only `PARAMETER_INSTABILITY` and `MALFUNCTION` trigger `system_halted = True`.
`MARKET_CONDITIONS` allows the pipeline to continue past the soft threshold
(the hard threshold still halts unconditionally).

---

### Open Question 3: Trim the implementation scope

Full implementation would require:

| File | Purpose |
|---|---|
| `models/safety.py` | `SafetyVerdict` enum + `SafetyAssessment` frozen model |
| `agents/safety_review_agent.py` | Heuristics + LLM analysis + structured verdict |
| `config.py` | New settings (soft threshold, burst limit, consecutive loss limit) |
| `graph/state.py` | Optional `safety_assessment` field |
| `graph/pipeline.py` | Wire between RiskManager and HaltHandler |
| `tests/test_safety_review_agent.py` | Tests |

A **minimal v1** could skip the `CircuitBreakerService` abstraction entirely and inline
all heuristics directly in `SafetyReviewAgent.run()`, reducing new files to 2 (agent + tests).
The signal burst and homogeneity checks are simple enough to be a dozen lines each.

**Decision needed:** Build full version now, or start with minimal v1?

---

### Open Question 4: LLM model tier for safety review

The safety review needs to reason over signal patterns and market context — a nuanced
task more suited to the deep model (Sonnet) than the quick model (Haiku).

- **Sonnet cost estimate:** ~2K input tokens per review ≈ $0.006 per triggered call.
  If it fires at most a handful of times per session, this is negligible.
- **Trigger frequency:** Depends on calibration of the soft threshold. At 2%, this
  may fire several times per session on volatile days — worth monitoring.
- **Recommendation:** Deep model, but add a cooldown (e.g. max one LLM review per 5
  cycles) to prevent runaway spend if the threshold is hit repeatedly.

---

### Open Question 5: Interaction with existing mechanisms

| Concern | Notes |
|---|---|
| `risk_dry_run=True` | LLM review should still run (for calibration logging) but HALT verdict should be suppressed, consistent with dry-run contract |
| PEAD positions | Most compelling use case: LLM can recognise `MARKET_CONDITIONS` and avoid premature close of healthy PEAD positions during intraday swings |
| Telegram `/stop` | Operator stop bypasses LLM review entirely — correct; `/stop` is explicit human intent |
| `HaltHandlerAgent` | Unchanged — still runs when `system_halted=True`; the LLM review only gates whether that flag gets set |

---

### Open Question 6: Drawdown metric is daily-only (related gap)

`PortfolioFetcherAgent` computes drawdown as `(equity − last_equity) / last_equity`
using Alpaca's `account.last_equity` (previous day's close). This is a **daily** measure.

Two failure modes:
- **Slow multi-day bleed** (−1%/day) will never trigger the daily 3% halt.
- **Sharp intraday spike** will trigger even if the week is flat.

Alternatives:
- `TradingClient.get_portfolio_history(period="1M", timeframe="1D")` — 30-day equity
  curve; compute true peak-to-trough. One extra API call per cycle.
- **DB high-watermark** (preferred): store peak equity in a new `AccountStateRow`
  (Alembic migration required); `PortfolioFetcherAgent` updates it each cycle when
  equity is above the stored peak. Gives accurate multi-day drawdown without extra API calls.

This interacts with ISSUE-001 threshold values — worth resolving together.

---

## Future: Prometheus / Grafana Operational Dashboard

**Priority:** P3 — Future
**Depends on:** System running stably in paper/live trading
**Labels:** `monitoring`, `infrastructure`

When the system is operative and producing consistent trading activity, a
Prometheus + Grafana dashboard will provide real-time operational metrics:
cycle latency histograms, signal throughput, error rates, Alpaca order fill
latency, and Redis event-bus queue depth.

**Planned approach:**
- Instrument each LangGraph node with `prometheus_client` counters/histograms
  (node duration, error count, signal count per EventType)
- Expose `/metrics` endpoint via a lightweight HTTP server (`prometheus_client.start_http_server`)
- Grafana dashboard with pre-built panels for the 8-node pipeline
- Alert rules for: drawdown halt triggered, >3 consecutive failed cycles,
  Claude daily budget >80% consumed

This is intentionally deferred — LangSmith already covers trace-level debugging
for the development phase. Prometheus/Grafana adds value once the system is
running 24/7 and operational anomaly detection becomes the priority.

