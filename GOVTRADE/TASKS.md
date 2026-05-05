# gov-trade Build Tasks

Work through these tasks in order. Do not skip ahead. Each task ends with a
checkpoint — pause and verify before continuing.

Before starting any task, re-read `GOV_TRADE.md` and `LOBBY_ENRICHMENT.md`
if you have not already.
Then read `CLAUDE.md` to understand existing conventions. Then explore the
relevant existing source files using grep and targeted line reads — do not
cat entire files speculatively.

---

## Task 0 — Codebase Audit

**Goal:** Understand exactly what exists and where before touching anything.

Actions:
1. Read `CLAUDE.md` fully
2. Read `src/news_trade/agents/base.py` — understand `BaseAgent` interface
3. Read `src/news_trade/providers/base.py` — understand all provider protocols
4. Read `src/news_trade/services/llm_client.py` — understand `LLMClientFactory`
   and how quick vs deep model routing works
5. Read `src/news_trade/graph/state.py` and `graph/pipeline.py` — understand
   how the StateGraph is constructed and how state flows between nodes
6. Read `src/news_trade/config.py` — understand `Pydantic BaseSettings` pattern
7. Read `src/news_trade/models/events.py` and `models/sentiment.py` — understand
   the model style to replicate
8. Skim `alembic/` to understand migration conventions

Produce a written summary (as a comment or note, not a file) of:
- What `BaseAgent` requires subclasses to implement
- What the `NewsProvider` protocol looks like and whether a `ContractProvider`
  should mirror it or extend it
- How the StateGraph handles early exits
- What the `LLMClientFactory` signature looks like

**Checkpoint:** Do not proceed until you can answer all four questions above
from memory (i.e., you have read enough).

---

## Task 1 — Models

**Goal:** Define the new domain models that the rest of the pipeline depends on.

Create `src/news_trade/models/contracts.py` with:
- `ContractAwardEvent` — raw award data from USASpending
- `EntityResolution` — result of name-to-ticker resolution
- `MaterialityResult` — output of the materiality scorer

Study `models/events.py` and `models/sentiment.py` carefully before writing.
The style (Pydantic v2, Field descriptions, frozen config, Optional vs required)
must be consistent with existing models.

Make deliberate choices about:
- Which fields are optional vs required (USASpending data is sometimes sparse)
- Whether `MaterialityResult` should include a raw score AND a bucketed label,
  or just one of them
- How to represent confidence numerically in `EntityResolution`

**Checkpoint:** Run `uv run mypy src/` — zero type errors in the new file.

---

## Task 2 — Configuration

**Goal:** Add gov-trade settings without breaking existing config.

Extend `config.py` (or create `src/news_trade/gov_config.py` — choose based on
how the existing file is structured) with `GovTradeSettings` as described in
`GOV_TRADE.md`.

All settings must have sensible defaults so that existing `news-trade` startup
is completely unaffected when `GOV_TRADE_ENABLED=false` (the default).

**Checkpoint:** Import the new settings class in a Python REPL and verify all
defaults load without errors.

---

## Task 3 — Static Contractor Lookup Table

**Goal:** Build the foundation of entity resolution — the fast, free layer.

Create `src/news_trade/data/contractor_tickers.csv` (or `.json` — choose the
format that will be easiest to load and extend) with at minimum the top 100 US
government contractors by annual contract value. Include:
- Canonical company name (as it appears on USASpending)
- Common aliases and subsidiary names that map to the same ticker
- Stock ticker symbol
- Primary exchange

Research this list carefully — accuracy here directly affects trade quality.
Key sectors to cover: defense, IT services, healthcare/pharma, aerospace,
construction/engineering, professional services.

Also create a loader utility in `src/news_trade/services/contractor_lookup.py`
that reads this file at startup, indexes it for O(1) lookup by name, and
supports fuzzy matching for minor name variations (e.g., "Inc." vs "Inc" vs
no suffix). Choose the fuzzy matching approach — `rapidfuzz` is likely already
available; check `pyproject.toml` first.

**Checkpoint:** Write a small inline test or `__main__` block that looks up
five known contractors and five unknown ones, printing results to confirm
correct behavior.

---

## Task 4 — Entity Resolution Service

**Goal:** Build the three-layer resolution service described in `GOV_TRADE.md`.

Create `src/news_trade/services/entity_resolver.py`.

Design decisions to make explicitly:
- Should Layer 2 (EDGAR) be synchronous or async? (Hint: look at how other
  HTTP calls are made in the existing providers)
- How should failed resolutions be cached in Redis to avoid hammering EDGAR
  with repeated lookups for the same unresolvable name?
- What is the right retry/backoff strategy for EDGAR calls?
- Should the LLM fallback (Layer 3) include the award description and agency
  in the prompt, or just the company name? (Think about what gives the model
  enough context without leaking too much irrelevant text)

The service should be injectable (takes its dependencies in `__init__`) and
stateless between calls. Log every resolution attempt to the database with
enough detail to later compute per-layer accuracy.

**Checkpoint:** Unit tests for all three layers with mocked HTTP and mocked
LLM responses. Tests must pass: `uv run pytest tests/test_entity_resolver.py`

---

## Task 5 — USASpending Provider

**Goal:** Implement the data ingestion layer.

Create `src/news_trade/providers/contracts/usaspending.py` implementing
whichever provider protocol you decided on in Task 0.

Key decisions:
- How to handle pagination (USASpending uses cursor-based pagination)
- How to handle the poll window — should the provider track its own last-seen
  timestamp, or receive it as a parameter?
- What to do when the API returns partial data for an award (some fields are
  optional or null in USASpending responses)
- Whether to implement a simple `MockContractProvider` alongside it for testing

Also create `src/news_trade/providers/contracts/sec_edgar.py` as the EDGAR
enrichment provider (used by the entity resolver, not directly by the agent).

**Checkpoint:** Write an integration test (skipped by default, enabled with
`pytest -m integration`) that hits the real USASpending API and prints the
first 5 awards. Confirm the data shape matches `ContractAwardEvent`.

---

## Task 6 — Materiality Scorer Providers

**Goal:** Build both scorer implementations.

Create:
- `src/news_trade/providers/materiality/llm_provider.py`
- `src/news_trade/providers/materiality/heuristic_provider.py`

The LLM provider must:
- Use `LLMClientFactory` (never instantiate `AnthropicLLMClient` directly)
- Apply the same daily budget cap pattern as `providers/sentiment/claude.py`
- Prompt the model to return structured JSON matching `MaterialityResult`
- Fall back to `HeuristicMaterialityProvider` when budget is exhausted

The heuristic provider must not make any network calls. It should use only
the data in `ContractAwardEvent` plus the resolved ticker's market cap
(passed in). Define clear scoring thresholds and document them in the code.

Design the prompt for the LLM provider carefully:
- What context does the model need to assess materiality?
- How do you prevent it from hallucinating revenue figures?
- Should you ask for a chain-of-thought before the JSON, or JSON only?

**Checkpoint:** Unit tests for `HeuristicMaterialityProvider` covering at least:
- Award < 1% of market cap → low materiality
- Award > 20% of market cap → high materiality  
- New agency (no prior relationship) → novelty bonus
- Renewal with same agency → novelty penalty

---

## Task 6b — Lobbying Enrichment Service

**Goal:** Build the confidence multiplier layer described in `LOBBY_ENRICHMENT.md`.
Read that file fully before starting this task.

This task inserts a service — not an agent, not a graph node — that
`MaterialityScorerAgent` will call internally after computing its base score.
The pipeline graph shape does not change.

Actions:
1. Build `models/lobbying.py` — `LobbyingFiling`, `LobbyingSignal`, `EnrichmentResult`
2. Build `providers/lobbying/lda_provider.py` — Senate LDA API client with Redis
   caching and quarterly freshness awareness
3. Build `providers/lobbying/mock_provider.py` — returns configurable multipliers
   for testing without an API key
4. Build `services/lobbying_enrichment.py` — `LobbyingEnrichmentService` with
   multiplier band logic from `LOBBY_ENRICHMENT.md`
5. Build `data/agency_lda_mapping.json` — normalize USASpending agency names to
   LDA entity names for the top 50 agencies by contract volume
6. Build `scripts/seed_lobbying_data.py` — one-time bulk historical load for
   the last 8 quarters (runs at setup, not in the live pipeline)

Design decisions to make explicitly and record in the Decision Log:
- How do you represent per-agency spend breakdown from LDA filings? The raw
  data gives activity descriptions, not clean per-agency dollar splits. Decide
  whether to use proportional allocation, LLM extraction, or a simpler
  presence/absence signal.
- How do you handle companies that lobby under a different legal name than
  the one in `contractor_tickers.csv`? (e.g., "Leidos Holdings" vs "Leidos Inc")
- Should `EnrichmentResult` be attached to `MaterialityResult` as a nested
  field, or stored separately in pipeline state?

**Checkpoint:**
- With `LOBBYING_ENRICHMENT_ENABLED=false`: pipeline runs identically to
  pre-enrichment. Zero behavior change.
- With mock provider returning multiplier 1.4: adjusted score in state equals
  base score × 1.4.
- With API unavailable (network error): service returns neutral 1.0 multiplier,
  no exception propagates.
- Run: `uv run pytest tests/test_lobbying_enrichment.py` — all tests pass.

---

## Task 7 — New Agents ✅

**Goal:** Wire providers into agents following existing patterns exactly.

Create:
- `src/gov_trade/agents/contract_poller.py` — wraps `ContractProvider`
- `src/gov_trade/agents/entity_resolver_agent.py` — wraps `EntityResolutionService`
- `src/gov_trade/agents/materiality_scorer.py` — wraps materiality provider

Each agent must:
- Extend `BaseAgent`
- Receive its provider/service via constructor injection
- Read from and write to the pipeline `StateGraph` state
- Handle errors gracefully with structured logging (never let an exception
  propagate uncaught out of an agent)
- Emit early-exit signals when appropriate (no awards found, resolution failed
  for all awards, all awards below materiality threshold)

Before writing any agent, re-read `agents/news_ingestor.py` and
`agents/sentiment_analyst.py` in full. Your agents should look like siblings
of those files, not inventions.

**Checkpoint:** Each agent can be instantiated and called in isolation with a
mock provider. No LangGraph integration yet.

**Status:** Done. Tests in `tests/test_contract_poller_agent.py`,
`tests/test_entity_resolver_agent.py`, `tests/test_materiality_scorer_agent.py`
(37 tests, all passing).

---

## Task 8 — Pipeline Graph ✅

**Goal:** Wire all agents into a LangGraph `StateGraph`.

Create `src/gov_trade/graph/` (separate from the existing `graph/` — clean
coexistence via separate entry points).

The state TypedDict must include fields for all intermediate data:
contract awards list, entity resolutions, materiality results, trade signals,
risk decisions, execution results. Follow `graph/state.py` conventions exactly.

Wire agents as nodes with conditional edges for:
- No awards found → end
- No awards resolved to tickers → end
- No awards pass materiality threshold → end
- Signals generated but all rejected by risk → end
- Signals approved → execution

**Checkpoint:** Run the full pipeline end-to-end with a hardcoded fixture
`ContractAwardEvent` (skip live API calls). Confirm a `TradeSignal` reaches
the `ExecutionAgent`.

**Status:** Done. Files created:
- `src/gov_trade/graph/__init__.py`
- `src/gov_trade/graph/state.py` — `GovTradeState` TypedDict (superset of reused-agent keys)
- `src/gov_trade/graph/pipeline.py` — `build_gov_pipeline()` (all agents injected),
  `_synthesise_node` (bridges `MaterialityResult` → `SentimentResult` + `NewsEvent`),
  `build_gov_pipeline_from_settings()` convenience factory for `main.py`

Key decision: `MaterialityScorerAgent` writes `materiality_results`; a dedicated
`_synthesise_node` converts each result into `SentimentResult` + `NewsEvent` so
`SignalGeneratorAgent`, `MarketDataAgent`, `RiskManagerAgent`, and `ExecutionAgent`
run completely unchanged.

Tests in `tests/test_gov_pipeline.py` (34 tests, all passing).

---

## Task 9 — Database Migrations

**Goal:** Persist contract events, resolution attempts, and signals.

Add Alembic migrations for the three new tables described in `GOV_TRADE.md`.
Follow the existing migration naming convention in `alembic/`.

**Checkpoint:** `uv run alembic upgrade head` runs cleanly on a fresh database.
Downgrade migration also works: `uv run alembic downgrade -1`.

---

## Task 10 — Entry Point & Wiring

**Goal:** Make `gov-trade` a runnable command.

Create `src/gov_trade/main.py` — the entry point. It should:
- Load `GovTradeSettings`
- Construct all providers and services with proper dependency injection
- Build and run the pipeline graph in a polling loop
- Respect `USASPENDING_POLL_INTERVAL_MINUTES`
- Handle shutdown signals gracefully (SIGINT/SIGTERM)

Add to `pyproject.toml`:
```
[project.scripts]
gov-trade = "gov_trade.main:main"
```

Update `docker-compose.yml` to include a `gov-trade` service that shares
the same Redis instance as `news-trade` but runs independently.

Update `CLAUDE.md` to document the new pipeline, its entry point, its
config variables, and the new files added.

**Checkpoint:** `uv run gov-trade` starts without errors (even if no real
awards arrive during a short test run). `docker compose up` brings up both
services cleanly.

---

## Task 11 — Final Review

Before declaring done:

1. Run full test suite: `uv run pytest` — all tests pass
2. Run type checker: `uv run mypy src/` — zero errors
3. Run linter: `uv run ruff check src/` — zero warnings
4. Verify `CLAUDE.md` reflects all new files and entry points
5. Verify `.env.example` includes all new config variables with comments
6. Confirm that running `uv run news-trade` with default env (no
   `GOV_TRADE_ENABLED`) is completely unaffected by all new code

---

## Decision Log

As you work through these tasks, record non-obvious decisions here so future
sessions have context. Format:

```
[Task N] Decision: <what you chose>
Rationale: <why>
Alternative considered: <what else was possible>
```

Start filling this in from Task 0.

---

[Task 7] Decision: Agents live in `src/gov_trade/agents/`, not `src/news_trade/agents/`
Rationale: gov_trade is a separate entry point; keeping its agents in its own package
enforces the one-way dependency rule (`gov_trade → news_trade`, never the reverse).
Alternative considered: placing agents in `news_trade/agents/` for proximity to `BaseAgent` —
rejected because it would pollute the news-trade package with gov-trade concerns.

[Task 7] Decision: `MaterialityScorerAgent` maps `award_id` → `ContractAwardEvent` by
`recipient_name` via `_find_by_ticker` fallback, not by `award_id` key lookup.
Rationale: `EntityResolution` carries `recipient_name` but not `award_id`, so the
`award_map` keyed by `award_id` always misses and the fallback linear scan is the real
lookup path. The map is retained for potential future use when `award_id` is threaded
through the resolution.
Alternative considered: threading `award_id` through `EntityResolution` — deferred to
avoid changing the model at this stage.

[Task 8] Decision: Add a dedicated `_synthesise_node` between `MaterialityScorerAgent`
and `MarketDataAgent` rather than having `MaterialityScorerAgent` write `sentiment_results`
and `news_events` directly.
Rationale: separation of concerns — the scorer owns materiality domain output;
synthesis is a pipeline-level bridge concern. Tests for the scorer remain focused.
Alternative considered: scorer writes both formats — simpler but blurs agent responsibility.

[Task 8] Decision: `build_gov_pipeline` accepts all agents as parameters (including
the three reused ones via optional keyword args defaulting to None).
Rationale: makes the pipeline fully testable without real LLM / DB / network dependencies.
When None, agents are built from settings — production behaviour unchanged.
Alternative considered: building all agents internally (matching news-trade pattern) —
rejected because it makes the end-to-end checkpoint test impractical without patching internals.

[Task 8] Decision: `MarketDataAgent` is inserted between synthesis and `SignalGeneratorAgent`.
Rationale: `SignalGeneratorAgent` accesses `market_ctx.volatility_20d` and
`market_ctx.latest_close` directly (no None guard). Without real market snapshots
the signal logic would raise `AttributeError`. `MarketDataAgent` derives tickers from
`news_events`, which synthesis writes, so inserting it at this point requires zero changes
to either agent.
Alternative considered: synthesising stub `MarketSnapshot` objects with default values —
rejected because it hides real market data from the signal generator.
