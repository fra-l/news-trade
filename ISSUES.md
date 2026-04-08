# Issues & Phase Tracking

---

## Issue 1: Agent self-learning — close the feedback loop beyond EARN_PRE

**Priority:** P2 — Nice-to-have
**Depends on:** Pattern D (Stage1Repository), Pattern A (DebateResult on TradeSignal), ExecutionAgent (OrderRow)
**Labels:** `feature`, `learning`, `architecture`

The system already has one self-learning loop (Pattern D): `Stage1Repository` records
EARN_PRE outcomes and, after ≥4 quarters per ticker, replaces static FMP beat rates
with the system's own observed beat rate.  All other signal types and scoring components
are fully static — weights and gates are hand-tuned constants that never update.

### Gaps to close

| Gap | Location | Impact |
|---|---|---|
| `ConfidenceScorer` weights (`_WEIGHTS`) are hardcoded constants | `services/confidence_scorer.py` | Surprise/sentiment/coverage/source weights never adapt based on realised P&L |
| `ConfidenceScorer` gate thresholds (`_GATES`) are hardcoded | `services/confidence_scorer.py` | Gate thresholds are hand-tuned; no data-driven calibration over time |
| `_SOURCE_SCORES` are static | `services/confidence_scorer.py` | A source proven consistently wrong (e.g. Reddit) is never demoted automatically |
| Non-earnings signals have no outcome recording | No equivalent of `record_outcome()` for M&A, guidance, regulatory, etc. | P&L data for these event types is never persisted for future use |
| Pattern A debate verdicts are audit-only | `TradeSignal.debate_result` | CONFIRM/REDUCE/REJECT verdicts are stored but never fed back to improve future debate prompts or thresholds |
| `ConfidenceScorer.score()` has no memory of which score ranges led to profitable trades | `services/confidence_scorer.py` | Gate thresholds remain static regardless of observed hit-rate per score bucket |

### Proposed approach (high level)

1. **Generic outcome table** — extend `EarningsOutcomeRow` (or add a new `SignalOutcomeRow`)
   to record final P&L, direction correctness, and confidence score for every filled order,
   keyed on `signal_id`.  `ExecutionAgent` writes the row when an order closes.

2. **Per-event-type hit-rate tracking** — after N outcomes per `EventType`, compute the
   empirical precision (correct direction / total) per confidence bucket (e.g. 0.5–0.6,
   0.6–0.7, …).  Feed this into a gate calibration step (e.g. shift the gate up/down by
   0.02 per period).

3. **Source credibility decay** — compute per-source precision over a rolling window; update
   `_SOURCE_SCORES` from DB at startup (DB values override the hardcoded defaults).

4. **Debate verdict calibration** — track the P&L of signals that received REDUCE or REJECT
   verdicts (compared to what would have happened without the debate) to assess whether the
   debate gate is adding value and tune `signal_debate_threshold`.

### Out of scope for this issue

- Full online gradient-based learning (would require a different architecture)
- Model fine-tuning or prompt optimisation via RL
- Changing the LangGraph pipeline structure

---

## Issue 2: Safety Mechanism — Intelligent Halt Guard

**Priority:** P1 — High
**Depends on:** `PortfolioFetcherAgent` (✅ done)
**Labels:** `feature`, `safety`, `risk`

### Summary

The system needs a smarter halt guard that can distinguish between two loss scenarios:

| Scenario | Correct response |
|---|---|
| Market-driven drawdown (correlated with broad sell-off; positions are coherent) | **Do not halt** — hold positions, let the thesis play out |
| System malfunction (loop, calibration drift, parameter instability) | **Halt immediately** — cancel orders, close all positions |

The current single-threshold hard stop (`max_drawdown_pct = 0.03`) cannot tell these
apart and will prematurely close healthy PEAD positions during ordinary market volatility.

---

### Open Question 1: Is 3% too tight?

**Yes, almost certainly.** The current 3% default was written for an intraday assumption
but the system holds positions for up to `PEAD_HORIZON_DAYS` (default 5) calendar days.
Normal single-day volatility for individual equities is easily ±2–3%.

Candidate values to discuss:

| Mode | Suggested `max_drawdown_pct` | Rationale |
|---|---|---|
| Intraday only (no PEAD) | 2–3% | Standard day-trading stop |
| Mixed (intraday + PEAD swing) | 6–8% | Covers normal 2–3× sigma moves |
| Position investing | 10–15% | Allows full earnings cycle to play out |

**The case for 10%:** A news-driven earnings trade that is directionally correct can
easily see a 5–7% counter-move intraday before the thesis resolves.  Closing at 3%
would systematically destroy P&L on trades that would have recovered.

**Decision needed:** What is the primary holding-period assumption for this system?
Once settled, `max_drawdown_pct` should be updated in `config.py` accordingly.

---

### Open Question 2: Proposed Two-Threshold Architecture

Instead of a single threshold, use two:

```
soft threshold (e.g. 2%)   → trigger LLM safety review → verdict decides
hard threshold (e.g. 8%)   → immediate halt, no LLM involved
```

**Simple circuit breakers (no LLM, always-on)** — catch obvious malfunctions cheaply:

| Check | Trigger condition | Action |
|---|---|---|
| Signal burst | `len(trade_signals) > N` in one cycle | Auto-halt |
| Signal homogeneity | All signals same ticker + direction + identical confidence score | Trigger LLM review |
| Loss velocity | Drawdown exceeds soft threshold in a single cycle | Trigger LLM review |

**LLM safety review (triggered by soft threshold or circuit breakers):**

A `SafetyReviewAgent` sends the LLM:
- Last N trade signals (ticker, direction, conviction, reasoning text, P&L outcome)
- Portfolio loss context (`daily_pnl`, `max_drawdown_pct`)
- Broad market context (e.g. SPY % change — a single `MarketSnapshot` suffices)

The LLM returns a structured verdict:

| Verdict | Meaning | Action |
|---|---|---|
| `MARKET_CONDITIONS` | Losses are market-correlated; signals are coherent and diverse | Override soft threshold; continue |
| `PARAMETER_INSTABILITY` | Confidence scores uniformly at floor; calibration drift detected | Halt; Telegram alert |
| `MALFUNCTION` | Repetitive signals, identical reasoning text, loop pattern detected | Halt; Telegram alert |

Only `PARAMETER_INSTABILITY` and `MALFUNCTION` trigger `system_halted = True`.
`MARKET_CONDITIONS` allows the pipeline to continue past the soft threshold
(the hard threshold still halts unconditionally).

---

### Open Question 3: Trim the implementation scope

Full implementation would require:

| File | Purpose |
|---|---|
| `models/safety.py` | `SafetyVerdict` enum + `SafetyAssessment` frozen model |
| `agents/safety_review_agent.py` | Heuristics + LLM analysis + structured verdict |
| `config.py` | New settings (soft threshold, burst limit, consecutive loss limit) |
| `graph/state.py` | Optional `safety_assessment` field |
| `graph/pipeline.py` | Wire between RiskManager and HaltHandler |
| `tests/test_safety_review_agent.py` | Tests |

A **minimal v1** could skip the `CircuitBreakerService` abstraction entirely and inline
all heuristics directly in `SafetyReviewAgent.run()`, reducing new files to 2 (agent + tests).
The signal burst and homogeneity checks are simple enough to be a dozen lines each.

**Decision needed:** Build full version now, or start with minimal v1?

---

### Open Question 4: LLM model tier for safety review

The safety review needs to reason over signal patterns and market context — a nuanced
task more suited to the deep model (Sonnet) than the quick model (Haiku).

- **Sonnet cost estimate:** ~2K input tokens per review ≈ $0.006 per triggered call.
  If it fires at most a handful of times per session, this is negligible.
- **Trigger frequency:** Depends on calibration of the soft threshold. At 2%, this
  may fire several times per session on volatile days — worth monitoring.
- **Recommendation:** Deep model, but add a cooldown (e.g. max one LLM review per 5
  cycles) to prevent runaway spend if the threshold is hit repeatedly.

---

### Open Question 5: Interaction with existing mechanisms

| Concern | Notes |
|---|---|
| `risk_dry_run=True` | LLM review should still run (for calibration logging) but HALT verdict should be suppressed, consistent with dry-run contract |
| PEAD positions | Most compelling use case: LLM can recognise `MARKET_CONDITIONS` and avoid premature close of healthy PEAD positions during intraday swings |
| Telegram `/stop` | Operator stop bypasses LLM review entirely — correct; `/stop` is explicit human intent |
| `HaltHandlerAgent` | Unchanged — still runs when `system_halted=True`; the LLM review only gates whether that flag gets set |

---

### Open Question 6: Drawdown metric is daily-only (related gap)

`PortfolioFetcherAgent` computes drawdown as `(equity − last_equity) / last_equity`
using Alpaca's `account.last_equity` (previous day's close). This is a **daily** measure.

Two failure modes:
- **Slow multi-day bleed** (−1%/day) will never trigger the daily 3% halt.
- **Sharp intraday spike** will trigger even if the week is flat.

Alternatives:
- `TradingClient.get_portfolio_history(period="1M", timeframe="1D")` — 30-day equity
  curve; compute true peak-to-trough. One extra API call per cycle.
- **DB high-watermark** (preferred): store peak equity in a new `AccountStateRow`
  (Alembic migration required); `PortfolioFetcherAgent` updates it each cycle when
  equity is above the stored peak. Gives accurate multi-day drawdown without extra API calls.

This interacts with ISSUE-001 threshold values — worth resolving together.

---

## Issue 3: Prometheus / Grafana Operational Dashboard

**Priority:** P3 — Future
**Depends on:** System running stably in paper/live trading
**Labels:** `monitoring`, `infrastructure`

When the system is operative and producing consistent trading activity, a
Prometheus + Grafana dashboard will provide real-time operational metrics:
cycle latency histograms, signal throughput, error rates, Alpaca order fill
latency, and Redis event-bus queue depth.

**Planned approach:**
- Instrument each LangGraph node with `prometheus_client` counters/histograms
  (node duration, error count, signal count per EventType)
- Expose `/metrics` endpoint via a lightweight HTTP server (`prometheus_client.start_http_server`)
- Grafana dashboard with pre-built panels for the 8-node pipeline
- Alert rules for: drawdown halt triggered, >3 consecutive failed cycles,
  Claude daily budget >80% consumed

This is intentionally deferred — LangSmith already covers trace-level debugging
for the development phase. Prometheus/Grafana adds value once the system is
running 24/7 and operational anomaly detection becomes the priority.
