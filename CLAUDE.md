# CLAUDE.md — AI Assistant Guide for news-trade

This file provides context for AI assistants working in this repository.
Detailed guidance for specific subdirectories is in nested `CLAUDE.md` files — see the
**Subdirectory Guides** section below to know when to read them.

---

## Project Overview

**news-trade** is a multi-agent, news-driven automated trading system. It ingests financial
news, classifies sentiment via an LLM (Anthropic Claude API or local Ollama models), generates
trade signals, manages risk, and executes orders through Alpaca Markets.

The pipeline is orchestrated with **LangGraph**. Each stage is an independent agent that
reads from and writes to a shared `PipelineState` TypedDict.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Agent orchestration | LangGraph |
| LLM / Sentiment | Anthropic Claude API (`anthropic`) or Ollama local models (`openai` SDK compat) |
| Broker | Alpaca Markets (`alpaca-py`) |
| Data Sources | RSS feeds, Benzinga, yfinance, Polygon.io |
| Event Bus | Redis async pub/sub |
| Operator interface | Telegram Bot (`python-telegram-bot` v20+) |
| Database | SQLite (default) via SQLAlchemy ORM + **Alembic** migrations |
| Validation | Pydantic v2 |
| Linting | ruff |
| Type checking | mypy (strict) |
| Testing | pytest + pytest-asyncio |

---

## Subdirectory Guides

Read these files when working in the corresponding area. Each one contains the detailed
conventions, patterns, and status information that would otherwise clutter this file.

| Directory | Read `CLAUDE.md` there when you are… |
|---|---|
| `src/news_trade/agents/` | Implementing or extending any agent; understanding which agents are stubs vs done; wiring `Stage1Repository`, `ConfidenceScorer`, or `LLMClientFactory` into `SignalGeneratorAgent` or `RiskManagerAgent`; `EarningsCalendarAgent` pattern for cron-driven agents |
| `src/news_trade/models/` | Adding or modifying Pydantic models; understanding frozen vs mutable, computed fields, or how `EventType` tiers map to signal logic |
| `src/news_trade/services/` | Using `LLMClientFactory`, `ConfidenceScorer`, `EstimatesRenderer`, or `Stage1Repository`; adding ORM tables; understanding the two-tier LLM routing or Pattern D reflection loop |
| `src/news_trade/providers/` | Adding a new data provider (news, market, sentiment, or calendar); understanding the Protocol abstraction and factory wiring |
| `tests/` | Writing tests; understanding the `_make(**kwargs)` helper pattern, in-memory SQLite setup, or `AsyncMock` usage |

---

## Repository Layout

```
alembic/                   # Alembic migration environment + versioned schema migrations
├── env.py                 # Runtime env: injects DATABASE_URL, wires Base.metadata
├── script.py.mako         # Migration file template
└── versions/              # One file per migration (e.g. initial_schema, add_foo_column)
alembic.ini                # Alembic config — sqlalchemy.url is always empty (set at runtime)
DEPLOY.md                  # Operator runbook: alembic stamp head, safe switch windows

src/news_trade/
├── config.py              # Pydantic Settings — all env vars (see Configuration below)
├── main.py                # Entrypoint: builds pipeline, runs polling loop (logs version banner)
├── agents/                # LangGraph agent nodes + cron agents — see agents/CLAUDE.md
├── graph/
│   ├── state.py           # PipelineState TypedDict
│   └── pipeline.py        # StateGraph builder + conditional routing
├── models/                # Pydantic data models — 10 files → see models/CLAUDE.md
├── providers/             # Protocol-based data providers → see providers/CLAUDE.md
│   ├── base.py            # NewsProvider, MarketDataProvider, SentimentProvider Protocols
│   ├── __init__.py        # Factory functions: get_*_provider(settings)
│   ├── news/              # RSSNewsProvider, BenzingaNewsProvider
│   ├── market/            # YFinance, PolygonFree, PolygonPaid
│   ├── sentiment/         # ClaudeSentimentProvider (budget-capped), KeywordSentimentProvider
│   └── calendar/          # FMPCalendarProvider (primary), YFinanceCalendarProvider (fallback)
├── services/              # Business logic + persistence → see services/CLAUDE.md
│   ├── database.py        # Engine + session factory + create_tables() [runs alembic upgrade head]
│   ├── tables.py          # ORM table definitions (6 tables)
│   ├── llm_client.py      # LLMClient Protocol, AnthropicLLMClient, OllamaLLMClient, LLMClientFactory
│   ├── estimates_renderer.py  # Deterministic FMP data → narrative formatter
│   ├── confidence_scorer.py   # 4-component weighted scorer + confidence gate
│   ├── stage1_repository.py   # Stage 1 position CRUD + outcome reflection (Pattern D)
│   ├── telegram_bot.py    # Telegram operator interface (notifications, approval gate, commands)
│   └── event_bus.py       # Async Redis pub/sub wrapper
└── py.typed               # PEP 561 marker

tests/                     # pytest suite — see tests/CLAUDE.md for conventions
```

---

## Pipeline Architecture

```
PortfolioFetcherAgent   ← NEW: fetches live equity + positions from Alpaca (always runs)
    ↓
NewsIngestorAgent
    │ (no new news? → END)
    ↓
MarketDataAgent
    ↓
SentimentAnalystAgent
    ↓
SignalGeneratorAgent
    ↓
RiskManagerAgent
    │ (no approved signals? → END)
    ↓
ExecutionAgent
    ↓
END
```

`PipelineState` (`graph/state.py`) is a `TypedDict` containing: `news_events`,
`market_context`, `sentiment_results`, `trade_signals`, `approved_signals`,
`rejected_signals`, `orders`, `portfolio`, `errors`, `system_halted`.

`portfolio` is populated by `PortfolioFetcherAgent` (first node) with live data from
Alpaca each cycle. All `RiskManagerAgent` checks (drawdown halt, size cap, position
concentration) operate on real figures.

---

## Configuration (`config.py`)

All settings come from environment variables via `pydantic-settings`. Copy `.env.example`
to `.env` before running.

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for Claude sentiment |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | — | Broker credentials |
| `BENZINGA_API_KEY` | — | Premium news (optional) |
| `POLYGON_API_KEY` | — | Premium market data (optional) |
| `FMP_API_KEY` | — | FMP earnings calendar + estimates (optional; falls back to yfinance) |
| `REDIS_URL` | `redis://localhost:6379/0` | Event bus |
| `DATABASE_URL` | `sqlite:///data/trades.db` | Persistence |
| `WATCHLIST` | `["AAPL","MSFT","GOOGL","AMZN","TSLA"]` | Tickers to monitor |
| `NEWS_PROVIDER` | `rss` | `rss` or `benzinga` |
| `MARKET_DATA_PROVIDER` | `yfinance` | `yfinance`, `polygon_free`, `polygon_paid` |
| `SENTIMENT_PROVIDER` | `claude` | `claude` or `keyword` |
| `LLM_PROVIDER` | `anthropic` | LLM backend: `anthropic` or `ollama` |
| `LLM_QUICK_MODEL` | `claude-haiku-4-5-20251001` | Quick model (e.g. `llama3.2:3b` for Ollama) |
| `LLM_DEEP_MODEL` | `claude-sonnet-4-6` | Deep model (e.g. `llama3.1:8b` for Ollama) |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama OpenAI-compatible endpoint (`ollama` provider only) |
| `CLAUDE_DAILY_BUDGET_USD` | `2.00` | Hard cap on Claude API spend per day (Anthropic only) |
| `SENTIMENT_DRY_RUN` | `false` | Skip real Claude calls; return neutral |
| `NEWS_KEYWORD_PREFILTER` | `true` | Pre-filter news by keyword before sentiment |
| `EARN_BEAT_PCT_THRESHOLD` | `2.0` | EPS % surprise above which event is EARN_BEAT |
| `EARN_MISS_PCT_THRESHOLD` | `-2.0` | EPS % surprise below which event is EARN_MISS |
| `EARN_STRONG_SIGMA_THRESHOLD` | `2.0` | Sigma threshold for STRONG signal tier |
| `EARN_MIN_ANALYST_COUNT` | `3` | Minimum analyst count for non-floor coverage score |
| `EARN_GUIDANCE_WEIGHT` | `0.20` | Weight of guidance sentiment in composite surprise |
| `EARN_DEFAULT_BEAT_RATE` | `0.65` | Fallback beat rate for EARN_PRE when < 4 observed outcomes exist |
| `PEAD_HORIZON_DAYS` | `5` | Calendar days after EARN_BEAT/MISS fill before auto-close via PEAD expiry cron |
| `MAX_OPEN_POSITIONS` | `5` | Max concurrent open positions (Stage 2 ADD exempt) |
| `RISK_DRY_RUN` | `false` | Log risk rejections without blocking — calibration mode |
| `TELEGRAM_BOT_TOKEN` | `""` | Telegram Bot API token from @BotFather (empty = bot disabled) |
| `TELEGRAM_CHAT_ID` | `0` | Operator chat ID (0 = bot disabled) |

---

## Development Workflow

```bash
# Setup
uv sync                          # Install all dependencies (including dev extras)
cp .env.example .env             # Configure environment variables
docker compose up -d             # Start Redis

# Run
uv run news-trade                # Start the main polling loop
uv run news-trade --once         # Run a single cycle and exit (debug mode)
uv run news-trade --replay-ticker AAPL            # Replay last 5 stored AAPL articles (implies --once)
uv run news-trade --replay-ticker AAPL --replay-limit 10  # Replay last 10 stored articles
uv run news-trade --resume-session                # Log previous session summary on startup (latest file)
uv run news-trade --session-file data/sessions/session_20260401_090000.json  # Load specific session

# Quality
uv run ruff check src/ tests/    # Lint (rules: E, F, I, N, UP, B, SIM, RUF)
uv run ruff format src/ tests/   # Auto-format
uv run mypy src/                 # Strict type checking — no implicit Any
uv run pytest                    # Full test suite
uv run pytest -x                 # Stop at first failure

# Database migrations (Alembic)
uv run alembic upgrade head                          # Apply all pending migrations (also runs at startup)
uv run alembic revision --autogenerate -m "name"     # Generate migration after editing tables.py
uv run alembic stamp head                            # Mark existing DB as current (first deploy only)
uv run alembic current                               # Show running revision
uv run alembic history                               # List all migrations
```

Always run `ruff check` before committing. Fix all errors; do not suppress with `# noqa`
unless absolutely necessary with a comment explaining why.

---

## Code Conventions

- Python 3.11+ — use `match`/`case` where appropriate
- `async def` for all I/O-bound operations; `sync` for pure computation and DB access
- `from __future__ import annotations` at the top of every file
- Standard library → third-party → local imports (ruff `I` rules enforce order)
- Classes: `PascalCase` · Functions/variables: `snake_case` · Constants: `UPPER_SNAKE_CASE` · Private: `_leading_underscore`
- Raise specific exceptions; log at `WARNING`/`ERROR` before re-raising
- Graceful degradation over exceptions for budget/quota limits (see `ClaudeSentimentProvider`)
- Dependency injection everywhere — no module-level singletons

---

## Implementation Status

| Component | Status |
|---|---|
| Provider Protocols (base.py) | Done |
| RSS + Benzinga news providers | Done |
| yfinance + Polygon market providers | Done |
| Claude + Keyword sentiment providers | Done |
| Provider factory functions | Done |
| **PortfolioFetcherAgent — pipeline entry point** | **Done — fetches live equity, positions, and drawdown from Alpaca each cycle; activates all RiskManagerAgent checks** |
| NewsIngestorAgent | Done |
| MarketDataAgent | Done |
| SentimentAnalystAgent | Done |
| LangGraph pipeline + routing | Done |
| SQLAlchemy ORM + tables | Done |
| Redis event bus | Done |
| **LLMClient service — deep/quick split (Pattern B)** | **Done** |
| **OllamaLLMClient — OpenAI-compatible local LLM backend** | **Done — `openai` SDK pointed at `http://localhost:11434/v1`; function calling for structured output; `LLM_PROVIDER=ollama` to activate** |
| **EventType fine-grained values** | **Done** |
| **Surprise models (EstimatesData, MetricSurprise, EarningsSurprise)** | **Done** |
| **EstimatesRenderer (Pattern C)** | **Done** |
| **ConfidenceScorer (Pattern C)** | **Done** |
| **TradeSignal confidence fields** | **Done** |
| **Stage1Status + OpenStage1Position models (Pattern D)** | **Done** |
| **HistoricalOutcomes model (Pattern D)** | **Done** |
| **Stage1Repository — CRUD + outcome reflection (Pattern D)** | **Done** |
| **ORM tables: OpenStage1PositionRow, EarningsOutcomeRow (Pattern D)** | **Done** |
| **DebateRound / DebateVerdict / DebateResult models (Pattern A)** | **Done** |
| **SignalGeneratorAgent — run + _build_signal + _debate_signal (Pattern A)** | **Done** |
| **ClaudeSentimentProvider — per-event LLM tier routing + EARN_PRE prompt** | **Done** |
| **Sentiment LLM routing Phase 2 — EstimatesRenderer injected into EARN_PRE prompt** | **Done** |
| **EarningsCalendarEntry model + ReportTiming StrEnum** | **Done** |
| **CalendarProvider Protocol** | **Done** |
| **FMPCalendarProvider + YFinanceCalendarProvider** | **Done** |
| **EarningsCalendarAgent — cron-driven EARN_PRE event synthesis** | **Done** |
| **ExecutionAgent (Alpaca)** | **Done — paper trading, asyncio.to_thread, OrderRow persistence** |
| **PEAD horizon expiry in `ExecutionAgent`** | **Done — `TradeSignal.horizon_days` + `OrderRow.close_after_date`; `scan_expired_pead()` cron closes Stage 2 positions at 09:45 ET** |
| **RiskValidation model (`models/risk.py`)** | **Done — frozen audit model produced by RiskManagerAgent** |
| **RiskManagerAgent — five-layer fail-fast checks** | **Done — confidence gate, drawdown halt, concentration (Stage 2 ADD exempt), pending dedup, L3b hard size cap (`_apply_size_cap`), direction conflict; risk_dry_run mode** |
| **SignalGeneratorAgent — ConfidenceScorer wiring (all event types)** | **Done — scorer injected; every signal exits _build_signal() with passed_confidence_gate set** |
| **SignalGeneratorAgent — EARN_\* two-stage logic** | **Done — EARN_PRE sizes from beat_rate + persists Stage1; EARN_BEAT/MISS confirm/reverse; EARN_MIXED exits flat** |
| **TradeSignal `stage1_id` field** | **Done — links Stage 2 POST signals to Stage 1 position; unblocks RiskManagerAgent ADD exemption** |
| **ExpiryScanner** | **Done — marks stale OPEN positions EXPIRED; 07:15 ET daily cron** |
| **Cron scheduler wiring in main.py** | **Done — APScheduler AsyncIOScheduler; EarningsCalendarAgent 07:00 ET, ExpiryScanner 07:15 ET, PEADExpiryScanner 09:45 ET Mon-Fri** |
| **FMPEstimatesProvider + EstimatesProvider Protocol** | **Done — fetches historical EPS beat rates from FMP earnings-surprises endpoint; three-tier fallback in `_handle_earn_pre()`: observed → FMP → static default; injected into EarningsCalendarAgent** |
| **`HaltHandlerAgent` — drawdown emergency cleanup node** | **Done — cancels all Alpaca orders, closes all positions, expires OPEN Stage1 positions; wired as `halt_handler` node after `RiskManagerAgent` via `_route_after_risk()` 3-way router** |
| **Dynamic Watchlist Selection** | **Done — `WatchlistManager` service + `select-watchlist` CLI; `WatchlistSelectionRow` ORM table; `is_candidate` computed field on `EarningsCalendarEntry`; injected into `NewsIngestorAgent`, `SentimentAnalystAgent`, `EarningsCalendarAgent`** |
| **Alembic schema migrations** | **Done — `create_tables()` runs `alembic upgrade head` programmatically on every startup; `alembic/versions/6dae9e7efe75_initial_schema.py` captures all 6 tables; `DEPLOY.md` documents the `alembic stamp head` first-deploy procedure** |
| **Version logging** | **Done — `main.py` logs `version`, short git commit hash, Python version, and `DATABASE_URL` at startup** |
| **Session reporting (`SessionReporter`)** | **Done — writes `data/sessions/session_YYYYMMDD_HHMMSS.json` on exit; `--resume-session` / `--session-file` CLI flags load a previous session at startup and log a summary with system-halt and error warnings** |
| **Telegram Bot (`TelegramBotService`)** | **Done — observer + operator stop: push notifications (drawdown halt, trade executed via Redis `trade_executed` channel); `/status` shows live portfolio equity, daily P&L, drawdown, cash, open positions with unrealized P&L, pending Stage 1 positions, and halt warning; `/portfolio` shows today's order summary + Stage 1 positions + last 10 orders; `/signals` shows today's approved/rejected count + last N signals; `/stop` shows inline confirmation buttons ("Yes, stop & close all" / "Cancel") — confirm cancels all orders, closes all positions, exits loop; disabled when token/chat_id unset** |

The full pipeline is now operational end-to-end for all event types. `SignalGeneratorAgent`
implements the complete EARN_\* two-stage logic (Pattern D): EARN_PRE sizes from the
historical beat rate and persists an `OpenStage1Position`; EARN_BEAT/MISS loads that
position and confirms or reverses direction; EARN_MIXED exits flat with a CLOSE signal that
bypasses the confidence gate. `ExpiryScanner` marks stale OPEN positions EXPIRED so the
`RiskManagerAgent` concentration check stays accurate. EARN_BEAT/MISS signals carry a
`horizon_days` value; `ExecutionAgent.scan_expired_pead()` auto-closes these positions when
the horizon passes. All three cron agents are scheduled via `APScheduler` (`AsyncIOScheduler`)
in `main.py` without blocking the polling loop. The dynamic watchlist (`WatchlistManager`)
lets operators scan the next 30 days of earnings via `uv run select-watchlist` and activate
tickers at runtime without restarting the process; `settings.watchlist` is the fallback when
no DB selection exists.

**Deployment blockers — minimum path to a running paper-trading instance:**

| Priority | Item | Why blocking | Status |
|---|---|---|---|
| ~~1~~ | ~~`RiskManagerAgent`~~ | ~~`run()` raises `NotImplementedError` — pipeline crashes on every cycle~~ | ✅ Done |
| ~~2~~ | ~~`ConfidenceScorer` wiring in `SignalGeneratorAgent`~~ | ~~`_build_signal()` never calls `apply_gate()`; every signal leaves with `passed_confidence_gate=False`~~ | ✅ Done |
| ~~3~~ | ~~Cron scheduler wiring in `main.py`~~ | ~~`EarningsCalendarAgent` is never triggered; EARN_PRE events never enter the pipeline~~ | ✅ Done |
| ~~4~~ | ~~`SignalGeneratorAgent` EARN_\* logic~~ | ~~EARN_PRE position sizing and Stage 1 persistence; EARN_BEAT/MISS confirm/reverse; without this the highest-value signal type is treated as a generic signal~~ | ✅ Done |
| ~~5~~ | ~~`ExpiryScanner`~~ | ~~Without it, expired Stage 1 positions accumulate in SQLite and inflate the concentration check in `RiskManagerAgent` over time~~ | ✅ Done |

All deployment blockers are resolved. The system can run paper trading end-to-end across
all event types including the full earnings two-stage flow. `FMPEstimatesProvider` gives
every ticker a calibrated historical beat rate from day one so EARN_PRE sizing never
relies solely on the static 0.65 default; once ≥4 own quarters are observed the
`Stage1Repository` data takes over. PEAD positions are now automatically closed after
`PEAD_HORIZON_DAYS` calendar days via the 09:45 ET cron.

**Remaining work (non-blocking enhancements):**
- Dynamic Watchlist Phase 2 — per-ticker assessment in `select-watchlist` (sector, 52-week return, analyst consensus, historical beat rate; CLI-only, no pipeline changes).
- Bump `pyproject.toml` version and add a git tag when cutting a release; see `DEPLOY.md` for the switch window and migration procedure.

---

## CI/CD

| Workflow | Trigger | What it does |
|---|---|---|
| `tests.yml` | PR opened/updated | Run `pytest` against all tests |
| `claude.yml` | `@claude` mention in issues/PRs | Claude Code handles the request |
| `claude-code-review.yml` | PR opened/updated | Automated code review by Claude |

All PRs must pass `tests.yml` before merging.

---

## Key Design Decisions

1. **Protocol-based providers** — Structural subtyping (not ABC). Mocks need only implement
   the called methods. Adding a provider = one new file + one enum value + one factory case.

2. **Dependency injection everywhere** — All agents and services receive dependencies at
   construction time. No globals or singletons. Makes unit testing straightforward.

3. **Daily budget cap on Claude** — `ClaudeSentimentProvider` falls back to neutral
   `SentimentResult` (not an exception) when the daily spend cap is hit.

4. **Keyword pre-filter** — `NEWS_KEYWORD_PREFILTER=true` skips Claude for events without
   relevant financial keywords, cutting API spend.

5. **Two-tier LLM routing (Pattern B)** — `LLMClientFactory.quick` for cheap tasks
   (classification, extraction, non-earnings sentiment); `.deep` for expensive tasks
   (confidence scoring, synthesis, EARN_PRE/BEAT/MISS sentiment). `ClaudeSentimentProvider`
   holds the full factory and selects the tier per event inside `_select_client()` — the
   agent layer is unchanged. Two backends are implemented: `AnthropicLLMClient` (Claude API)
   and `OllamaLLMClient` (local models via Ollama's OpenAI-compatible endpoint). Adding
   further providers (OpenAI, Gemini) requires one new class — no agent changes.

6. **Deterministic confidence scoring (Pattern C)** — `EstimatesRenderer` pre-computes
   surprise deltas; `ConfidenceScorer` applies a per-`EventType` weight matrix. No LLM
   involvement. `EARN_MIXED` gate is 1.01 — always fails, forcing human review.

7. **Reflection loop / observed beat rates (Pattern D)** — `Stage1Repository` records
   EARN_PRE outcomes to `earnings_outcomes`. After ≥4 quarters per ticker, `load_historical_outcomes()`
   returns `source='observed'` displacing static FMP data. `record_outcome()` is idempotent
   (unique constraint on `stage1_id`). System starts in bootstrapping mode (all FMP).

8. **Bull/Bear debate gate (Pattern A)** — `SignalGeneratorAgent._debate_signal()` runs N
   rounds of LLM bull/bear argument (quick model) then a synthesis verdict (deep model) for
   signals above `signal_debate_threshold`. Default `signal_debate_rounds=0` keeps the feature
   off and API cost flat. Verdicts: `CONFIRM` (no-op), `REDUCE` (halve qty), `REJECT` (flip
   `passed_confidence_gate=False`). Debate results are stored on `TradeSignal.debate_result`
   for auditability.

9. **LangGraph for orchestration** — Typed state graph makes the pipeline inspectable and
   testable at the graph level.

10. **SQLite default** — Zero-config for development; swap to PostgreSQL via `DATABASE_URL`.

11. **Operator-driven dynamic watchlist** — `WatchlistManager` surfaces upcoming earnings via
    `uv run select-watchlist`; the operator picks tickers interactively. The selection is
    persisted to `WatchlistSelectionRow` and picked up on the next polling cycle — no restart.
    `settings.watchlist` remains the fallback when no DB selection exists. Full automation
    (auto-add all reporters) was rejected to keep API costs and risk exposure bounded.
