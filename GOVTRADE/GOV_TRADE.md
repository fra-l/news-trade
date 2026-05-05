# gov-trade: Government Contract Signal Pipeline

## Purpose

A second trading pipeline that lives alongside `news-trade` in this repository.
It monitors US federal contract awards from public data sources, identifies
material events for publicly traded companies, and generates trade signals using
the same execution and risk infrastructure already in place.

This is **not** a rewrite of `news-trade`. It is an extension. Every piece of
shared infrastructure (LLM routing, Redis event bus, Alpaca execution, risk
management, Alembic migrations, Docker setup) must be reused without
modification. New code should only exist where the domain genuinely differs.

---

## Core Thesis

Federal contract awards are public, legally required disclosures. They often
precede press coverage or analyst attention by hours to days, especially for
small and mid-cap companies. The pipeline exploits this lead time by detecting
awards that are material relative to the recipient company's size, then acting
before the market fully prices the information in.

---

## Integration Strategy

Before writing any code, read the existing codebase carefully:

1. Read `CLAUDE.md` to understand conventions and structure
2. Study `providers/base.py` — all new providers must implement those protocols
3. Study `agents/base.py` — all new agents must extend `BaseAgent`
4. Study `services/llm_client.py` — use `LLMClientFactory`, never instantiate LLM clients directly
5. Study `graph/pipeline.py` and `graph/state.py` — the new pipeline must follow the same `StateGraph` pattern
6. Study `config.py` — all new settings go through `Pydantic BaseSettings`, never raw `os.environ`

The goal is that someone reading both pipelines sees the same idioms, the same
dependency injection pattern, the same provider abstraction, and the same graph
shape — just with different domain objects.

---

## Pipeline Shape

The graph should mirror the existing one in structure:

```
ContractPollerAgent
    → EntityResolverAgent
        → MaterialityScorerAgent
            → SignalGeneratorAgent   ← reuse existing
                → RiskManagerAgent   ← reuse existing
                    → ExecutionAgent ← reuse existing
```

Early-exit conditions (analogous to "no news" / "all signals rejected") should
be implemented as conditional edges, exactly as the existing pipeline does.

---

## New Components to Build

### 1. Data Source: USASpending.gov

The primary data source is the free, unauthenticated REST API at
`api.usaspending.gov`. No API key is required.

The poller should:
- Query awards from the last polling window (configurable, default 1 hour)
- Filter by award type (contracts only, not grants or loans)
- Filter by a configurable minimum award value (default $5M)
- Deduplicate against Redis using award ID as the key
- Emit structured `ContractAwardEvent` objects

Design the poller as a provider implementing the existing `NewsProvider`
protocol or a new analogous `ContractProvider` protocol — whichever fits more
naturally after reading the existing code. The agent above it should not care
which data source is underneath.

A secondary provider for **SEC EDGAR full-text search** should be built as an
optional enrichment source that can confirm whether a company is publicly listed
before the entity resolution step.

### 2. Entity Resolution Service

This is the hardest and most novel part of the pipeline. The USASpending API
returns recipient names like "PALANTIR TECHNOLOGIES INC - FEDERAL" or
"BOOZ ALLEN HAMILTON HOLDING CORPORATION". These must be mapped to stock tickers.

Build a service (not an agent — it is synchronous and has no LLM calls of its
own unless the other layers fail) with three resolution layers:

**Layer 1 — Static lookup table**
A CSV or JSON file shipped with the repo mapping known government contractor
names (and common aliases) to tickers. Seed it with the top 200 US government
contractors. This should be fast and cover the majority of large awards.

**Layer 2 — SEC EDGAR company search**
If Layer 1 misses, query the free EDGAR full-text search API using the
recipient name. Parse the response to extract CIK, then resolve to ticker using
the EDGAR company facts endpoint. This layer handles mid-cap companies not in
the static table.

**Layer 3 — LLM fallback**
If Layers 1 and 2 both fail, use the quick LLM model (via `LLMClientFactory`)
to attempt resolution from the company name, description, and agency context.
This is the most expensive layer and should only fire when the others fail.
Prompt it to return a structured response including confidence level. Discard
low-confidence resolutions rather than trading on guesses.

The service must return a result object that includes: the resolved ticker (or
None), confidence level, resolution layer used, and any reasoning. Log all
resolution attempts to the database for later analysis.

### 3. Materiality Scorer

This replaces `SentimentAnalystAgent` in the new pipeline. It should follow
the exact same structure — a provider injected via constructor, same `BaseAgent`
inheritance, same output model pattern.

The scorer must evaluate:
- Award value as a percentage of estimated annual revenue (use market cap as a
  proxy if revenue is unavailable)
- Whether this is a new customer relationship, an expansion, or a renewal
- The strategic significance of the awarding agency for this company's sector
- Whether the award description suggests recurring revenue or one-time work

Output a structured `MaterialityResult` model (analogous to `SentimentResult`)
with a numeric score, a human-readable reasoning string, a signal direction
(bullish / neutral / bearish), and a novelty classification.

Build two providers:
- `LLMmateriality Provider` — uses `LLMClientFactory`, respects daily budget cap
  (reuse the same budget mechanism from `providers/sentiment/claude.py`)
- `HeuristicMaterialityProvider` — rule-based fallback using award size
  thresholds and agency classification without any LLM call

### 4. New Pydantic Models

Define in `models/contracts.py`:
- `ContractAwardEvent` — the raw award from USASpending (award ID, recipient
  name, amount, agency, description, period of performance, sign date)
- `EntityResolution` — result of the resolution service (ticker, confidence,
  layer used, reasoning)
- `MaterialityResult` — output of the scorer (score 0–10, direction, novelty,
  reasoning)

These models should follow the same style as existing models: Pydantic v2,
frozen where appropriate, all fields documented with Field descriptions.

### 5. Pipeline Entry Point

Add a `gov-trade` entry point to `pyproject.toml` alongside the existing
`news-trade` entry point. The new pipeline should have its own `main.py`
equivalent under `src/gov_trade/` but share all infrastructure from
`src/news_trade/` by direct import — no copying.

### 6. Configuration

Add a `GovTradeSettings` section to `config.py` (or a new `gov_config.py` if
the existing file is hard to extend cleanly — use your judgment). New settings:

- `GOV_TRADE_ENABLED` — bool, default false (so existing `news-trade` startup
  is unaffected)
- `USASPENDING_POLL_INTERVAL_MINUTES` — int, default 60
- `USASPENDING_MIN_AWARD_USD` — int, default 5_000_000
- `USASPENDING_AWARD_TYPES` — list of type codes, default contracts only
- `ENTITY_RESOLUTION_MIN_CONFIDENCE` — float 0–1, default 0.7
- `GOV_TRADE_MARKET_CAP_MAX_USD` — int, max market cap for eligible tickers
  (focus on small/mid cap), default 5_000_000_000
- `GOV_TRADE_LLM_PROVIDER` — independent of the news-trade LLM setting, same
  valid values (anthropic / ollama)

---

## What NOT to Build

Do not build:
- A dashboard or UI (out of scope for this phase)
- A backtesting engine (log signals; backtest separately later)
- A new Redis setup, Docker config, or Alembic setup — extend the existing ones
- Any agent that bypasses the provider abstraction and calls APIs directly

---

## Database

Add new Alembic migration(s) for:
- `contract_awards` table — raw events from USASpending
- `entity_resolutions` table — resolution attempts and outcomes (for later
  accuracy analysis)
- `gov_trade_signals` table — signals generated by this pipeline (separate from
  `news-trade` signals for clean attribution)

Use the existing `services/database.py` session factory without modification.

---

## Testing

Follow the existing test conventions (look at `tests/` before writing anything).

At minimum, add:
- Unit tests for each resolution layer in `EntityResolutionService` with mocked
  HTTP responses
- Unit tests for `HeuristicMaterialityProvider` covering edge cases (tiny
  award on large-cap, massive award on micro-cap)
- Integration test for the full pipeline using a fixture `ContractAwardEvent`
  and asserting a `TradeSignal` is produced

---

## Coding Constraints

Follow all conventions already established in `CLAUDE.md`. Additionally:

- No `requests` — use `httpx` (async) consistent with existing HTTP usage
- No new top-level dependencies unless strictly necessary; check `pyproject.toml`
  first — the needed library may already be available
- All new async functions must be properly awaited; no `asyncio.run()` inside
  agent methods
- Type annotations on every function signature — no `Any` unless genuinely
  unavoidable
- Provider classes must be injectable and testable in isolation (no singleton
  state)
