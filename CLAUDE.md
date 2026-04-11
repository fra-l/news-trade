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
| Data Sources | RSS feeds, Benzinga, yfinance, Massive.com |
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
│   ├── market/            # YFinance, MassiveFree, MassivePaid
│   ├── sentiment/         # ClaudeSentimentProvider (budget-capped), KeywordSentimentProvider
│   └── calendar/          # FMPCalendarProvider (primary), YFinanceCalendarProvider (fallback)
├── services/              # Business logic + persistence → see services/CLAUDE.md
│   ├── database.py        # Engine + session factory + create_tables() [runs alembic upgrade head]
│   ├── tables.py          # ORM table definitions (5 tables)
│   ├── llm_client.py      # LLMClient Protocol, AnthropicLLMClient, OllamaLLMClient, LLMClientFactory
│   ├── estimates_renderer.py  # Deterministic FMP data → narrative formatter
│   ├── confidence_scorer.py   # 4-component weighted scorer + confidence gate
│   ├── stage1_repository.py   # Stage 1 position CRUD + outcome reflection (Pattern D)
│   ├── telegram_bot.py    # Telegram operator interface (notifications, approval gate, commands)
│   └── event_bus.py       # Async Redis pub/sub wrapper
├── cli/
│   └── startup_selector.py    # Interactive small-cap ticker selection at startup
└── py.typed               # PEP 561 marker

tests/                     # pytest suite — see tests/CLAUDE.md for conventions
```

---

## Pipeline Architecture

```
[START]
  ├── PortfolioFetcherAgent    ← live Alpaca equity + positions (parallel)
  ├── NewsIngestorAgent        ← news from RSS/Benzinga provider (parallel)
  └── EarningsTickerNode       ← active earnings tickers from calendar DB (parallel, new)
            ↓  (fan-in: post_init)
            │  no work (no news + no active tickers)? → END
            ↓  (fan-out: analysis_fan)
  ├── MarketDataAgent          ← OHLCV snapshots for all tickers (parallel)
  └── SentimentAnalystAgent    ← LLM sentiment per event; concurrent internally (parallel)
            ↓  (fan-in: SignalGeneratorAgent)
       SignalGeneratorAgent     ← builds signals; debate rounds run bull/bear in parallel
            ↓
       RiskManagerAgent
            ↓
  ┌── HaltHandlerAgent → END
  ├── ExecutionAgent → END
  └── END
```

**No-news cycles:** `EarningsTickerNode` synthesises ephemeral `EARN_PRE` `NewsEvent`
objects for every active earnings ticker (earnings in the next 1–7 days) on every
pipeline cycle. These events flow through `SentimentAnalystAgent` and
`SignalGeneratorAgent` exactly like cron-generated events. A lack of supporting news
naturally produces a lower `ConfidenceScorer` output, resulting in a smaller or gated
signal — no separate handling required.

`PipelineState` (`graph/state.py`) is a `TypedDict` containing: `news_events`
(Annotated with `operator.add` reducer), `active_tickers`, `market_context`,
`sentiment_results`, `trade_signals`, `approved_signals`, `rejected_signals`,
`orders`, `portfolio`, `errors` (Annotated with `operator.add` reducer), `system_halted`.

`news_events` and `errors` use `operator.add` reducers so parallel nodes accumulate
without overwriting each other. `portfolio` is populated by `PortfolioFetcherAgent`
at the start of each cycle. All `RiskManagerAgent` checks operate on real figures.

`post_init` and `analysis_fan` are no-op passthrough nodes whose only purpose is
fan-in / fan-out synchronisation.

---

## Configuration (`config.py`)

All settings come from environment variables via `pydantic-settings`. Copy `.env.example`
to `.env` before running.

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for Claude sentiment |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | — | Broker credentials |
| `BENZINGA_API_KEY` | — | Premium news (optional) |
| `MASSIVE_API_KEY` | — | Premium market data (optional) |
| `FINNHUB_API_KEY` | — | Finnhub earnings calendar — preferred; free tier supports broad market scan |
| `FMP_API_KEY` | — | FMP earnings calendar (fallback) + historical EPS beat rates; falls back to yfinance when absent |
| `REDIS_URL` | `redis://localhost:6379/0` | Event bus |
| `DATABASE_URL` | `sqlite:///data/trades.db` | Persistence |
| `SMALL_CAP_MAX_MARKET_CAP_USD` | `2000000000` | Market-cap ceiling (USD) for small-cap filter at startup |
| `SMALL_CAP_MIN_PRICE_USD` | `1.0` | Minimum stock price (USD) for startup ticker selection; filters out penny stocks |
| `MAX_STARTUP_TICKERS` | `5` | Max tickers selected at startup (-1 = unlimited) |
| `NEWS_PROVIDER` | `rss` | `rss` or `benzinga` |
| `MARKET_DATA_PROVIDER` | `yfinance` | `yfinance`, `massive_free`, `massive_paid`, `finnhub` |
| `SENTIMENT_PROVIDER` | `claude` | `claude` or `keyword` |
| `LLM_PROVIDER` | `anthropic` | LLM backend: `anthropic` or `ollama` |
| `LLM_QUICK_MODEL` | `claude-haiku-4-5-20251001` | Quick model (e.g. `llama3.2:3b` for Ollama) |
| `LLM_DEEP_MODEL` | `claude-sonnet-4-6` | Deep model (e.g. `llama3.1:8b` for Ollama) |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama OpenAI-compatible endpoint (`ollama` provider only) |
| `CLAUDE_DAILY_BUDGET_USD` | `2.00` | Hard cap on Claude API spend per day (Anthropic only) |
| `SENTIMENT_DRY_RUN` | `false` | Skip real Claude calls; return neutral |
| `SENTIMENT_MAX_CONCURRENT` | `5` | Max parallel LLM calls per batch; lower for local Ollama (e.g. `2`) |
| `NEWS_KEYWORD_PREFILTER` | `true` | Pre-filter news by keyword before sentiment |
| `ARTICLE_DECAY_HALFLIFE_HOURS` | `72` | Half-life (hours) for exponential article age decay in per-ticker sentiment aggregation; lower = recency-focused, higher = broader quarterly context |
| `EARN_BEAT_PCT_THRESHOLD` | `2.0` | EPS % surprise above which event is EARN_BEAT |
| `EARN_MISS_PCT_THRESHOLD` | `-2.0` | EPS % surprise below which event is EARN_MISS |
| `EARN_STRONG_SIGMA_THRESHOLD` | `2.0` | Sigma threshold for STRONG signal tier |
| `EARN_MIN_ANALYST_COUNT` | `3` | Minimum analyst count for non-floor coverage score |
| `EARN_GUIDANCE_WEIGHT` | `0.20` | Weight of guidance sentiment in composite surprise |
| `EARN_DEFAULT_BEAT_RATE` | `0.65` | Fallback beat rate for EARN_PRE when < 4 observed outcomes exist |
| `EARN_PRE_HORIZON_DAYS` | `14` | Look-ahead window (days) for pre-earnings pipeline — controls EarningsCalendarAgent scan range, EarningsTickerNode filter, and StartupSelector range |
| `EARN_THESIS_FLIP_CONVICTION_THRESHOLD` | `0.65` | Min thesis-debate conviction required to flip (REVERSE) an open EARN_PRE position (Phase 2) |
| `PEAD_HORIZON_DAYS` | `5` | Calendar days after EARN_BEAT/MISS fill before auto-close via PEAD expiry cron |
| `MAX_OPEN_POSITIONS` | `5` | Max concurrent open positions (Stage 2 ADD exempt) |
| `RISK_DRY_RUN` | `false` | Log risk rejections without blocking — calibration mode |
| `TELEGRAM_BOT_TOKEN` | `""` | Telegram Bot API token from @BotFather (empty = bot disabled) |
| `TELEGRAM_CHAT_ID` | `0` | Operator chat ID (0 = bot disabled) |
| `LANGCHAIN_TRACING_V2` | `false` | Enable LangSmith tracing; set `true` to activate |
| `LANGCHAIN_API_KEY` | `""` | LangSmith API key from smith.langchain.com |
| `LANGCHAIN_PROJECT` | `news-trade` | LangSmith project name for grouping runs |
| `LANGCHAIN_ENDPOINT` | `https://api.smith.langchain.com` | LangSmith ingestion endpoint |

---

## Development Workflow

```bash
# Setup
uv sync                          # Install all dependencies (including dev extras)
cp .env.example .env             # Configure environment variables
docker compose up -d             # Start Redis

# Run
uv run news-trade                # Start — fetches small-cap earnings, prompts for ticker selection, then loops
uv run news-trade --once         # Run a single cycle and exit (non-interactive: auto-selects top-N tickers)
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

# LangGraph Studio (graph visualization + step-through debugging)
uv run langgraph dev --allow-blocking   # start local API server on http://localhost:2024
# --allow-blocking is required: agents use sync SQLAlchemy which blocks the ASGI event loop.
# This flag suppresses the BlockingError — all Studio features (state inspection, time-travel,
# step-through) work normally. The production polling loop is unaffected (no ASGI server there).
# Open https://smith.langchain.com/studio → connect to http://localhost:2024
# Note: on WSL2, localhost is auto-forwarded — use http://localhost:2024 from Windows browser
#
# Two graphs are available in Studio:
#   news_trade         — normal pipeline; submit {} to trigger a live news fetch
#   news_trade_replay  — adds a studio_seed node that auto-loads the last 5 DB events,
#                        so the full chain always fires without a live news source.
#                        Submit {} as state — no config needed.

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

11. **Startup small-cap ticker selection** — At launch, `StartupSelector` fetches the
    earnings calendar for the next 14 days, filters to small-cap companies
    (market cap ≤ `SMALL_CAP_MAX_MARKET_CAP_USD`, default $2B via yfinance), and prompts
    the operator to pick up to `MAX_STARTUP_TICKERS` (default 5, -1 = unlimited). The
    selected `list[str]` is passed directly to all agents for the session. In non-interactive
    mode (no TTY) the top-N by nearest report date are auto-selected. No DB persistence —
    selections are per-session and restart-stable. Full automation was rejected to keep API
    costs and risk exposure bounded.
