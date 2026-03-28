# Event-Driven Signal Layer

**Project:** news-trade  
**Status:** Designed, pending implementation  
**Scope:** v1 — US equities (NYSE/NASDAQ), swing trading (1–5 days)  
**Out of scope for v1:** Crypto (deferred), FDA events (excluded)

---

## Table of Contents

1. [Event Type Taxonomy](#1-event-type-taxonomy)
2. [Surprise Delta Calculation](#2-surprise-delta-calculation)
3. [Two-Stage Position Model](#3-two-stage-position-model)
4. [TradeSignal Model](#4-tradesignal-model)
5. [Confidence Thresholds](#5-confidence-thresholds)
6. [Earnings Calendar Integration](#6-earnings-calendar-integration)
7. [RiskManagerAgent](#7-riskmanageragent)
8. [SQLite ORM — OpenStage1Position](#8-sqlite-orm--openstage1position)
9. [End-to-End Flow](#9-end-to-end-flow)
10. [All Issues](#10-all-issues)

---

## 1. Event Type Taxonomy

### Design Principles

- Every event type has a unique string code used as a Python `StrEnum` value
- Codes are the contract between `NewsIngestorAgent` (classification) and `SignalGeneratorAgent` (logic dispatch)
- `SignalGeneratorAgent` uses a `match` statement on `EventType` — one handler function per event type

### Tier 1 — Earnings & Guidance

| Code | Name | Trigger | Direction Logic | Horizon | Size Tier |
|---|---|---|---|---|---|
| `EARN_PRE` | Pre-earnings positioning | 2–5 days before known report date | Long/short based on historical beat rate | Exit at announcement | Small (25–40%) |
| `EARN_BEAT` | Earnings beat | EPS/rev above consensus + positive guidance | Long; add to any `EARN_PRE` position | 2–4 days PEAD | Full |
| `EARN_MISS` | Earnings miss | EPS/rev below consensus or guidance cut | Short; reverse any `EARN_PRE` long | 2–4 days PEAD | Full |
| `EARN_MIXED` | Mixed earnings | Beat on one metric, miss on other, or flat guidance | Exit `EARN_PRE` position; no new signal | Immediate exit | Zero |
| `GUID_UP` | Guidance raise | Forward guidance raised above consensus | Long | 3–5 days | Full |
| `GUID_DOWN` | Guidance cut | Forward guidance cut | Short | 3–5 days | Full |
| `GUID_WARN` | Pre-announcement warning | Off-cycle negative pre-announcement | Short, high urgency | Immediate + 2–3 days | Full |

### Tier 2 — M&A

| Code | Name | Trigger | Direction Logic | Horizon | Size Tier |
|---|---|---|---|---|---|
| `MA_TARGET` | Acquisition target | Company confirmed as acquisition target | Long to near offer price | Until deal close/break | Medium |
| `MA_ACQUIRER` | Acquirer announced | Company announces acquisition | Short (market punishes acquirers) | 2–5 days | Medium |
| `MA_RUMOUR` | M&A rumour | Unconfirmed reports of deal talks | Small long on target | Exit on confirmation or denial | Small |
| `MA_BREAK` | Deal break | Confirmed deal falls apart | Short the target | 1–3 days | Medium |
| `MA_COUNTER` | Counter-bid / bidding war | Second bidder emerges | Add to target long | Until auction resolves | Medium |

### Tier 3 — Regulatory & Legal (Non-FDA)

| Code | Name | Trigger | Direction Logic | Horizon | Size Tier |
|---|---|---|---|---|---|
| `REG_BLOCK` | Merger blocked | Antitrust/FTC blocks a deal | Short the target | 1–2 days | Medium |
| `REG_CLEAR` | Merger cleared | Regulatory approval granted | Long target | Until close | Small |
| `REG_ACTION` | SEC/DOJ enforcement | Company under formal investigation | Short | 2–5 days | Medium |
| `REG_FINE` | Regulatory fine | Fine announced | Short if material, neutral if minor | 1–2 days | Small–Medium |
| `REG_LICENSE` | License decision | Operating license granted or revoked | Long (grant) / Short (revoke) | 2–4 days | Medium |

### Tier 4 — Sector Contagion

| Code | Name | Trigger | Direction Logic | Horizon | Size Tier |
|---|---|---|---|---|---|
| `SECTOR_BEAT_SPILL` | Sector peer beat spillover | Major peer beats; others not yet reported | Long sector peers by correlation rank | Until peer reports | Small |
| `SECTOR_MISS_SPILL` | Sector peer miss spillover | Major peer misses | Short sector peers | Until peer reports | Small |
| `SUPPLY_CHAIN` | Supply chain signal | Supplier/customer earnings imply demand shift | Long/short downstream by direction | 2–4 days | Small |

---

## 2. Surprise Delta Calculation

### Data Provider

**Financial Modeling Prep (FMP)** — free tier, 250 req/day.

Chosen because it is the only free provider offering EPS estimates, revenue estimates, analyst high/low range, and historical surprises — all required for the sigma calculation. yfinance is used as a calendar fallback only (see Section 6).

Key endpoints:
- `GET /analyst-estimates/{ticker}` — consensus, high, low, analyst count
- `GET /earnings-surprises/{ticker}` — historical beat/miss data for beat rate calculation

### Models — `models/surprise.py`

```
SurpriseDirection     StrEnum: BEAT | MISS | IN_LINE

MetricSurprise        BaseModel
  actual              float
  consensus           float          mean analyst estimate
  estimate_high       float
  estimate_low        float
  analyst_count       int
  pct_surprise        computed       ((actual - consensus) / |consensus|) * 100
  estimate_std        computed       (high - low) / 4
  sigma_surprise      computed       (actual - consensus) / estimate_std
  direction           computed       BEAT if pct > 2.0, MISS if pct < -2.0
  confidence          computed       (sigma_score * 0.7) + (coverage_score * 0.3)

EarningsSurprise      BaseModel
  ticker              str
  report_date         date
  fiscal_quarter      str
  eps                 MetricSurprise
  revenue             MetricSurprise
  guidance_sentiment  float | None   -1.0 to +1.0, from SentimentAnalystAgent
  guidance_direction  SurpriseDirection | None
  composite_surprise  computed       (eps.pct * 0.6) + (rev.pct * 0.4) + (guidance * 20)
  composite_confidence computed      mean(eps.confidence, revenue.confidence)
  signal_strength     computed       STRONG | MODERATE | WEAK | NONE
```

### Signal Strength Thresholds

| Tier | composite_surprise | composite_confidence |
|---|---|---|
| STRONG | > 10 | > 0.7 |
| MODERATE | > 5 | > 0.5 |
| WEAK | > 2 | any |
| NONE | ≤ 2 | any |

### Provider Interface — `providers/protocols.py`

```
EstimatesProvider (Protocol)
  get_consensus(ticker, fiscal_quarter) → (consensus, high, low, actual, analyst_count)
  get_revenue_consensus(ticker, fiscal_quarter) → (...)
  get_historical_surprise(ticker) → list[dict]
```

Implementation: `providers/estimates/fmp.py` — `FMPEstimatesProvider`

### Settings Additions

```
earn_beat_pct_threshold       float   default 2.0
earn_miss_pct_threshold       float   default -2.0
earn_strong_sigma_threshold   float   default 2.0
earn_min_analyst_count        int     default 3
earn_guidance_weight          float   default 0.20
```

---

## 3. Two-Stage Position Model

### Rationale

Pre-earnings positioning (stage 1) and post-announcement PEAD (stage 2) are treated as a sequential two-stage strategy. Stage 1 is an optionality bet at reduced size; stage 2 deploys remaining size only after the announcement confirms direction.

Because the two stages span separate pipeline runs, an `OpenStage1Position` record is persisted to SQLite as a bridge.

### Model — `models/positions.py`

```
Stage1Status    StrEnum: OPEN | CONFIRMED | REVERSED | EXITED | EXPIRED

OpenStage1Position  BaseModel
  id                    str (uuid)
  ticker                str
  direction             str               "long" | "short"
  size_pct              float
  entry_price           float
  opened_at             datetime
  expected_report_date  date
  fiscal_quarter        str
  historical_beat_rate  float
  status                Stage1Status      default OPEN
  days_to_report        computed
```

### Stage 1 — EARN_PRE Signal Logic

Fires 2–5 days before report (from `EarningsCalendarAgent`).

```
beat_rate = historical beat rate from FMP
skip if beat_rate < 0.55 or > 0.85

direction  = long  if beat_rate >= 0.60
             short if beat_rate <  0.60

size_pct   = 0.25 + ((beat_rate - 0.60) / 0.25) * 0.15
             clamped to [0.25, 0.40]

stop_loss  = 4%
take_profit = None   (event-driven exit)

→ persist OpenStage1Position to SQLite via Stage1Repository
→ emit TradeSignal(stage=PRE, direction=direction, size_pct=size_pct)
```

### Stage 2 — Post-Announcement Decision Tree

```
load OpenStage1Position for ticker from Stage1Repository

BEAT:
  stage1 exists + agrees       → add remaining size (1.0 - stage1.size_pct)
                                  status = CONFIRMED
  stage1 exists + disagrees    → close stage1, full reverse short
                                  status = REVERSED
  no stage1                    → fresh PEAD entry at 75% size

MISS:
  mirror of BEAT with short direction

IN_LINE / MIXED:
  stage1 exists                → EXIT signal, close flat
                                  status = EXITED
  no stage1                    → no signal

Holding horizon by signal_strength:
  STRONG   → 4 days
  MODERATE → 2 days
  WEAK     → 1 day
```

### PipelineState Additions

```
earnings_surprise     EarningsSurprise | None
open_stage1           OpenStage1Position | None
stage2_action         Literal["confirm", "reverse", "exit", None]
```

---

## 4. TradeSignal Model

Full replacement for `models/signals.py`.

```
SignalDirection   StrEnum: LONG | SHORT | EXIT
SignalStage       StrEnum: PRE | POST

TradeSignal       BaseModel
  id                      str           uuid, auto-generated
  created_at              datetime
  ticker                  str
  event_type              EventType
  stage                   SignalStage
  stage1_id               str | None    links POST signal to OpenStage1Position
  direction               SignalDirection
  size_pct                float         (0.0, 1.0]
  stop_loss_pct           float
  take_profit_pct         float | None  None for event-driven exits
  horizon_days            int           0 = immediate exit
  composite_surprise      float | None  POST stage only
  composite_confidence    float | None  POST stage only
  signal_strength         STRONG|MODERATE|WEAK|NONE|None
  confidence_score        float | None  0.0–1.0
  passed_confidence_gate  bool          default False
  rejection_reason        str | None
```

### Model Validators

**exit_signal_invariants** — EXIT must have `horizon_days=0` and `take_profit_pct=None`. Hard error.

**stage1_has_no_surprise** — PRE stage cannot carry `composite_surprise`. Hard error.

**stage2_links_to_stage1** — POST without `stage1_id` is valid (fresh PEAD) but logs soft warning in `rejection_reason`.

### Key Design Decisions

- `passed_confidence_gate` defaults to `False` — every signal starts rejected
- `EXIT` is a first-class `SignalDirection` value, not a flag
- `SignalStage` uses readable strings for legibility in logs and SQLite

---

## 5. Confidence Thresholds

### Architecture

Each signal's `confidence_score` is a weighted composite of four components. Weights vary per event type. A signal proceeds to `RiskManagerAgent` only if `confidence_score >= CONFIDENCE_GATES[event_type]`.

`RiskManagerAgent` checks `passed_confidence_gate` first — before all other checks.

### Four Components — `ConfidenceScorer`

```
surprise_score(surprise)
  min(max(eps_sigma, rev_sigma) / 3.0, 1.0)
  returns 0.0 if no surprise data

sentiment_score(sentiment)
  sentiment.confidence * abs(sentiment.score)
  returns 0.0 if no sentiment data

coverage_score(analyst_count)
  min(analyst_count / 10.0, 1.0), minimum 0.1 if count < 3

source_score(source)
  tier lookup:
    sec.gov, businesswire, prnewswire   0.90–1.00
    reuters, bloomberg, wsj, benzinga   0.75–0.85
    yahoo_finance, cnbc, marketwatch    0.55–0.65
    twitter, reddit                     0.15–0.20
    unknown                             0.30
```

### Weight Matrix

| Event Type | surprise | sentiment | coverage | source |
|---|---|---|---|---|
| `EARN_PRE` | 0.00 | 0.30 | 0.40 | 0.30 |
| `EARN_BEAT` | 0.50 | 0.30 | 0.15 | 0.05 |
| `EARN_MISS` | 0.50 | 0.30 | 0.15 | 0.05 |
| `EARN_MIXED` | 0.00 | 0.00 | 0.00 | 0.00 |
| `GUID_UP` | 0.30 | 0.50 | 0.10 | 0.10 |
| `GUID_DOWN` | 0.30 | 0.50 | 0.10 | 0.10 |
| `GUID_WARN` | 0.20 | 0.50 | 0.10 | 0.20 |
| `MA_TARGET` | 0.00 | 0.30 | 0.00 | 0.70 |
| `MA_ACQUIRER` | 0.00 | 0.30 | 0.00 | 0.70 |
| `MA_RUMOUR` | 0.00 | 0.20 | 0.00 | 0.80 |
| `MA_BREAK` | 0.00 | 0.40 | 0.00 | 0.60 |
| `MA_COUNTER` | 0.00 | 0.30 | 0.00 | 0.70 |
| `REG_ACTION` | 0.00 | 0.40 | 0.00 | 0.60 |
| `REG_FINE` | 0.00 | 0.40 | 0.00 | 0.60 |
| `REG_BLOCK` | 0.00 | 0.30 | 0.00 | 0.70 |
| `REG_CLEAR` | 0.00 | 0.30 | 0.00 | 0.70 |
| `REG_LICENSE` | 0.00 | 0.30 | 0.00 | 0.70 |
| `SECTOR_BEAT_SPILL` | 0.40 | 0.30 | 0.20 | 0.10 |
| `SECTOR_MISS_SPILL` | 0.40 | 0.30 | 0.20 | 0.10 |
| `SUPPLY_CHAIN` | 0.30 | 0.40 | 0.10 | 0.20 |

### Confidence Gates

| Event Type | Gate | Rationale |
|---|---|---|
| `EARN_PRE` | 0.45 | Optionality bet, small size |
| `EARN_BEAT` | 0.55 | Published number |
| `EARN_MISS` | 0.55 | Same |
| `EARN_MIXED` | 1.01 | Never passes |
| `GUID_UP` | 0.50 | — |
| `GUID_DOWN` | 0.50 | — |
| `GUID_WARN` | 0.60 | Off-cycle, must be credible |
| `MA_TARGET` | 0.65 | — |
| `MA_ACQUIRER` | 0.65 | — |
| `MA_RUMOUR` | 0.75 | Highest — unverified |
| `MA_BREAK` | 0.65 | — |
| `MA_COUNTER` | 0.65 | — |
| `REG_ACTION` | 0.60 | — |
| `REG_FINE` | 0.55 | — |
| `REG_BLOCK` | 0.65 | — |
| `REG_CLEAR` | 0.65 | — |
| `REG_LICENSE` | 0.65 | — |
| `SECTOR_BEAT_SPILL` | 0.50 | — |
| `SECTOR_MISS_SPILL` | 0.50 | — |
| `SUPPLY_CHAIN` | 0.55 | — |

Gates stored in `config.py` as `dict[str, float]`, overridable per environment via `.env`.

---

## 6. Earnings Calendar Integration

### Architecture Decision

`EarningsCalendarAgent` is a dedicated agent running on a **daily cron at 07:00 ET Mon–Fri**, outside the main LangGraph pipeline. It publishes synthetic `NewsEvent(event_type=EARN_PRE)` objects to the Redis event bus. The pipeline treats these identically to news-sourced events.

### Data Providers

Primary: `FMPCalendarProvider` — `GET /earning_calendar?from=&to=`  
Fallback: `YFinanceCalendarProvider` — unofficial, no API key, used when FMP fails

### Model — `models/calendar.py`

```
ReportTiming          StrEnum: PRE_MARKET | POST_MARKET | UNKNOWN

EarningsCalendarEntry BaseModel
  ticker              str
  report_date         date
  fiscal_quarter      str
  fiscal_year         int
  timing              ReportTiming    default UNKNOWN
  eps_estimate        float | None
  fetched_at          datetime
  days_until_report   computed
  is_actionable       computed        True if 2 <= days_until_report <= 5
```

Actionable window: 2–5 days. Beyond 5 = signal decays; under 2 = IV already elevated.

### Provider Protocol

```
CalendarProvider (Protocol)
  get_upcoming_earnings(tickers, from_date, to_date) → list[EarningsCalendarEntry]
```

Implementations: `providers/calendar/fmp.py`, `providers/calendar/yfinance_provider.py`

### Agent Behaviour

```
EarningsCalendarAgent.run()
  scan_window = today → today + 5 days
  entries = FMP.get_upcoming_earnings(watchlist, window)
             fallback to yfinance if FMP empty/fails
  filter is_actionable only
  for each entry:
    event_id = f"calendar_earn_pre_{ticker}_{report_date}"
    skip if event_id in SQLite (dedup guard)
    synthesise NewsEvent(EARN_PRE, source="earnings_calendar")
    publish → Redis
    persist → SQLite
```

### Synthesised NewsEvent

```
event_id        f"calendar_earn_pre_{ticker}_{report_date}"
ticker          from entry
headline        "{ticker} scheduled to report {quarter} on {date} ({timing})"
source          "earnings_calendar"
event_type      EARN_PRE
metadata        { days_until_report, eps_estimate, timing }
```

### Cron Scheduling

```
APScheduler: cron, hour=7, minute=0, day_of_week="mon-fri"
misfire_grace_time = 300 seconds
Dependency: apscheduler>=3.10
```

---

## 7. RiskManagerAgent

### Responsibility

Validates `TradeSignal` before `ExecutionAgent`. May approve, reduce size, or reject. Never generates signals.

### Output Model — `RiskValidation` (`models/risk.py`)

```
RiskValidation    BaseModel
  approved          bool
  rejection_reason  str | None
  original_size     float
  approved_size     float | None    may be reduced from original
  checks_run        list[str]       audit trail
  checked_at        datetime
```

### Check Layers (executed in order, fail-fast)

**Layer 1 — Confidence gate**
```
if not signal.passed_confidence_gate
  → REJECT with signal.rejection_reason
```

**Layer 2a — Drawdown halt**
```
drawdown = (peak_value - portfolio_value) / peak_value

if drawdown >= settings.max_drawdown_pct (default 0.10)
   and signal.direction != EXIT
  → REJECT
  set system_halted = True in PipelineState
  publish SYSTEM_HALTED event to Redis

EXIT signals always bypass this check.
```

**Layer 2b — Concentration limit**
```
is_add_to_existing = signal.stage1_id is not None

if open_position_count >= settings.max_open_positions (default 5)
   and signal.direction != EXIT
   and not is_add_to_existing
  → REJECT

Stage 2 ADD signals are exempt — they extend, not open, a position.
```

**Layer 3a — Pending order conflict**
```
if signal.ticker in pending_order_tickers
  → REJECT
```

**Layer 3b — Position size cap (modify, not reject)**
```
position_value = signal.suggested_qty * signal.entry_price   (skipped if entry_price is None)
max_value      = portfolio.equity * settings.max_position_pct  (default 0.15)

if position_value > max_value:
  capped_qty = max(1, floor(max_value / signal.entry_price))
  signal = signal.model_copy(update={"suggested_qty": capped_qty})
  log warning and continue
```
Applied via `_apply_size_cap()` in `run()` after `_evaluate()` approves the signal.
Skipped for market orders (`entry_price is None`) or when `equity <= 0`.

**Layer 3c — Direction conflict**
```
if ticker has existing open position in opposite direction
   and signal.direction != EXIT
  → REJECT
```

### LangGraph Routing After Risk

```
route_after_risk(state)
  if state.system_halted          → "halt_handler"
  if not risk_validation.approved → "end"
  else                            → "execution"
```

### PipelineState Additions

```
system_halted           bool                default False
risk_validation         RiskValidation | None
peak_portfolio_value    float | None        high-water mark, persisted in SQLite
```

### Settings Additions

```
max_position_pct      float   default 0.15
max_drawdown_pct      float   default 0.10
max_open_positions    int     default 5
risk_dry_run          bool    default False   log rejections without blocking
```

`risk_dry_run` recommended during early paper trading to calibrate thresholds before enforcement.

---

## 8. SQLite ORM — OpenStage1Position

### Design Principle

Two separate objects for two separate concerns:

```
OpenStage1Position        Pydantic model    in-memory, validated, used by agents
OpenStage1PositionRow     SQLAlchemy model  persistence only, maps to SQLite table
```

All DB access goes through `Stage1Repository` — no agent touches the ORM directly.

### Table — `stage1_positions`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | VARCHAR | PK | uuid string |
| `ticker` | VARCHAR | NOT NULL, INDEX | fast lookup by ticker |
| `direction` | VARCHAR | NOT NULL | "long" \| "short" |
| `size_pct` | FLOAT | NOT NULL | |
| `entry_price` | FLOAT | NOT NULL | |
| `opened_at` | DATETIME | NOT NULL | |
| `expected_report_date` | DATE | NOT NULL, INDEX | expiry scanner queries |
| `fiscal_quarter` | VARCHAR | NOT NULL | |
| `historical_beat_rate` | FLOAT | NOT NULL | |
| `status` | VARCHAR | NOT NULL, INDEX, DEFAULT "open" | Stage1Status value |
| `updated_at` | DATETIME | NOT NULL, onupdate=now() | |

### Repository — `services/stage1_repository.py`

```
Stage1Repository

  persist(position: OpenStage1Position) → None
    upsert into stage1_positions
    handles re-fired EARN_PRE for same ticker/quarter

  load_open(ticker: str) → OpenStage1Position | None
    SELECT WHERE ticker=:ticker AND status='open'
    ORDER BY opened_at DESC LIMIT 1

  update_status(id: str, status: Stage1Status) → None
    UPDATE SET status=:status, updated_at=now() WHERE id=:id

  load_expired() → list[OpenStage1Position]
    SELECT WHERE status='open' AND expected_report_date < today

  load_all_open() → list[OpenStage1Position]
    SELECT WHERE status='open'
    used by RiskManagerAgent concentration check
```

### Conversion Helpers

```
_to_row(position)   Pydantic → ORM row    status = position.status.value
_from_row(row)      ORM row → Pydantic    status = Stage1Status(row.status)
```

### Expiry Scanner

```
ExpiryScanner.run()   cron: 07:15 ET Mon-Fri

  expired = stage1_repo.load_expired()
  for each:
    stage1_repo.update_status(id, EXPIRED)
    log_warning(f"EARN_PRE expired: {ticker} {fiscal_quarter}")
    publish STAGE1_EXPIRED event to Redis
```

### Schema Initialisation

```
services/database.py

def init_db()
  Base.metadata.create_all(engine)   # picks up OpenStage1PositionRow automatically
```

### Agent Wiring

```
SignalGeneratorAgent (_handle_earn_pre)   stage1_repo.persist(position)
SignalGeneratorAgent (_handle_earn_post)  stage1_repo.load_open(ticker)
                                          stage1_repo.update_status(id, status)
RiskManagerAgent (concentration)          stage1_repo.load_all_open()
ExpiryScanner                             stage1_repo.load_expired()
                                          stage1_repo.update_status(id, EXPIRED)
```

---

## 9. End-to-End Flow

### Run A — Stage 1 (T-4 days, 07:00 ET cron)

```
EarningsCalendarAgent
  FMP: AAPL reports in 4 days, beat_rate = 0.72
  synthesises NewsEvent(EARN_PRE) → Redis

Pipeline
  NewsIngestorAgent       passes through (source="earnings_calendar")
  MarketDataAgent         fetches OHLCV snapshot
  FMPEstimatesProvider    returns historical beat rate = 0.72
  SignalGeneratorAgent
    direction=long, size_pct=0.33
    stage1_repo.persist(OpenStage1Position)
    emits TradeSignal(stage=PRE, size=0.33, stop=4%)
  ConfidenceGate          score vs 0.45 threshold
  RiskManagerAgent        layers 1–3 all pass
  ExecutionAgent          places 33% position on Alpaca
```

### Run B — Stage 2 (T+0, post-market)

```
NewsIngestorAgent       classifies EARN_BEAT (EPS +9.2%, rev +4.1%)
MarketDataAgent         fetches updated snapshot
FMPEstimatesProvider    builds EarningsSurprise
                        composite_surprise=+11.4, signal_strength=STRONG
SentimentAnalystAgent   guidance_sentiment=+0.6 → BEAT
SignalGeneratorAgent
  stage1_repo.load_open("AAPL") → direction=long, confirmed
  adds remaining 67%, horizon=4 days
  stage1_repo.update_status(id, CONFIRMED)
  emits TradeSignal(stage=POST, size=0.67, stop=6%, tp=12%)
ConfidenceGate          score vs 0.55 threshold
RiskManagerAgent        full 3-layer check
ExecutionAgent          adds to existing Alpaca position
```

### Run C — PEAD Exit (T+4)

```
ExecutionAgent horizon expiry
  closes full combined position
  stage1_repo.update_status(id, EXITED)
```

### Cron Schedule

| Time (ET) | Job | Purpose |
|---|---|---|
| 07:00 Mon–Fri | `EarningsCalendarAgent` | Scan upcoming reports, emit EARN_PRE |
| 07:15 Mon–Fri | `ExpiryScanner` | Mark stale OPEN positions as EXPIRED |

---

## 10. All Issues

| # | Title | File | Depends on |
|---|---|---|---|
| ~~10~~ | ~~Add `EarningsCalendarEntry` model~~ ✅ | `models/calendar.py` | — |
| ~~11~~ | ~~Implement `FMPCalendarProvider` + `YFinanceCalendarProvider`~~ ✅ | `providers/calendar/` | #10 |
| ~~12~~ | ~~Implement `EarningsCalendarAgent` with dedup guard~~ ✅ | `agents/earnings_calendar.py` | #10, #11 |
| 13 | Wire cron scheduler into `main.py` | `main.py` | #12, #22 |
| 14 | Add `EarningsSurprise` + `MetricSurprise` models | `models/surprise.py` | — |
| 15 | Add `OpenStage1Position` + `Stage1Status` | `models/positions.py` | — |
| 16 | Update `TradeSignal` model with stage fields | `models/signals.py` | #15 |
| 17 | Implement `FMPEstimatesProvider` | `providers/estimates/fmp.py` | #14 |
| 18 | Implement `ConfidenceScorer` + `apply_confidence_gate` | `agents/signal_generator.py` | #14, #16 |
| 19 | Add `EstimatesProvider` + `CalendarProvider` to protocols | `providers/protocols.py` | #14, #10 |
| 20 | Add `OpenStage1PositionRow` ORM class | `services/database.py` | #15 |
| 21 | Implement `Stage1Repository` | `services/stage1_repository.py` | #20 |
| 22 | Add `ExpiryScanner` cron job | `agents/expiry_scanner.py` | #21 |
| 23 | Wire `Stage1Repository` into `SignalGeneratorAgent` | `agents/signal_generator.py` | #21 |
| 24 | Wire `Stage1Repository` into `RiskManagerAgent` | `agents/risk_manager.py` | #21 |
| 25 | Implement `RiskManagerAgent` full logic | `agents/risk_manager.py` | #16, #21, #26 |
| 26 | Add `RiskValidation` model | `models/risk.py` | — |
| 27 | Add `halt_handler` node to LangGraph pipeline | `graph/pipeline.py` | #25 |

### Implementation Order

```
Leaf nodes (no dependencies) — start here, can be parallel:
  #14  EarningsSurprise + MetricSurprise
  #15  OpenStage1Position + Stage1Status
  #26  RiskValidation model
  #10  EarningsCalendarEntry

Second tier:
  #16  TradeSignal updates             ← #15
  #19  Protocol additions              ← #14, #10
  #20  OpenStage1PositionRow ORM       ← #15

Third tier:
  #11  Calendar providers ✅            ← #10 ✅
  #17  FMPEstimatesProvider            ← #14
  #21  Stage1Repository                ← #20

Fourth tier:
  #18  ConfidenceScorer + gate         ← #14, #16
  #12  EarningsCalendarAgent ✅         ← #11 ✅
  #22  ExpiryScanner                   ← #21
  #23  SignalGenerator wiring          ← #21
  #24  RiskManager wiring              ← #21
  #25  RiskManagerAgent full logic     ← #16, #21, #26

Final tier:
  #13  Cron scheduler in main.py       ← #12, #22
  #27  halt_handler pipeline node      ← #25
```
