# eToro Broker Integration — Implementation Plan

Tracking document for adding eToro as an optional broker alongside Alpaca.
Steps are ordered by risk and dependency: the Protocol abstraction must ship first;
the Alpaca refactor and eToro client are deferred as high-risk future work.

---

## Step 1 — Define a `BrokerClient` Protocol (low risk)

**Goal:** Introduce a broker-agnostic interface so `ExecutionAgent` and
`HaltHandlerAgent` no longer import `alpaca.trading` directly.

**Status:** Not started

### Tasks

- [ ] Create `src/news_trade/services/broker_client.py` with a `@runtime_checkable`
      `BrokerClient` Protocol covering the five operations currently called on
      `TradingClient`:

  | Method | Used by |
  |---|---|
  | `async submit_order(signal) → Order` | `ExecutionAgent._submit_order` |
  | `async cancel_order(broker_order_id)` | `ExecutionAgent._cancel_order` |
  | `async cancel_all_orders()` | `HaltHandlerAgent._cancel_all_orders` |
  | `async close_position(ticker)` | `ExecutionAgent.scan_expired_pead` |
  | `async close_all_positions()` | `HaltHandlerAgent._close_all_positions` |
  | `async get_order(broker_order_id) → Order` | `ExecutionAgent._sync_order_status` |

- [ ] Add `BrokerProviderType` StrEnum to `config.py`:
  ```python
  class BrokerProviderType(StrEnum):
      ALPACA = "alpaca"
      ETORO = "etoro"
  ```
- [ ] Add config fields to `Settings`:
  ```python
  broker_provider: BrokerProviderType = Field(default=BrokerProviderType.ALPACA)
  etoro_api_key: str = Field(default="", description="eToro subscription key")
  etoro_demo: bool = Field(default=True, description="Use eToro demo environment")
  ```
- [ ] Add `get_broker_client(settings) → BrokerClient` factory to
      `providers/__init__.py` (or a new `services/broker_factory.py`).
- [ ] Update `ExecutionAgent.__init__` and `HaltHandlerAgent.__init__` to accept
      `broker_client: BrokerClient | None` instead of `alpaca_client: TradingClient | None`.
- [ ] Update `main.py` to call the factory and inject the result.
- [ ] Run `ruff check` and `mypy src/` — fix all errors.
- [ ] Verify all existing tests still pass (`uv run pytest`).

### Acceptance criteria
- `alpaca.trading` is no longer imported in `execution.py` or `halt_handler.py`.
- All existing tests pass without modification.
- `mypy --strict` reports no new errors.

---

## Step 2 — Wrap Alpaca in `AlpacaBrokerClient` ⚠️ HIGH RISK — POSTPONE

> **This step is intentionally deferred as a future feature.**
>
> Wrapping the existing Alpaca logic into a concrete `AlpacaBrokerClient` class
> involves moving and reshaping code that is already tested and working. The
> blast radius of a regression here is high: a broken `ExecutionAgent` or
> `HaltHandlerAgent` means live orders are neither submitted nor cancelled during
> a drawdown halt.
>
> **Do not start this step until Step 1 is merged, reviewed, and has run
> successfully in the paper-trading environment for at least one full trading week.**

### Planned tasks (do not act on yet)

- [ ] Create `src/news_trade/services/broker/alpaca_broker.py` implementing
      `BrokerClient` by moving the Alpaca-specific logic out of `execution.py`
      and `halt_handler.py`.
- [ ] Keep `_alpaca_to_order()` and `_signal_to_order_side()` as module-level
      helpers inside `alpaca_broker.py`.
- [ ] Register `AlpacaBrokerClient` in the broker factory for
      `BrokerProviderType.ALPACA`.
- [ ] Migrate `tests/test_execution.py` and `tests/test_halt_handler.py` to
      inject `AlpacaBrokerClient` mocks via the Protocol rather than raw
      `TradingClient` mocks.

### Known risks
- Any logic error in the wrapper silently breaks order submission or halt cleanup.
- `asyncio.to_thread` wrapping must be preserved exactly — Alpaca's SDK is sync.
- `cancel_orders()` (plural, no args) vs `cancel_order_by_id()` are different
  methods on `TradingClient`; the Protocol must expose both semantics cleanly.

---

## Step 3 — Implement `EtoroBrokerClient` ⚠️ HIGH RISK — POSTPONE

> **This step is intentionally deferred as a future feature.**
>
> The eToro client carries the highest risk in the entire integration:
> there is no official Python SDK (all calls are raw REST via `httpx`),
> the rate limit is 20 req/min for trade execution, short selling uses CFD
> mechanics that differ fundamentally from Alpaca's direct short model, and
> symbol identifiers require a runtime mapping from standard tickers to eToro
> internal `InstrumentID` integers. None of these blockers can be resolved
> without live API access, which itself requires account verification and
> key approval from eToro.
>
> **Do not start this step until:**
> 1. Step 2 is merged and stable.
> 2. eToro API access has been approved and the sandbox manually validated.
> 3. The following open questions are answered (see below).

### Open questions to resolve before starting

| # | Question | Why it matters |
|---|---|---|
| 1 | Does eToro expose a bulk "close all positions" endpoint? | `HaltHandlerAgent` relies on a single call; without it, halt cleanup requires N calls at 20 req/min |
| 2 | What is the exact endpoint + payload for a market order? | Core order submission path |
| 3 | How are short positions represented — as CFDs with leverage or as standard sells? | `SignalDirection.SHORT` maps directly to `OrderSide.SELL` today; CFD semantics may break `PortfolioState` tracking |
| 4 | Are fractional quantities supported for stock orders? | `signal.suggested_qty` may be a fractional float |
| 5 | How is order status polled — REST pull or WebSocket push? | Affects `_sync_order_status` design |
| 6 | Are all watchlist tickers (AAPL, MSFT, GOOGL, AMZN, TSLA) available as non-CFD equities? | Determines whether position sizing logic needs CFD-awareness |

### Planned tasks (do not act on yet)

- [ ] Fetch and cache the `InstrumentID` map from
      `https://api.etoro.com/Metadata/V1/Instruments` at startup; expose a
      `ticker_to_instrument_id(ticker) → int` helper.
- [ ] Create `src/news_trade/services/broker/etoro_broker.py` implementing
      `BrokerClient` using `httpx.AsyncClient` with
      `Ocp-Apim-Subscription-Key` header auth.
- [ ] Implement rate-limit back-off (20 req/min ceiling).
- [ ] Handle CFD short semantics — either adapt the internal `Order` model or
      document that `SignalDirection.SHORT` is unsupported on eToro and block
      it at the factory level.
- [ ] Register `EtoroBrokerClient` in the broker factory for
      `BrokerProviderType.ETORO`.
- [ ] Write integration tests against the eToro demo environment.
- [ ] Add `ETORO_API_KEY` and `ETORO_DEMO` to `.env.example`.

### Known risks
- No official Python SDK — all response parsing is manual and brittle.
- 20 req/min rate limit is a hard ceiling during drawdown halts with multiple
  open positions.
- CFD leverage and margin mechanics are outside the current risk model.
- Symbol mapping adds a network call at startup and a failure mode if the
  Metadata API is unavailable.
- eToro API access is gated; development cannot begin without approval.
