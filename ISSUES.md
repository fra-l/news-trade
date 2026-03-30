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
