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
7. [End-to-End Flow](#7-end-to-end-flow)
8. [New Issues](#8-new-issues)

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
SurpriseDirection   StrEnum: BEAT | MISS | IN_LINE

MetricSurprise      BaseModel
  actual            float
  consensus         float          mean analyst estimate
  estimate_high     float
  estimate_low      float
  analyst_count     int

  pct_surprise      computed  ((actual - consensus) / |consensus|) * 100
  estimate_std      computed  (high - low) / 4   [range/4 heuristic]
  sigma_surprise    computed  (actual - consensus) / estimate_std
  direction         computed  BEAT if pct > 2.0, MISS if pct < -2.0
  confidence        computed  (sigma_score * 0.7) + (coverage_score * 0.3)

EarningsSurprise    BaseModel
  ticker            str
  report_date       date
  fiscal_quarter    str            e.g. "Q3 2025"
  eps               MetricSurprise
  revenue           MetricSurprise
  guidance_sentiment  float | None    -1.0 to +1.0, from SentimentAnalystAgent
  guidance_direction  SurpriseDirection | None

  composite_surprise  computed  (eps.pct * 0.6) + (rev.pct * 0.4) + (guidance * 20)
  composite_confidence  computed  mean(eps.confidence, revenue.confidence)
  signal_strength   computed  STRONG | MODERATE | WEAK | NONE
```

### Signal Strength Thresholds

| Tier | composite_surprise | composite_confidence |
|---|---|---|
| STRONG | > 10 | > 0.7 |
| MODERATE | > 5 | > 0.5 |
| WEAK | > 2 | any |
| NONE | ≤ 2 | any |

### Provider Interface — `providers/protocols.py`

```python
class EstimatesProvider(Protocol):
    async def get_consensus(ticker, fiscal_quarter) -> tuple[float, float, float, float, int]
    async def get_revenue_consensus(ticker, fiscal_quarter) -> tuple[...]
    async def get_historical_surprise(ticker) -> list[dict]
```

Implementation: `providers/estimates/fmp.py` — `FMPEstimatesProvider`

### Settings Additions — `config.py`

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

Pre-earnings positioning (stage 1) and post-announcement PEAD (stage 2) are treated as a sequential two-stage strategy, not two independent signals. Stage 1 is an optionality bet at reduced size; stage 2 deploys remaining size only after the announcement confirms direction.

Because the two stages span separate pipeline runs, an `OpenStage1Position` record is persisted to SQLite as a bridge.

### Model — `models/positions.py`

```
Stage1Status    StrEnum: OPEN | CONFIRMED | REVERSED | EXITED | EXPIRED

OpenStage1Position  BaseModel
  id                    str (uuid)
  ticker                str
  direction             str         "long" | "short"
  size_pct              float       fraction of max position used in stage 1
  entry_price           float
  opened_at             datetime
  expected_report_date  date
  fiscal_quarter        str
  historical_beat_rate  float       from FMP historical surprises
  status                Stage1Status  default OPEN
  days_to_report        computed
```

### Stage 1 — `EARN_PRE` Signal Logic

Fires 2–5 days before report (from `EarningsCalendarAgent`, see Section 6).

```
beat_rate = historical beat rate from FMP
Skip if beat_rate < 0.55 or > 0.85   (no edge / suspiciously perfect)

direction  = long  if beat_rate >= 0.60
             short if beat_rate <  0.60

size_pct   = 0.25 + ((beat_rate - 0.60) / 0.25) * 0.15
             clamped to [0.25, 0.40]

stop_loss  = 4%   (tight — optionality bet)
take_profit = None (exit is event-driven, not price-driven)

→ Persist OpenStage1Position to SQLite
→ Emit TradeSignal(stage=PRE, direction=direction, size_pct=size_pct)
```

### Stage 2 — Post-Announcement Decision Tree

```
Load OpenStage1Position for ticker from SQLite

BEAT announcement:
  Stage 1 exists + agrees (long)  → add remaining size (1.0 - stage1.size_pct)
                                    status = CONFIRMED
  Stage 1 exists + disagrees      → close stage 1, open full reverse short
                                    status = REVERSED
  No stage 1                      → fresh PEAD entry at 75% size

MISS announcement:
  Mirror of BEAT with short direction

IN_LINE / MIXED:
  Stage 1 exists                  → EXIT signal, close position flat
                                    status = EXITED
  No stage 1                      → no signal

Holding horizon by signal_strength:
  STRONG   → 4 days
  MODERATE → 2 days
  WEAK     → 1 day
```

### PipelineState Additions — `graph/state.py`

```python
earnings_surprise: EarningsSurprise | None
open_stage1:       OpenStage1Position | None
stage2_action:     Literal["confirm", "reverse", "exit", None]
```

---

## 4. TradeSignal Model

Full replacement for `models/signals.py`.

```python
class SignalDirection(StrEnum):
    LONG  = "long"
    SHORT = "short"
    EXIT  = "exit"       # close existing position, no new directional bet

class SignalStage(StrEnum):
    PRE  = "pre"         # stage 1 — pre-announcement positioning
    POST = "post"        # stage 2 — post-announcement PEAD or reversal

class TradeSignal(BaseModel):
    # Identity
    id            str         uuid, auto-generated
    created_at    datetime    auto-generated
    ticker        str

    # Event context
    event_type    EventType
    stage         SignalStage
    stage1_id     str | None  links stage 2 back to its OpenStage1Position

    # Direction & sizing
    direction     SignalDirection
    size_pct      float       (0.0, 1.0]  fraction of max allowed position

    # Risk parameters
    stop_loss_pct   float     e.g. 0.04 = 4%
    take_profit_pct float | None   None for event-driven exits
    horizon_days    int       0 = immediate exit

    # Surprise delta payload (POST stage only)
    composite_surprise    float | None
    composite_confidence  float | None
    signal_strength       Literal["STRONG","MODERATE","WEAK","NONE"] | None

    # Confidence gating
    confidence_score        float | None   0.0–1.0
    passed_confidence_gate  bool           default False
    rejection_reason        str | None
```

### Model Validators

**`exit_signal_invariants`** — EXIT signals must have `horizon_days=0` and `take_profit_pct=None`.

**`stage1_has_no_surprise`** — PRE stage signals cannot carry `composite_surprise` (report not yet released). Hard error.

**`stage2_links_to_stage1`** — POST stage signals without `stage1_id` are valid (fresh PEAD entry) but receive a soft warning in `rejection_reason`.

### Key Design Decisions

- `passed_confidence_gate` defaults to `False` — every signal starts rejected; confidence logic must explicitly flip it
- `EXIT` is a first-class `SignalDirection` value, not a flag, keeping `RiskManagerAgent` and `ExecutionAgent` clean
- `SignalStage` uses readable strings (`"pre"`, `"post"`) for legibility in logs and SQLite trade history

---

## 5. Confidence Thresholds

### Architecture

Each signal's `confidence_score` is a weighted composite of four components. Weights vary per event type. A signal only proceeds to `RiskManagerAgent` if `confidence_score >= CONFIDENCE_GATES[event_type]`.

`RiskManagerAgent` checks `passed_confidence_gate` first — before position limits, drawdown, or conflict checks.

### Four Components — `ConfidenceScorer`

**`surprise_score(surprise)`**
- `min(max(eps_sigma, rev_sigma) / 3.0, 1.0)`
- Uses the higher of EPS and revenue sigma (market reacts to the more surprising metric)
- Returns 0.0 if no surprise data

**`sentiment_score(sentiment)`**
- `sentiment.confidence * abs(sentiment.score)`
- High confidence weak signal < moderate confidence strong signal
- Returns 0.0 if no sentiment data

**`coverage_score(analyst_count)`**
- `min(analyst_count / 10.0, 1.0)`, minimum 0.1 if count < 3
- More analysts = more reliable consensus = more meaningful surprise

**`source_score(source)`**
- Lookup against a source tier table
- Tier 1 (sec.gov, businesswire, prnewswire): 0.90–1.00
- Tier 2 (reuters, bloomberg, wsj, benzinga): 0.75–0.85
- Tier 3 (yahoo_finance, cnbc, marketwatch): 0.55–0.65
- Tier 4 (twitter, reddit): 0.15–0.20
- Unknown: 0.30

### Weight Matrix (per event type)

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

### Confidence Gates (minimum score to pass)

| Event Type | Gate | Rationale |
|---|---|---|
| `EARN_PRE` | 0.45 | Optionality bet, small size — lower bar acceptable |
| `EARN_BEAT` | 0.55 | Published number — moderate bar |
| `EARN_MISS` | 0.55 | Same |
| `EARN_MIXED` | 1.01 | Never passes — no signal generated |
| `GUID_UP` | 0.50 | — |
| `GUID_DOWN` | 0.50 | — |
| `GUID_WARN` | 0.60 | Off-cycle — must be credible source |
| `MA_TARGET` | 0.65 | — |
| `MA_ACQUIRER` | 0.65 | — |
| `MA_RUMOUR` | 0.75 | Highest equity threshold — unverified |
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

Gates are stored in `config.py` as a `dict[str, float]` and overridable per environment via `.env`.

---

## 6. Earnings Calendar Integration

### Architecture Decision

`EarningsCalendarAgent` is a dedicated agent that runs on a **daily cron schedule (07:00 ET, Mon–Fri)**, outside the main LangGraph pipeline. It feeds the pipeline by publishing synthetic `NewsEvent` objects with `event_type=EARN_PRE` to the Redis event bus. The rest of the pipeline is unchanged — it treats calendar-sourced events identically to news-sourced events.

### Data Providers

**Primary:** `FMPCalendarProvider` — `GET /earning_calendar?from=&to=`  
**Fallback:** `YFinanceCalendarProvider` — unofficial scraping, no API key, used only when FMP returns empty or raises

Fallback is triggered automatically; no configuration required.

### Model — `models/calendar.py`

```
ReportTiming    StrEnum: PRE_MARKET | POST_MARKET | UNKNOWN

EarningsCalendarEntry   BaseModel
  ticker              str
  report_date         date
  fiscal_quarter      str
  fiscal_year         int
  timing              ReportTiming  default UNKNOWN
  eps_estimate        float | None
  fetched_at          datetime
  days_until_report   computed
  is_actionable       computed   True if 2 <= days_until_report <= 5
```

**Actionable window rationale:**
- Beyond 5 days: signal decays before event; IV not yet elevated
- Under 2 days: IV already elevated; poor entry economics

### Provider Protocol — `providers/protocols.py`

```python
class CalendarProvider(Protocol):
    async def get_upcoming_earnings(
        tickers: list[str],
        from_date: date,
        to_date: date,
    ) -> list[EarningsCalendarEntry]: ...
```

Implementations:
- `providers/calendar/fmp.py` — `FMPCalendarProvider`
- `providers/calendar/yfinance_provider.py` — `YFinanceCalendarProvider`

### Agent Behaviour

```
EarningsCalendarAgent.run()
  scan_window = today → today + 5 days
  entries = FMP.get_upcoming_earnings(watchlist, window)
           fallback to yfinance if FMP empty/fails
  filter  is_actionable entries only
  for each entry:
    event_id = f"calendar_earn_pre_{ticker}_{report_date}"
    skip if event_id already in SQLite    (dedup guard)
    synthesise NewsEvent(event_type=EARN_PRE, source="earnings_calendar")
    publish → Redis event bus
    persist entry → SQLite
```

Dedup guard reuses the same pattern as `NewsIngestorAgent._is_duplicate()`, ensuring `EARN_PRE` fires exactly once per ticker per report date even if the scheduler misfires.

### Synthesised NewsEvent Fields

```
event_id        f"calendar_earn_pre_{ticker}_{report_date}"
ticker          from EarningsCalendarEntry
headline        "{ticker} scheduled to report {quarter} earnings on {date} ({timing})"
source          "earnings_calendar"
event_type      EventType.EARN_PRE
report_date     from entry
fiscal_quarter  from entry
metadata        { days_until_report, eps_estimate, timing }
```

### Cron Scheduling — `main.py`

Uses `APScheduler` (`apscheduler>=3.10`):

```python
scheduler.add_job(
    calendar_agent.run,
    trigger="cron",
    hour=7, minute=0,
    day_of_week="mon-fri",
    misfire_grace_time=300,
)
```

The scheduler runs alongside the main pipeline loop inside the same async process.

---

## 7. End-to-End Flow

### Run A — Stage 1 (T-4 days before earnings, 07:00 ET)

```
EarningsCalendarAgent (cron)
  → FMP: AAPL reports in 4 days, beat_rate = 0.72
  → synthesises NewsEvent(EARN_PRE) → Redis

Pipeline (consuming from bus)
  NewsIngestorAgent   passes through (source = "earnings_calendar")
  MarketDataAgent     fetches current OHLCV snapshot
  FMPEstimatesProvider  returns historical beat rate = 0.72
  SignalGeneratorAgent
    direction = long, size_pct = 0.33
    persists OpenStage1Position(status=OPEN) → SQLite
    emits TradeSignal(stage=PRE, size=0.33, stop=4%)
  ConfidenceGate      score vs 0.45 threshold
  RiskManagerAgent    validates
  ExecutionAgent      places 33% position on Alpaca
```

### Run B — Stage 2 (T+0, post-market announcement)

```
NewsIngestorAgent   classifies EARN_BEAT (EPS +9.2%, rev +4.1%)
MarketDataAgent     fetches updated snapshot
FMPEstimatesProvider  returns actual vs consensus → EarningsSurprise built
  composite_surprise = +11.4, signal_strength = STRONG
SentimentAnalystAgent  analyses press release + call transcript
  guidance_sentiment = +0.6 → guidance_direction = BEAT
SignalGeneratorAgent
  loads OpenStage1Position(direction=long) from SQLite
  stage 1 confirmed → adds remaining 67% of position
  horizon = 4 days (STRONG)
  updates status = CONFIRMED
  emits TradeSignal(stage=POST, size=0.67, stop=6%, tp=12%)
ConfidenceGate      score vs 0.55 threshold
RiskManagerAgent    full position check
ExecutionAgent      adds to existing Alpaca position
```

### Run C — PEAD Exit (T+4)

```
Triggered by horizon expiry in ExecutionAgent
Closes full combined position (stage 1 + stage 2)
Updates OpenStage1Position.status = EXITED
```

---

## 8. New Issues

| # | Title | File | Depends on |
|---|---|---|---|
| 10 | Add `EarningsCalendarEntry` model | `models/calendar.py` | — |
| 11 | Implement `FMPCalendarProvider` + `YFinanceCalendarProvider` | `providers/calendar/` | #10 |
| 12 | Implement `EarningsCalendarAgent` with dedup guard | `agents/earnings_calendar.py` | #10, #11 |
| 13 | Wire cron scheduler into `main.py` | `main.py` | #12 |
| 14 | Add `EarningsSurprise` + `MetricSurprise` models | `models/surprise.py` | — |
| 15 | Add `OpenStage1Position` + `Stage1Status` | `models/positions.py` | — |
| 16 | Update `TradeSignal` model with stage fields | `models/signals.py` | #15 |
| 17 | Implement `FMPEstimatesProvider` | `providers/estimates/fmp.py` | #14 |
| 18 | Implement `ConfidenceScorer` + `apply_confidence_gate` | `agents/signal_generator.py` | #14, #16 |
| 19 | Add `EstimatesProvider` + `CalendarProvider` to protocols | `providers/protocols.py` | #14, #10 |

### Suggested Implementation Order

```
#10 EarningsCalendarEntry
#14 EarningsSurprise + MetricSurprise
#15 OpenStage1Position
#16 TradeSignal updates          ← depends on #15
#19 Protocol additions           ← depends on #10, #14
#11 Calendar providers           ← depends on #10
#17 FMPEstimatesProvider         ← depends on #14
#12 EarningsCalendarAgent        ← depends on #11
#18 ConfidenceScorer + gate      ← depends on #14, #16
#13 Cron scheduler in main.py    ← depends on #12
```
