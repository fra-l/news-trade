# CLAUDE.md — AI Assistant Guide for news-trade

This file provides context for AI assistants working in this repository.
Detailed guidance for specific subdirectories is in nested `CLAUDE.md` files — see the
**Subdirectory Guides** section below to know when to read them.

---

## Project Overview

**news-trade** is a multi-agent, news-driven automated trading system. It ingests financial
news, classifies sentiment via the Anthropic Claude API, generates trade signals, manages
risk, and executes orders through Alpaca Markets.

The pipeline is orchestrated with **LangGraph**. Each stage is an independent agent that
reads from and writes to a shared `PipelineState` TypedDict.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Agent orchestration | LangGraph |
| LLM / Sentiment | Anthropic Claude API (`anthropic`) |
| Broker | Alpaca Markets (`alpaca-py`) |
| Data Sources | RSS feeds, Benzinga, yfinance, Polygon.io |
| Event Bus | Redis async pub/sub |
| Database | SQLite (default) via SQLAlchemy ORM |
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
| `src/news_trade/agents/` | Implementing or extending any agent; understanding which agents are stubs vs done; wiring `Stage1Repository`, `ConfidenceScorer`, or `LLMClientFactory` into `SignalGeneratorAgent` or `RiskManagerAgent` |
| `src/news_trade/models/` | Adding or modifying Pydantic models; understanding frozen vs mutable, computed fields, or how `EventType` tiers map to signal logic |
| `src/news_trade/services/` | Using `LLMClientFactory`, `ConfidenceScorer`, `EstimatesRenderer`, or `Stage1Repository`; adding ORM tables; understanding the two-tier LLM routing or Pattern D reflection loop |
| `src/news_trade/providers/` | Adding a new data provider (news, market, or sentiment); understanding the Protocol abstraction and factory wiring |
| `tests/` | Writing tests; understanding the `_make(**kwargs)` helper pattern, in-memory SQLite setup, or `AsyncMock` usage |

---

## Repository Layout

```
src/news_trade/
├── config.py              # Pydantic Settings — all env vars (see Configuration below)
├── main.py                # Entrypoint: builds pipeline, runs polling loop
├── agents/                # LangGraph agent nodes — 3 done, 3 stubs → see agents/CLAUDE.md
├── graph/
│   ├── state.py           # PipelineState TypedDict
│   └── pipeline.py        # StateGraph builder + conditional routing
├── models/                # Pydantic data models — 9 files → see models/CLAUDE.md
├── providers/             # Protocol-based data providers → see providers/CLAUDE.md
│   ├── base.py            # NewsProvider, MarketDataProvider, SentimentProvider Protocols
│   ├── __init__.py        # Factory functions: get_*_provider(settings)
│   ├── news/              # RSSNewsProvider, BenzingaNewsProvider
│   ├── market/            # YFinance, PolygonFree, PolygonPaid
│   └── sentiment/         # ClaudeSentimentProvider (budget-capped), KeywordSentimentProvider
├── services/              # Business logic + persistence → see services/CLAUDE.md
│   ├── database.py        # Engine + session factory + create_tables()
│   ├── tables.py          # ORM table definitions (5 tables)
│   ├── llm_client.py      # LLMClient Protocol, AnthropicLLMClient, LLMClientFactory
│   ├── estimates_renderer.py  # Deterministic FMP data → narrative formatter
│   ├── confidence_scorer.py   # 4-component weighted scorer + confidence gate
│   ├── stage1_repository.py   # Stage 1 position CRUD + outcome reflection (Pattern D)
│   └── event_bus.py       # Async Redis pub/sub wrapper
└── py.typed               # PEP 561 marker

tests/                     # pytest suite — see tests/CLAUDE.md for conventions
```

---

## Pipeline Architecture

```
NewsIngestorAgent
    │ (no new news? → END)
    ↓
MarketDataAgent
    ↓
SentimentAnalystAgent
    ↓
SignalGeneratorAgent       ← STUB
    ↓
RiskManagerAgent           ← STUB
    │ (no approved signals? → END)
    ↓
ExecutionAgent             ← STUB
    ↓
END
```

`PipelineState` (`graph/state.py`) is a `TypedDict` containing: `news_events`,
`market_context`, `sentiment_results`, `trade_signals`, `approved_signals`,
`rejected_signals`, `orders`, `portfolio`, `errors`.

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
| `REDIS_URL` | `redis://localhost:6379/0` | Event bus |
| `DATABASE_URL` | `sqlite:///data/trades.db` | Persistence |
| `WATCHLIST` | `["AAPL","MSFT","GOOGL","AMZN","TSLA"]` | Tickers to monitor |
| `NEWS_PROVIDER` | `rss` | `rss` or `benzinga` |
| `MARKET_DATA_PROVIDER` | `yfinance` | `yfinance`, `polygon_free`, `polygon_paid` |
| `SENTIMENT_PROVIDER` | `claude` | `claude` or `keyword` |
| `LLM_PROVIDER` | `anthropic` | LLM backend; `anthropic` only for now |
| `LLM_QUICK_MODEL` | `claude-haiku-4-5-20251001` | Cheap model for classification / debate rounds |
| `LLM_DEEP_MODEL` | `claude-sonnet-4-6` | Accurate model for confidence scoring / synthesis |
| `CLAUDE_DAILY_BUDGET_USD` | `2.00` | Hard cap on Claude API spend per day |
| `SENTIMENT_DRY_RUN` | `false` | Skip real Claude calls; return neutral |
| `NEWS_KEYWORD_PREFILTER` | `true` | Pre-filter news by keyword before sentiment |
| `EARN_BEAT_PCT_THRESHOLD` | `2.0` | EPS % surprise above which event is EARN_BEAT |
| `EARN_MISS_PCT_THRESHOLD` | `-2.0` | EPS % surprise below which event is EARN_MISS |
| `EARN_STRONG_SIGMA_THRESHOLD` | `2.0` | Sigma threshold for STRONG signal tier |
| `EARN_MIN_ANALYST_COUNT` | `3` | Minimum analyst count for non-floor coverage score |
| `EARN_GUIDANCE_WEIGHT` | `0.20` | Weight of guidance sentiment in composite surprise |

---

## Development Workflow

```bash
# Setup
uv sync                          # Install all dependencies (including dev extras)
cp .env.example .env             # Configure environment variables
docker compose up -d             # Start Redis

# Run
uv run news-trade                # Start the main polling loop

# Quality
uv run ruff check src/ tests/    # Lint (rules: E, F, I, N, UP, B, SIM, RUF)
uv run ruff format src/ tests/   # Auto-format
uv run mypy src/                 # Strict type checking — no implicit Any
uv run pytest                    # Full test suite
uv run pytest -x                 # Stop at first failure
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
| NewsIngestorAgent | Done |
| MarketDataAgent | Done |
| SentimentAnalystAgent | Done |
| LangGraph pipeline + routing | Done |
| SQLAlchemy ORM + tables | Done |
| Redis event bus | Done |
| **LLMClient service — deep/quick split (Pattern B)** | **Done** |
| **EventType fine-grained values** | **Done** |
| **Surprise models (EstimatesData, MetricSurprise, EarningsSurprise)** | **Done** |
| **EstimatesRenderer (Pattern C)** | **Done** |
| **ConfidenceScorer (Pattern C)** | **Done** |
| **TradeSignal confidence fields** | **Done** |
| **Stage1Status + OpenStage1Position models (Pattern D)** | **Done** |
| **HistoricalOutcomes model (Pattern D)** | **Done** |
| **Stage1Repository — CRUD + outcome reflection (Pattern D)** | **Done** |
| **ORM tables: OpenStage1PositionRow, EarningsOutcomeRow (Pattern D)** | **Done** |
| **SignalGeneratorAgent** | **TODO — stub** |
| **RiskManagerAgent** | **TODO — stub** |
| **ExecutionAgent (Alpaca)** | **TODO — stub** |
| **EarningsCalendarAgent** | **TODO — calls Stage1Repository.load_historical_outcomes()** |
| **ExpiryScanner** | **TODO — calls Stage1Repository.record_outcome()** |

The three stub agents raise `NotImplementedError`. `EarningsCalendarAgent` and `ExpiryScanner`
are the next planned additions — both wire into `Stage1Repository` which is now complete.

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

5. **Two-tier LLM routing (Pattern B)** — `LLMClientFactory.quick` (Haiku) for cheap tasks
   (classification, extraction); `.deep` (Sonnet) for expensive tasks (confidence scoring,
   synthesis). Adding OpenAI/Gemini requires one new class — no agent changes.

6. **Deterministic confidence scoring (Pattern C)** — `EstimatesRenderer` pre-computes
   surprise deltas; `ConfidenceScorer` applies a per-`EventType` weight matrix. No LLM
   involvement. `EARN_MIXED` gate is 1.01 — always fails, forcing human review.

7. **Reflection loop / observed beat rates (Pattern D)** — `Stage1Repository` records
   EARN_PRE outcomes to `earnings_outcomes`. After ≥4 quarters per ticker, `load_historical_outcomes()`
   returns `source='observed'` displacing static FMP data. `record_outcome()` is idempotent
   (unique constraint on `stage1_id`). System starts in bootstrapping mode (all FMP).

8. **LangGraph for orchestration** — Typed state graph makes the pipeline inspectable and
   testable at the graph level.

9. **SQLite default** — Zero-config for development; swap to PostgreSQL via `DATABASE_URL`.
