# gov_trade/ — Government Contract Award Pipeline

This package is the **application layer** for the gov-trade pipeline. It contains
only the pieces that are specific to the contract-award domain: agents, graph
wiring, and the entry point. All shared infrastructure (models, providers,
services, data files) lives in `src/news_trade/` and is imported from there.

---

## Dependency direction

```
gov_trade  →  news_trade  (one-way; gov_trade imports from news_trade, never the reverse)
```

Key imports gov_trade uses from news_trade:

| What | From |
|---|---|
| `ContractAwardEvent`, `EntityResolution`, `MaterialityResult` | `news_trade.models.contracts` |
| `TradeSignal`, `SignalDirection` | `news_trade.models.signals` |
| `ContractProvider`, `EdgarProvider` | `news_trade.providers.base` / `entity_resolver` |
| `USASpendingProvider`, `MockContractProvider`, `SECEdgarProvider` | `news_trade.providers.contracts.*` |
| `LLMmaterialityProvider`, `HeuristicMaterialityProvider` | `news_trade.providers.materiality.*` |
| `EntityResolutionService` | `news_trade.services.entity_resolver` |
| `LobbyingEnrichmentService` | `news_trade.services.lobbying_enrichment` |
| `LLMClientFactory` | `news_trade.services.llm_client` |
| `EventBus` | `news_trade.services.event_bus` |
| `build_session_factory` | `news_trade.services.database` |
| `GovTradeSettings` | `news_trade.config` |
| `BaseAgent` | `news_trade.agents.base` |
| `RiskManagerAgent`, `ExecutionAgent` | `news_trade.agents.*` (reused unchanged) |

---

## Package layout (target state after all tasks complete)

```
src/gov_trade/
├── __init__.py
├── CLAUDE.md               ← this file
├── main.py                 ← entry point: polling loop, DI wiring, SIGINT handling
├── agents/
│   ├── __init__.py
│   ├── contract_poller.py       ← wraps ContractProvider; writes contract_awards to state
│   ├── entity_resolver_agent.py ← wraps EntityResolutionService; resolves tickers
│   └── materiality_scorer.py    ← wraps materiality provider + lobbying enrichment
└── graph/
    ├── __init__.py
    ├── state.py                 ← GovTradeState TypedDict
    └── pipeline.py              ← build_gov_pipeline(); conditional early-exit edges
```

---

## Pipeline shape

```
ContractPollerAgent          fetch new awards from USASpending
    ↓  no awards? → END
EntityResolverAgent          map recipient names → tickers
    ↓  no resolutions? → END
MaterialityScorerAgent       score + enrich each award
    ↓  no results? → END
_synthesise_node             convert MaterialityResult → SentimentResult + NewsEvent
    ↓
MarketDataAgent              fetch OHLCV snapshots for resolved tickers (reused)
    ↓
SignalGeneratorAgent         reused from news_trade unchanged
    ↓
RiskManagerAgent             reused from news_trade unchanged
    ↓  halt | execute | end
HaltHandlerAgent | ExecutionAgent
    ↓
END
```

Early-exit conditions (conditional edges):
- No awards fetched → END
- No awards resolved to tickers above confidence threshold → END
- No awards pass materiality threshold → END
- All signals rejected by risk → END

The `_synthesise_node` bridges the gov-trade domain into the format
`SignalGeneratorAgent` already understands: each `MaterialityResult` becomes
one `NewsEvent` (source=`usaspending`, event_type=`OTHER`) and one
`SentimentResult` (label derived from `MaterialityDirection`, score/confidence
normalised from `adjusted_score / 10`). No modification to downstream agents.

---

## GovTradeState keys

| Key | Type | Populated by |
|---|---|---|
| `contract_awards` | `list[ContractAwardEvent]` | `ContractPollerAgent` |
| `last_poll_govtrade` | `datetime` | `ContractPollerAgent` |
| `resolutions` | `list[EntityResolution]` | `EntityResolverAgent` |
| `materiality_results` | `list[MaterialityResult]` | `MaterialityScorerAgent` |
| `news_events` | `Annotated[list[NewsEvent], operator.add]` | `_synthesise_node` |
| `sentiment_results` | `list[SentimentResult]` | `_synthesise_node` |
| `market_context` | `dict[str, MarketSnapshot]` | `MarketDataAgent` (reused) |
| `trade_signals` | `list[TradeSignal]` | `SignalGeneratorAgent` (reused) |
| `approved_signals` | `list[TradeSignal]` | `RiskManagerAgent` (reused) |
| `rejected_signals` | `list[TradeSignal]` | `RiskManagerAgent` (reused) |
| `orders` | `list[Order]` | `ExecutionAgent` (reused) |
| `portfolio` | `PortfolioState` | (passed in at pipeline start) |
| `errors` | `Annotated[list[str], operator.add]` | All agents |
| `system_halted` | `bool` | `RiskManagerAgent` (reused) |

---

## Agent conventions

All agents in this package subclass `BaseAgent` from `news_trade.agents.base`:

```python
from news_trade.agents.base import BaseAgent

class ContractPollerAgent(BaseAgent):
    def __init__(
        self,
        settings: GovTradeSettings,
        event_bus: EventBus,
        provider: ContractProvider,
        last_poll: datetime | None = None,
    ) -> None: ...

    async def run(self, state: dict) -> dict: ...
```

- Provider/service dependencies injected at construction time — never fetched from globals
- `run()` reads from state, writes only its own keys, never mutates keys it doesn't own
- Errors caught and appended to `state["errors"]`; never propagated as exceptions

---

## Entry point

`gov_trade.main:entrypoint` is registered in `pyproject.toml`:

```
gov-trade = "gov_trade.main:entrypoint"
```

`main.py` responsibilities:
1. Load `GovTradeSettings`
2. Construct all providers and services with DI
3. Build and compile the graph via `build_gov_pipeline()`
4. Run the polling loop — sleep `usaspending_poll_interval_minutes` between cycles
5. Handle `SIGINT` / `SIGTERM` gracefully

---

## Build status

| Component | Status |
|---|---|
| `__init__.py` | Done |
| `agents/contract_poller.py` | Done (Task 7) |
| `agents/entity_resolver_agent.py` | Done (Task 7) |
| `agents/materiality_scorer.py` | Done (Task 7) |
| `graph/__init__.py` | Done (Task 8) |
| `graph/state.py` | Done (Task 8) — `GovTradeState` TypedDict |
| `graph/pipeline.py` | Done (Task 8) — `build_gov_pipeline()` + `_synthesise_node` + `build_gov_pipeline_from_settings()` |
| `main.py` | Done (Task 10) — polling loop, DI wiring, SIGINT/SIGTERM, `--once` flag |
