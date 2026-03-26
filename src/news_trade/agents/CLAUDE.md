# agents/ — LangGraph Agent Implementations

Each agent is a node in the LangGraph pipeline. Agents receive `PipelineState`,
compute their stage, write results back, and return the updated state.

---

## Implementation Status

| Agent | File | Status |
|---|---|---|
| `NewsIngestorAgent` | `news_ingestor.py` | Done |
| `MarketDataAgent` | `market_data.py` | Done |
| `SentimentAnalystAgent` | `sentiment_analyst.py` | Done |
| `SignalGeneratorAgent` | `signal_generator.py` | **Done — Pattern A implemented** |
| `RiskManagerAgent` | `risk_manager.py` | **STUB — all methods raise `NotImplementedError`** |
| `ExecutionAgent` | `execution.py` | **STUB — all methods raise `NotImplementedError`** |
| `OrchestratorAgent` | `orchestrator.py` | Not used — pipeline built via `graph/pipeline.py` directly |

---

## BaseAgent Contract (`base.py`)

```python
class BaseAgent(ABC):
    def __init__(self, settings: Settings, event_bus: EventBus) -> None: ...
    self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def run(self, state: dict) -> dict: ...
```

All agents inherit `BaseAgent`. Additional dependencies (providers, db session,
repositories) are injected in the subclass `__init__` — never fetched from globals.

---

## PipelineState Keys — What Each Agent Reads / Writes

| Agent | Reads | Writes |
|---|---|---|
| `NewsIngestorAgent` | `last_poll` | `news_events`, `errors` |
| `MarketDataAgent` | `news_events` | `market_context` (dict[ticker → MarketSnapshot]) |
| `SentimentAnalystAgent` | `news_events` | `sentiment_results` |
| `SignalGeneratorAgent` | `sentiment_results`, `market_context` | `trade_signals` |
| `RiskManagerAgent` | `trade_signals`, `portfolio` | `approved_signals`, `rejected_signals` |
| `ExecutionAgent` | `approved_signals` | `orders` |

Full `PipelineState` schema lives in `graph/state.py`.

---

## Pipeline Topology

```
NewsIngestorAgent
    │ news_events empty? → END
    ↓
MarketDataAgent → SentimentAnalystAgent → SignalGeneratorAgent → RiskManagerAgent
                                                                      │ no approved signals? → END
                                                                      ↓
                                                               ExecutionAgent → END
```

Routing logic: `graph/pipeline.py` — `_has_news_events()` and `_has_approved_signals()`.

---

## Adding a New Agent

1. Subclass `BaseAgent` in a new file.
2. Accept provider/service dependencies in `__init__` (not via globals).
3. Read from `state`, write results back, return updated `state`.
4. Register the new node in `graph/pipeline.py` and add it to `build_pipeline()`.
5. Add routing edge if the agent can short-circuit downstream.
6. Raise `NotImplementedError` for unimplemented sub-methods (never silently pass).

---

## Implemented: `SignalGeneratorAgent`

Accepts `llm: LLMClientFactory` at construction time (no other service deps yet).

### What is implemented (Pattern A)

| Method | Purpose |
|---|---|
| `run(state)` | Iterates `sentiment_results`, pairs with `market_context`, calls `_build_signal()`, then `_debate_signal()` for gate-passed signals |
| `_build_signal(sentiment, market_ctx)` | Maps label → direction; computes conviction; applies `min_signal_conviction` threshold; returns `TradeSignal` or `None` |
| `_compute_position_size(ticker, conviction, volatility)` | Volatility-adjusted heuristic: `max(1, int(conviction / max(vol, 0.01) * 10))` |
| `_compute_stop_loss(entry, volatility, direction)` | 2× daily vol proxy offset from entry; LONG → below, SHORT → above |
| `_debate_signal(signal)` | Bull/bear debate gate — skips if `signal_debate_rounds=0` or below `signal_debate_threshold`; applies CONFIRM/REDUCE/REJECT verdict |

Prompt helpers are module-level functions: `_build_bull_prompt`, `_build_bear_prompt`,
`_build_synthesis_prompt`. The synthesis uses `response_schema=_DebateVerdictSchema`
(structured output via tool-use).

### What is NOT yet implemented in `SignalGeneratorAgent`

The following EARN_* logic is deferred to a future PR (requires `ConfidenceScorer` and
`Stage1Repository` injection):
- `EARN_PRE` — size from `historical_beat_rate`, persist `OpenStage1Position`
- `EARN_BEAT/MISS` — load open Stage 1 position, confirm/reverse
- `EARN_MIXED` — emit EXIT signal (ConfidenceScorer gate 1.01 always fails — by design)

See `docs/architecture/event-driven-signal-layer.md §3` for the full decision tree.

## Stub Agents

### `RiskManagerAgent`

Five check layers (fail-fast, in order):
1. `passed_confidence_gate` — reject if False
2. Drawdown halt — reject + set `system_halted=True` if portfolio drawdown ≥ `max_drawdown_pct`
3. Concentration limit — reject if `open_positions >= max_open_positions` (Stage 2 ADD exempt)
4. Pending order conflict — reject if ticker already has a pending order
5. Position size cap — reduce `size_pct` to `max_position_pct` (soft limit, not reject)

Inject `stage1_repo.load_all_open()` for the concentration check.
See `docs/architecture/event-driven-signal-layer.md §7`.

### `ExecutionAgent`

Wraps Alpaca via `alpaca-py`. Constructor receives `AlpacaTradingClient`.
Methods: `_submit_order()`, `_sync_order_status()`, `_cancel_order()`.
Persists every `Order` to `OrderRow` via injected `Session`.
