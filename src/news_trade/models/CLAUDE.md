# models/ — Pydantic Data Models

All models use Pydantic v2. Value objects are frozen (`ConfigDict(frozen=True)`);
`TradeSignal` is mutable (updated via `model_copy(update={...})` through the pipeline).

---

## File Map

| File | Key Classes | Frozen? |
|---|---|---|
| `events.py` | `EventType` (StrEnum), `NewsEvent` | Yes |
| `market.py` | `OHLCVBar`, `MarketSnapshot` | Yes |
| `sentiment.py` | `SentimentLabel` (StrEnum), `SentimentResult` | Yes |
| `signals.py` | `SignalDirection` (StrEnum), `DebateRound`, `DebateVerdict` (StrEnum), `DebateResult`, `TradeSignal` | `DebateRound`, `DebateResult` frozen; `TradeSignal` **No** — mutable, updated via `model_copy()` |
| `surprise.py` | `SurpriseDirection`, `SignalStrength`, `MetricSurprise`, `EarningsSurprise`, `EstimatesData` | Yes |
| `positions.py` | `Stage1Status` (StrEnum), `OpenStage1Position` | Yes |
| `outcomes.py` | `HistoricalOutcomes` | Yes |
| `calendar.py` | `ReportTiming` (StrEnum), `EarningsCalendarEntry` | Yes |
| `orders.py` | `OrderSide`, `OrderStatus`, `OrderType`, `Order` | Yes |
| `portfolio.py` | `Position`, `PortfolioState` | Yes |

---

## EventType Tiers (`events.py`)

**Coarse (8, backward-compatible):** `EARNINGS`, `FDA_APPROVAL`, `MERGER_ACQUISITION`,
`MACRO`, `GUIDANCE`, `ANALYST_RATING`, `SEC_FILING`, `OTHER`

**Fine-grained (20, used by ConfidenceScorer and SignalGeneratorAgent):**

| Tier | Values |
|---|---|
| Earnings & Guidance | `EARN_PRE`, `EARN_BEAT`, `EARN_MISS`, `EARN_MIXED`, `GUID_UP`, `GUID_DOWN`, `GUID_WARN` |
| M&A | `MA_TARGET`, `MA_ACQUIRER`, `MA_RUMOUR`, `MA_BREAK`, `MA_COUNTER` |
| Regulatory | `REG_BLOCK`, `REG_CLEAR`, `REG_ACTION`, `REG_FINE`, `REG_LICENSE` |
| Sector Contagion | `SECTOR_BEAT_SPILL`, `SECTOR_MISS_SPILL`, `SUPPLY_CHAIN` |

Use fine-grained values for all new logic. Coarse values exist only for legacy compatibility.

---

## Model Relationships

```
NewsEvent (event_id) ──────────────────────► SentimentResult (event_id)
NewsEvent (tickers[]) ─────────────────────► TradeSignal (ticker)
MarketSnapshot (ticker) ────────────────────► TradeSignal (via SignalGeneratorAgent)
EarningsSurprise ──────────────────────────► ConfidenceScorer → TradeSignal.confidence_score
EstimatesData ─────────────────────────────► EstimatesRenderer → ConfidenceScorer
OpenStage1Position (id = stage1_id) ───────► EarningsOutcomeRow.stage1_id (FK)
OpenStage1Position ────────────────────────► TradeSignal.stage1_id (links POST to PRE)
HistoricalOutcomes ────────────────────────► EarningsCalendarAgent (beat rate for sizing)
EarningsCalendarEntry ─────────────────────► EarningsCalendarAgent._synthesise_event() → NewsEvent
```

---

## Computed Fields

Models with `@computed_field` properties (derived at access time, not stored):

| Model | Field | Formula |
|---|---|---|
| `MetricSurprise` | `pct_surprise` | `(actual - consensus) / \|consensus\| * 100` |
| `MetricSurprise` | `estimate_std` | `(high - low) / 4` |
| `MetricSurprise` | `sigma_surprise` | `(actual - consensus) / estimate_std` |
| `MetricSurprise` | `direction` | `BEAT` if pct > 2, `MISS` if pct < -2 |
| `MetricSurprise` | `confidence` | `sigma_score * 0.7 + coverage_score * 0.3` |
| `EarningsSurprise` | `composite_surprise` | `eps_pct*0.6 + rev_pct*0.4 + guidance*20` |
| `EarningsSurprise` | `composite_confidence` | `mean(eps.confidence, revenue.confidence)` |
| `EarningsSurprise` | `signal_strength` | `STRONG/MODERATE/WEAK/NONE` by threshold |
| `EstimatesData` | `estimate_dispersion` | `(high-low) / (4 * \|estimate\|)` |
| `OpenStage1Position` | `days_to_report` | `(expected_report_date - date.today()).days` |
| `EarningsCalendarEntry` | `days_until_report` | `(report_date - date.today()).days` |
| `EarningsCalendarEntry` | `is_actionable` | `2 <= days_until_report <= 5` |

`days_to_report` is time-relative — negative means the report date has passed.

---

## TradeSignal Confidence Fields (Pattern C)

Added to `TradeSignal` by `ConfidenceScorer.apply_gate()`:

```python
signal_strength: SignalStrength | None      # STRONG/MODERATE/WEAK/NONE
confidence_score: float | None              # 0.0-1.0 composite score
passed_confidence_gate: bool = False        # default False — must be explicitly set True
rejection_reason: str | None               # populated when gate fails
```

Every signal starts with `passed_confidence_gate=False`. `RiskManagerAgent` rejects
any signal where this is still `False`.

---

## TradeSignal Debate Field (Pattern A)

Added to `TradeSignal` by `SignalGeneratorAgent._debate_signal()`:

```python
debate_result: DebateResult | None = None  # None if debate was skipped
```

`DebateResult` (frozen) holds the `DebateVerdict` (`CONFIRM`/`REDUCE`/`REJECT`), a
`confidence_delta` (applied to `confidence_score`), and the list of `DebateRound` objects.
Each `DebateRound` (frozen) stores `round_number`, `bull_argument`, and `bear_argument`.

The debate only runs when `signal_debate_rounds > 0` (default 0 — disabled) **and**
`confidence_score >= signal_debate_threshold` (default 0.70).

---

## Two-Stage Position Models (Pattern D)

`OpenStage1Position` (`positions.py`) is the in-memory bridge between Stage 1 (EARN_PRE)
and Stage 2 (EARN_BEAT/MISS/MIXED). It is persisted to `stage1_positions` SQLite table via
`Stage1Repository`. `HistoricalOutcomes` (`outcomes.py`) is returned by
`Stage1Repository.load_historical_outcomes()` to inform Stage 1 position sizing.

`Stage1Status` lifecycle: `OPEN` → `CONFIRMED | REVERSED | EXITED | EXPIRED`

---

## Conventions

- `frozen=True` on all value objects — use `model_copy(update={...})` for modifications
- `Annotated[float, Field(ge=0.0, le=1.0)]` for constrained numeric fields
- `from __future__ import annotations` at the top of every file
- `StrEnum` for all enumerations (not `Enum`) — values are plain strings in JSON/SQLite
- `@computed_field @property` stack for derived fields
- Add `model_id: str` and `provider: str` to any model produced by an LLM call
