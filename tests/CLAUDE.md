# tests/ — Test Conventions

All tests use `pytest` with `asyncio_mode = "auto"` (configured in `pyproject.toml`).
No real network calls, no real API keys, no Redis, no file-system databases.

---

## File Map

| File | What it tests | Key dependency |
|---|---|---|
| `test_models.py` | All Pydantic models | None |
| `test_pipeline.py` | LangGraph graph topology | Mock agents |
| `test_providers.py` | Protocol compliance + factory functions + `ClaudeSentimentProvider` tier routing | Mock HTTP / AsyncMock |
| `test_news_ingestor.py` | `NewsIngestorAgent` | AsyncMock provider + in-memory SQLite |
| `test_llm_client.py` | `LLMClient` protocol, factory, `AnthropicLLMClient` | `unittest.mock.patch` |
| `test_estimates_renderer.py` | `EstimatesRenderer` | None (stateless) |
| `test_confidence_scorer.py` | `ConfidenceScorer` | None (pure Python) |
| `test_stage1_repository.py` | `Stage1Repository` | In-memory SQLite |
| `test_earnings_calendar.py` | `EarningsCalendarAgent`, `EarningsCalendarEntry`, `_synthesise_event`, `estimates_provider` wiring | AsyncMock providers + in-memory SQLite |
| `test_fmp_estimates_provider.py` | `FMPEstimatesProvider` — beat rate computation, HTTP path, edge cases, protocol compliance | `sys.modules` aiohttp mock (aiohttp not installed in test env) |
| `test_risk_rules.py` | `RiskManagerAgent` — all five check layers (L1–L3c), `run()` integration, Stage 2 ADD exemption (L2b), size cap reduction (L3b) | In-memory SQLite + `MagicMock` `EventBus` |
| `test_execution.py` | `ExecutionAgent` — order side mapping, Alpaca submission, `OrderRow` persistence, `close_after_date` storage, `scan_expired_pead()` cron | MagicMock Alpaca + in-memory SQLite |
| `test_expiry_scanner.py` | `ExpiryScanner` — expired position marking, no-op on empty, real-repo integration | MagicMock `Stage1Repository` + in-memory SQLite |
| `test_signal_generator.py` | `SignalGeneratorAgent` — generic signals, debate gate, EARN_\* two-stage logic, three-tier beat-rate fallback, `_parse_calendar_fields` | MagicMock `Stage1Repository` + `ConfidenceScorer` |
| `test_watchlist_manager.py` | `WatchlistManager` — `get_active_watchlist()` fallback logic, `save_selection()`/`load_selected()` round-trip, `scan_candidates()` filtering + provider fallback, agent injection assertions | In-memory SQLite + `AsyncMock` providers |

---

## Helper Function Pattern

Every test file uses module-level helper functions (not fixtures) with a dict-merge default
pattern. This makes it easy to override a single field without spelling out all required fields.

```python
def _make_signal(**kwargs) -> TradeSignal:
    defaults: dict[str, object] = dict(
        signal_id="sig-1",
        event_id="ev-1",
        ticker="AAPL",
        direction=SignalDirection.LONG,
        conviction=0.75,
        suggested_qty=10,
    )
    return TradeSignal(**(defaults | kwargs))
```

Use `**kwargs` as the parameter type and `dict[str, object]` for defaults. The `defaults | kwargs`
merge means callers only specify what differs from the default.

---

## Test Class Organization

Group tests by the method or behaviour under test, not by input type:

```python
class TestScoreSurprise:       # tests for scorer._score_surprise()
class TestScoreSentiment:      # tests for scorer._score_sentiment()
class TestApplyGate:           # tests for scorer.apply_gate()
```

Use `setup_method(self)` for per-test setup (creates fresh instances):

```python
class TestApplyGate:
    def setup_method(self) -> None:
        self.scorer = ConfidenceScorer(settings=_make_settings())

    def test_gate_passes(self) -> None:
        ...
```

---

## Database Tests (in-memory SQLite)

For any test that needs the ORM, create a fresh in-memory database per test class:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from news_trade.services.tables import Base

def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()
```

Call `_make_session()` in `setup_method` so each test gets an isolated empty database.
Never share sessions across tests.

---

## Async Tests

Mark async test methods with `@pytest.mark.asyncio` (or rely on `asyncio_mode = "auto"`
which auto-collects coroutine test functions):

```python
async def test_fetch_returns_events(self) -> None:
    result = await self.agent.run(state)
    assert len(result["news_events"]) == 2
```

Use `unittest.mock.AsyncMock` for async provider/service dependencies:

```python
mock_provider = AsyncMock()
mock_provider.fetch.return_value = [_make_event()]
```

Use `MagicMock` for sync dependencies and `patch` for module-level functions.

---

## Model Tests

Test Pydantic models by verifying:
1. Happy path construction
2. Serialization round-trip: `Model.model_validate(model.model_dump())`
3. Field constraint violations: `pytest.raises(ValidationError)`
4. Computed field values (formula verification, not just type check)
5. Optional field defaults

```python
def test_serialization_round_trip(self) -> None:
    obj = self._make()
    restored = MyModel.model_validate(obj.model_dump())
    assert restored == obj
```

---

## What Never Goes in Tests

- Real Anthropic API calls (`ANTHROPIC_API_KEY` not set in test env)
- Real Alpaca API calls
- Real Redis connections
- Real HTTP requests to FMP, Benzinga, yfinance, or Polygon
- File-system SQLite databases (always use `sqlite:///:memory:`)
- `time.sleep()` — use `AsyncMock` and control timing explicitly
