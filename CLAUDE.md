# CLAUDE.md — AI Assistant Guide for news-trade

This file provides context for AI assistants (Claude Code, etc.) working in this repository.

---

## Project Overview

**news-trade** is a multi-agent, news-driven automated trading system. It ingests financial news, classifies sentiment via the Anthropic Claude API, generates trade signals, manages risk, and executes orders through Alpaca Markets.

The pipeline is orchestrated with **LangGraph** (a stateful agent graph framework). Each stage is an independent agent that reads from and writes to a shared `PipelineState` TypedDict.

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

## Repository Layout

```
src/news_trade/
├── config.py              # Pydantic Settings — all configuration & env vars
├── main.py                # Entrypoint: builds pipeline, runs polling loop
├── agents/
│   ├── base.py            # Abstract BaseAgent (all agents inherit this)
│   ├── news_ingestor.py   # Fetch, deduplicate, persist news events (DONE)
│   ├── market_data.py     # Fetch OHLCV bars + volatility metrics (DONE)
│   ├── sentiment_analyst.py  # Classify sentiment with Claude or keywords (DONE)
│   ├── signal_generator.py   # Generate TradeSignals — STUB (NotImplementedError)
│   ├── risk_manager.py       # Validate signals against risk rules — STUB
│   └── execution.py          # Place/track Alpaca orders — STUB
├── graph/
│   ├── state.py           # PipelineState TypedDict (shared data between agents)
│   └── pipeline.py        # LangGraph StateGraph builder + conditional routing
├── models/
│   ├── events.py          # NewsEvent, EventType enum
│   ├── market.py          # MarketSnapshot, OHLCVBar, ATR / volume metrics
│   ├── sentiment.py       # SentimentResult, SentimentLabel enum
│   ├── signals.py         # TradeSignal, SignalDirection enum
│   ├── orders.py          # Order, OrderSide / Status / Type enums
│   └── portfolio.py       # PortfolioState, Position
├── providers/
│   ├── base.py            # Protocol definitions (structural subtyping)
│   ├── __init__.py        # Factory functions: get_*_provider(settings)
│   ├── news/
│   │   ├── rss.py         # RSSNewsProvider (free: Yahoo Finance, MarketWatch, SEC)
│   │   └── benzinga.py    # BenzingaNewsProvider (premium)
│   ├── market/
│   │   ├── yfinance.py    # YFinanceMarketProvider (free)
│   │   ├── polygon_free.py   # PolygonFreeMarketProvider
│   │   └── polygon_paid.py   # PolygonPaidMarketProvider (Starter+)
│   └── sentiment/
│       ├── claude.py      # ClaudeSentimentProvider — uses Claude API, budget-capped
│       └── keyword.py     # KeywordSentimentProvider — heuristic fallback
├── services/
│   ├── database.py        # SQLAlchemy engine + session factory
│   ├── event_bus.py       # Async Redis pub/sub wrapper
│   ├── llm_client.py      # LLMClient Protocol, AnthropicLLMClient, LLMClientFactory
│   └── tables.py          # ORM table definitions (NewsEventRow, TradeSignalRow, OrderRow)
└── py.typed               # PEP 561 marker

tests/
├── test_models.py         # 44+ Pydantic model tests
├── test_pipeline.py       # 10+ LangGraph graph construction / routing tests
├── test_providers.py      # 25+ provider Protocol compliance + factory tests
├── test_news_ingestor.py  # NewsIngestorAgent with mocked provider
├── test_llm_client.py     # 19 LLMClient protocol, factory, and invoke tests
└── test_risk_rules.py     # Placeholder risk tests (all skipped)
```

---

## Pipeline Architecture

The LangGraph pipeline flows through agents in order, with conditional short-circuit logic:

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

The shared state between all agents is `PipelineState` (`graph/state.py`), a `TypedDict` containing lists of `NewsEvent`, `MarketSnapshot`, `SentimentResult`, `TradeSignal`, `Order`, and `PortfolioState`.

---

## Agent Design Pattern

All agents inherit from `BaseAgent` (`agents/base.py`) and implement:

```python
async def run(self, state: PipelineState) -> PipelineState:
    ...
```

Agents receive **external dependencies via constructor injection** (not via globals). Example:

```python
agent = NewsIngestorAgent(
    settings=settings,
    provider=get_news_provider(settings),
    db_session=session,
    event_bus=bus,
)
```

When implementing a new agent:
1. Subclass `BaseAgent`
2. Accept provider/service dependencies in `__init__`
3. Read from `state`, compute, write results back to `state`, return updated `state`
4. Raise `NotImplementedError` for unimplemented sub-methods rather than silently passing

---

## Provider Abstraction Layer

Providers are defined as **Protocols** (`providers/base.py`), enabling structural subtyping without forcing inheritance:

```python
class NewsProvider(Protocol):
    async def fetch_news(self, tickers: list[str]) -> list[NewsEvent]: ...

class MarketDataProvider(Protocol):
    async def get_snapshots(self, tickers: list[str]) -> list[MarketSnapshot]: ...

class SentimentProvider(Protocol):
    async def analyze(self, event: NewsEvent) -> SentimentResult: ...
```

**Factory functions** in `providers/__init__.py` select the concrete implementation from `Settings`:

```python
provider = get_news_provider(settings)        # → RSSNewsProvider or BenzingaNewsProvider
provider = get_market_data_provider(settings) # → YFinance, PolygonFree, or PolygonPaid
provider = get_sentiment_provider(settings)   # → ClaudeSentimentProvider or KeywordSentimentProvider
```

When adding a new provider:
1. Implement the relevant Protocol in a new file under `providers/<category>/`
2. Add the provider type to the enum in `config.py`
3. Wire it into the factory function in `providers/__init__.py`
4. Add Protocol compliance tests in `tests/test_providers.py`

---

## Configuration (`config.py`)

All configuration comes from environment variables via `pydantic-settings`. Key settings:

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

Copy `.env.example` to `.env` and populate before running.

---

## Development Workflow

### Setup

```bash
uv sync                          # Install all dependencies (including dev extras)
cp .env.example .env             # Configure environment variables
docker compose up -d             # Start Redis
```

### Running

```bash
uv run news-trade                # Start the main polling loop
```

### Linting & Formatting

```bash
uv run ruff check src/ tests/    # Lint (rules: E, F, I, N, UP, B, SIM, RUF)
uv run ruff format src/ tests/   # Auto-format
```

Ruff replaces both flake8 and isort. Always run `ruff check` before committing. Fix all errors; do not suppress with `# noqa` unless absolutely necessary and commented with a reason.

### Type Checking

```bash
uv run mypy src/                 # Strict mypy — no implicit Any allowed
```

All public APIs must be fully typed. Use `from __future__ import annotations` for forward references.

### Testing

```bash
uv run pytest                    # Run all tests
uv run pytest tests/test_models.py -v   # Run a specific file
uv run pytest -x                 # Stop at first failure
```

- Async tests use `@pytest.mark.asyncio` and are auto-configured via `pyproject.toml`
- Use `unittest.mock.AsyncMock` / `MagicMock` for provider/service dependencies
- Do not make real network calls or hit real APIs in tests

---

## Code Conventions

### General

- Python 3.11+ — use `match`/`case`, `tomllib`, `ExceptionGroup` where appropriate
- Prefer `async def` for I/O-bound operations (all provider calls are async)
- Use `pydantic.BaseModel` (v2 style) for all data transfer objects
- Use `dataclasses` only for simple internal structs without validation

### Imports

- Standard library → third-party → local (ruff enforces this via `I` rules)
- Absolute imports only; no relative imports (`from .foo import bar` is allowed within a package)

### Error Handling

- Raise specific exceptions; never silently swallow errors
- Log at `WARNING` or `ERROR` level before re-raising or returning a degraded result
- The `ClaudeSentimentProvider` returns a neutral `SentimentResult` (not an exception) when the daily budget is exhausted — follow this pattern for graceful degradation

### Naming

- Classes: `PascalCase`
- Functions/methods/variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private methods: `_single_leading_underscore`

### Pydantic Models

- Always set `model_config = ConfigDict(frozen=True)` for immutable value objects
- Validate at the boundary; trust internal data after construction
- Use `Annotated` types for field constraints (e.g., `Annotated[float, Field(ge=0.0)]`)

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
| **LLMClient service (deep/quick split)** | **Done** |
| **SignalGeneratorAgent** | **TODO — stub** |
| **RiskManagerAgent** | **TODO — stub** |
| **ExecutionAgent (Alpaca)** | **TODO — stub** |

The three stub agents raise `NotImplementedError` for their core methods. These are the primary areas for future development.

---

## CI/CD

GitHub Actions workflows (`.github/workflows/`):

| Workflow | Trigger | What it does |
|---|---|---|
| `tests.yml` | PR opened/updated | Run `pytest` against all tests |
| `claude.yml` | `@claude` mention in issues/PRs | Claude Code handles the request |
| `claude-code-review.yml` | PR opened/updated | Automated code review by Claude |

All PRs must pass the `tests.yml` check before merging.

---

## Key Design Decisions

1. **Protocol-based providers** — Structural subtyping (not ABC inheritance) allows swapping providers without touching agent code. Test mocks just need to implement the right methods.

2. **Dependency injection everywhere** — Agents and services receive their dependencies at construction time. No module-level singletons. This makes unit testing straightforward.

3. **Daily budget cap on Claude** — `ClaudeSentimentProvider` tracks cumulative token cost per day and falls back to a neutral result instead of raising once the cap is hit. This prevents runaway API spend.

4. **Keyword pre-filter** — When `NEWS_KEYWORD_PREFILTER=true`, news events without relevant financial keywords skip Claude entirely, reducing cost.

5. **Two-tier LLM routing** — `LLMClientFactory` (`services/llm_client.py`) exposes a `.quick` client (Haiku) and a `.deep` client (Sonnet) via the `LLMClient` Protocol. Cheap tasks (classification, extraction, debate rounds) route to quick; expensive tasks (confidence scoring, signal synthesis) route to deep. Adding a second provider (OpenAI, Gemini) requires one new class in `llm_client.py` — no agent changes. Every `SentimentResult` and `TradeSignal` records `model_id` and `provider` for provenance tracking.

6. **LangGraph for orchestration** — Using a typed state graph makes the pipeline inspectable, testable at the graph level, and easy to extend with new conditional branches.

7. **SQLite default** — Zero-config persistence for development; swap to PostgreSQL via `DATABASE_URL` for production.
