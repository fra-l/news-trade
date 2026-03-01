# news-trade

A multi-agent, news-driven trading system built with **LangGraph**, **Claude**, and **Alpaca Markets**.

The system monitors financial news in real time, analyses sentiment with Claude, generates trade signals, validates them against risk rules, and executes paper trades — all coordinated through a LangGraph state graph.

## Architecture

```
NewsIngestorAgent → MarketDataAgent → SentimentAnalystAgent
    → SignalGeneratorAgent → RiskManagerAgent → ExecutionAgent
```

All agents communicate via typed **Pydantic v2** models. The **OrchestratorAgent** wires them together as a LangGraph `StateGraph` with conditional edges for early exit (no news, all signals rejected).

## Agents

| Agent | Module | Responsibility |
|---|---|---|
| **NewsIngestorAgent** | `agents/news_ingestor.py` | Polls Benzinga / Polygon.io, filters by watchlist, deduplicates |
| **MarketDataAgent** | `agents/market_data.py` | Fetches OHLCV bars and computes volatility via Alpaca |
| **SentimentAnalystAgent** | `agents/sentiment_analyst.py` | Classifies news sentiment using Claude (`claude-sonnet-4-6`) |
| **SignalGeneratorAgent** | `agents/signal_generator.py` | Combines sentiment + market context into trade signals |
| **RiskManagerAgent** | `agents/risk_manager.py` | Validates signals against position limits, drawdown, and conflicts |
| **ExecutionAgent** | `agents/execution.py` | Places and manages orders on Alpaca paper trading |
| **OrchestratorAgent** | `agents/orchestrator.py` | Builds and runs the LangGraph pipeline |

## Data Models

| Model | File | Description |
|---|---|---|
| `NewsEvent` | `models/events.py` | Ingested news article with tickers and event type |
| `SentimentResult` | `models/sentiment.py` | Claude sentiment classification with score and confidence |
| `TradeSignal` | `models/signals.py` | Proposed trade with direction, size, stop-loss, and take-profit |
| `Order` | `models/orders.py` | Alpaca order with lifecycle tracking |
| `PortfolioState` | `models/portfolio.py` | Account and position snapshot for risk checks |

## Stack

- **Python 3.11+**
- **LangGraph** — multi-agent orchestration via state graph
- **Anthropic Claude API** — sentiment analysis (`claude-sonnet-4-6`)
- **Alpaca Markets API** — paper trading execution and market data
- **Benzinga / Polygon.io** — news ingestion
- **Redis** — inter-agent event bus (pub/sub)
- **SQLite + SQLAlchemy** — trade logging and signal history
- **Pydantic v2** — data validation across all agent boundaries

## Quick Start

```bash
# Install with uv
uv sync

# Copy and fill in API keys
cp .env.example .env

# Run the system
uv run news-trade
```

## Configuration

All settings are loaded from environment variables or a `.env` file. See `src/news_trade/config.py` for the full list. Key variables:

```
ANTHROPIC_API_KEY=...
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
BENZINGA_API_KEY=...
REDIS_URL=redis://localhost:6379/0
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
├── config.py              # Pydantic BaseSettings configuration
├── main.py                # Entrypoint — pipeline loop
├── agents/
│   ├── base.py            # Abstract BaseAgent
│   ├── news_ingestor.py   # NewsIngestorAgent
│   ├── market_data.py     # MarketDataAgent
│   ├── sentiment_analyst.py # SentimentAnalystAgent
│   ├── signal_generator.py  # SignalGeneratorAgent
│   ├── risk_manager.py    # RiskManagerAgent
│   ├── execution.py       # ExecutionAgent
│   └── orchestrator.py    # OrchestratorAgent
├── models/
│   ├── events.py          # NewsEvent
│   ├── sentiment.py       # SentimentResult
│   ├── signals.py         # TradeSignal
│   ├── orders.py          # Order, OrderStatus
│   └── portfolio.py       # PortfolioState, Position
├── services/
│   ├── database.py        # SQLAlchemy engine/session
│   └── event_bus.py       # Redis pub/sub wrapper
└── graph/
    ├── state.py           # PipelineState TypedDict
    └── pipeline.py        # LangGraph StateGraph builder
```
