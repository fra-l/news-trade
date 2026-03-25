# news-trade

A multi-agent, news-driven trading system built with **LangGraph**, **Claude**, and **Alpaca Markets**.

The system monitors financial news in real time, analyses sentiment with Claude, generates trade signals, validates them against risk rules, and executes paper trades — all coordinated through a LangGraph state graph.

A **provider abstraction layer** (Phase 0) lets you swap free-tier data sources for premium ones via a single environment variable — no agent code changes required.

## Architecture

```
NewsIngestorAgent → MarketDataAgent → SentimentAnalystAgent
    → SignalGeneratorAgent → RiskManagerAgent → ExecutionAgent
```

All agents communicate via typed **Pydantic v2** models. The **OrchestratorAgent** wires them together as a LangGraph `StateGraph` with conditional edges for early exit (no news, all signals rejected).

Each of the three data-facing agents receives a **provider** via constructor injection:

```
NewsIngestorAgent ←── NewsProvider      (rss | benzinga)
MarketDataAgent   ←── MarketDataProvider (yfinance | polygon_free | polygon_paid | alpaca)
SentimentAnalystAgent ←── SentimentProvider (claude | keyword)
```

## Agents

| Agent | Module | Responsibility |
|---|---|---|
| **NewsIngestorAgent** | `agents/news_ingestor.py` | Fetches news via injected provider, filters by watchlist, deduplicates |
| **MarketDataAgent** | `agents/market_data.py` | Fetches OHLCV bars and computes volatility via injected provider |
| **SentimentAnalystAgent** | `agents/sentiment_analyst.py` | Classifies news sentiment via injected provider (Claude or keyword fallback) |
| **SignalGeneratorAgent** | `agents/signal_generator.py` | Combines sentiment + market context into trade signals |
| **RiskManagerAgent** | `agents/risk_manager.py` | Validates signals against position limits, drawdown, and conflicts |
| **ExecutionAgent** | `agents/execution.py` | Places and manages orders on Alpaca paper trading |
| **OrchestratorAgent** | `agents/orchestrator.py` | Builds and runs the LangGraph pipeline |

## Providers

### News

| Provider | Type | Key |
|---|---|---|
| `RSSNewsProvider` | Free | — |
| `BenzingaNewsProvider` | Premium | `BENZINGA_API_KEY` |

### Market Data

| Provider | Type | Key |
|---|---|---|
| `YFinanceMarketProvider` | Free | — |
| `PolygonFreeMarketProvider` | Free | `POLYGON_API_KEY` |
| `PolygonPaidMarketProvider` | Premium | `POLYGON_API_KEY` |

### Sentiment

| Provider | Type | Notes |
|---|---|---|
| `ClaudeSentimentProvider` | Paid | Daily budget cap; falls back to neutral when exhausted |
| `KeywordSentimentProvider` | Free | Heuristic keyword weights, confidence fixed at 0.4 |

## Data Models

| Model | File | Description |
|---|---|---|
| `NewsEvent` | `models/events.py` | Ingested news article with tickers and event type |
| `MarketSnapshot` | `models/market.py` | OHLCV bars + volatility, ATR, and relative volume |
| `OHLCVBar` | `models/market.py` | Single candlestick bar |
| `SentimentResult` | `models/sentiment.py` | Sentiment classification with score and confidence |
| `TradeSignal` | `models/signals.py` | Proposed trade with direction, size, stop-loss, and take-profit |
| `Order` | `models/orders.py` | Alpaca order with lifecycle tracking |
| `PortfolioState` | `models/portfolio.py` | Account and position snapshot for risk checks |

## Stack

- **Python 3.11+**
- **LangGraph** — multi-agent orchestration via state graph
- **Anthropic Claude API** — two-tier LLM routing via `LLMClientFactory`: Haiku for cheap tasks, Sonnet for deep reasoning
- **Alpaca Markets API** — paper trading execution
- **RSS / Benzinga / Polygon.io** — news ingestion (switchable)
- **yfinance / Polygon.io** — market data (switchable)
- **Redis** — inter-agent event bus (pub/sub)
- **SQLite + SQLAlchemy** — trade logging and signal history
- **Pydantic v2** — data validation across all agent boundaries

## Quick Start

```bash
# Install with uv
uv sync

# Copy and fill in API keys
cp .env.example .env

# Start Redis (requires Docker)
docker compose up -d

# Run the system
uv run news-trade
```

## Configuration

All settings are loaded from environment variables or a `.env` file. See `.env.example` for the full template.

### Provider selection

```env
# Free-tier stack (default)
NEWS_PROVIDER=rss
MARKET_DATA_PROVIDER=yfinance
SENTIMENT_PROVIDER=claude

# Premium stack
NEWS_PROVIDER=benzinga
MARKET_DATA_PROVIDER=polygon_paid
SENTIMENT_PROVIDER=claude
```

### LLM tier configuration

```env
LLM_PROVIDER=anthropic                   # 'anthropic' only for now
LLM_QUICK_MODEL=claude-haiku-4-5-20251001  # cheap/fast: classification, debate rounds
LLM_DEEP_MODEL=claude-sonnet-4-6           # accurate: confidence scoring, synthesis
```

### Cost controls

```env
CLAUDE_DAILY_BUDGET_USD=2.00   # hard cap; falls back to neutral when hit
SENTIMENT_DRY_RUN=false        # set true to skip all API calls (mock scores)
NEWS_KEYWORD_PREFILTER=true    # strip non-watchlist articles before Claude
```

### Required API keys

```env
ANTHROPIC_API_KEY=sk-ant-...
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
```

## Development

```bash
# Install with dev dependencies
uv sync --group dev

# Lint
uv run ruff check src/ tests/

# Type check
uv run mypy src/

# Test
uv run pytest
```

## Project Layout

```
src/news_trade/
├── __init__.py
├── config.py              # Pydantic BaseSettings + provider enums
├── main.py                # Entrypoint — pipeline loop
├── agents/
│   ├── base.py            # Abstract BaseAgent
│   ├── news_ingestor.py   # NewsIngestorAgent (DI)
│   ├── market_data.py     # MarketDataAgent (DI)
│   ├── sentiment_analyst.py # SentimentAnalystAgent (DI)
│   ├── signal_generator.py  # SignalGeneratorAgent
│   ├── risk_manager.py    # RiskManagerAgent
│   ├── execution.py       # ExecutionAgent
│   └── orchestrator.py    # OrchestratorAgent
├── models/
│   ├── events.py          # NewsEvent
│   ├── market.py          # MarketSnapshot, OHLCVBar
│   ├── sentiment.py       # SentimentResult
│   ├── signals.py         # TradeSignal
│   ├── orders.py          # Order, OrderStatus
│   └── portfolio.py       # PortfolioState, Position
├── providers/
│   ├── base.py            # Protocol definitions
│   ├── news/
│   │   ├── rss.py         # Yahoo Finance + MarketWatch RSS (free)
│   │   └── benzinga.py    # Benzinga API (premium)
│   ├── market/
│   │   ├── yfinance.py    # yfinance library (free)
│   │   ├── polygon_free.py # Polygon.io free tier
│   │   └── polygon_paid.py # Polygon.io Starter+ (premium)
│   └── sentiment/
│       ├── claude.py      # Claude API with daily budget cap
│       └── keyword.py     # Keyword heuristic fallback (free)
├── services/
│   ├── database.py        # SQLAlchemy engine/session
│   ├── event_bus.py       # Redis pub/sub wrapper
│   └── llm_client.py      # LLMClient Protocol, AnthropicLLMClient, LLMClientFactory
└── graph/
    ├── state.py           # PipelineState TypedDict
    └── pipeline.py        # LangGraph StateGraph builder
```
