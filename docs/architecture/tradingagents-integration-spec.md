# TradingAgents Integration Spec

> **Reference repo:** [github.com/TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (read-only local clone — never a package dependency)
> **Integration mode:** Pattern extraction — no PyPI dependency
> **Target agents:** `SignalGeneratorAgent`, `SentimentAnalystAgent`, `ConfidenceScorer`, `Stage1Repository`
> **Workflow:** Read → design in chat (pseudocode) → implement via Claude Code

---

## Contents

1. [Overview](#1-overview)
2. [Pattern A — Bull / Bear Debate in SignalGeneratorAgent](#2-pattern-a--bull--bear-debate-in-signalgeneratoragent)
3. [Pattern B — Deep / Quick LLM Split with Provider-Ready Factory](#3-pattern-b--deep--quick-llm-split-with-provider-ready-factory)
4. [Pattern C — Fundamentals Prompt Structure for ConfidenceScorer](#4-pattern-c--fundamentals-prompt-structure-for-confidencescorer)
5. [Pattern D — Reflection Loop in Stage1Repository](#5-pattern-d--reflection-loop-in-stage1repository)
6. [Multi-Provider Compatibility — Design Considerations](#6-multi-provider-compatibility--design-considerations)
7. [Recommended Implementation Order](#7-recommended-implementation-order)
8. [TradingAgents Source Code Reading Guide](#8-tradingagents-source-code-reading-guide)
9. [Quick Reference](#9-quick-reference)

---

## 1  Overview

[TradingAgents](https://github.com/TauricResearch/TradingAgents) is a multi-agent LLM trading framework built on LangGraph. It introduces four architectural patterns that are directly applicable to news-trade as native re-implementations — not as a package dependency.

> ⚠️ **TradingAgents is a reference implementation, not a library.** Clone it locally, read the relevant files, then implement each pattern natively inside news-trade using your existing Pydantic v2 / LangGraph / provider-abstraction stack. Never add it to `pyproject.toml`.

### 1.1  Why not import the package?

- **No stable PyPI release.** Depending on a GitHub HEAD breaks reproducibility and `uv.lock` integrity.
- **Heavyweight transitive deps.** BM25, multiple LLM client libraries, and Alpha Vantage SDK conflict with your cost-discipline philosophy.
- **Wrong execution model.** TradingAgents is a synchronous, per-ticker, on-demand analysis system. news-trade is an event-driven, always-on pipeline. The abstractions don't compose cleanly.

### 1.2  What is borrowed

| Pattern | Source in TradingAgents | Target in news-trade | Priority |
|---|---|---|---|
| A — Bull/Bear Debate | `agents/researchers/` | `SignalGeneratorAgent._debate_signal()` | High |
| B — Deep/Quick LLM Split | `default_config.py` + `llm_clients/` | `config.py` + `SentimentAnalystAgent` | High |
| C — Fundamentals Prompt Structure | `agents/analysts/fundamentals_analyst.py` | `ConfidenceScorer` prompt templates | Medium |
| D — Reflection / Memory Loop | `graph/` reflection logic | `Stage1Repository.load_historical_outcomes()` | Medium |

### 1.3  Setup: cloning TradingAgents as a local reference

Run once, outside the news-trade repo. Never install into your virtualenv.

```bash
# from your workspace root (outside the news-trade repo)
git clone https://github.com/TauricResearch/TradingAgents.git tradingagents-ref

# files you will read during implementation
tradingagents-ref/tradingagents/agents/researchers/                          # Pattern A
tradingagents-ref/tradingagents/default_config.py                            # Pattern B
tradingagents-ref/tradingagents/llm_clients/                                 # Pattern B
tradingagents-ref/tradingagents/agents/analysts/fundamentals_analyst.py      # Pattern C
tradingagents-ref/tradingagents/graph/                                       # Pattern D
```

---

## 2  Pattern A — Bull / Bear Debate in SignalGeneratorAgent

### 2.1  What TradingAgents does

> 📂 **Source to read:** `tradingagents-ref/tradingagents/agents/researchers/`

TradingAgents runs a structured debate between a Bull researcher and a Bear researcher before the Trader agent produces a final decision. Each researcher receives the same analyst reports and argues their position for N rounds. A synthesiser then weighs both arguments into an investment plan.

Key things to study in their code:

- **Prompt structure:** how the bull/bear roles are framed in the system prompt, what context they receive, and how round history is accumulated in the message thread.
- **Termination logic:** how `max_debate_rounds` is checked and how the synthesiser collapses the debate into a structured output.
- **State threading:** how partial debate state is passed between LangGraph nodes without leaking into the main `PipelineState`.

### 2.2  Why it matters for news-trade

The current `SignalGeneratorAgent` takes `SentimentResult` + `MarketSnapshot` and produces a `TradeSignal` in a single forward pass — no internal challenge mechanism. High-confidence `EARN_PRE` signals commit capital 3–5 days before a report with no adversarial check. The debate pattern adds a lightweight self-critique step before `passed_confidence_gate` is flipped to `True`.

> ℹ️ The debate is most valuable for Stage 1 `EARN_PRE` signals and any signal with `composite_confidence` above 0.80 — precisely where the cost of a false positive is highest.

### 2.3  Design

#### New config keys (`config.py`)

```python
signal_debate_rounds: int = 0                                  # 0 = disabled (default, cost-controlled)
signal_debate_model: str = 'claude-haiku-4-5-20251001'         # cheap model for debate rounds
signal_debate_threshold: float = 0.70                          # only debate signals above this confidence
```

> ℹ️ Default is `0` (off). This preserves existing behaviour and keeps API costs flat during development. Enable with `signal_debate_rounds = 1` or `2` in `.env` when calibrating.

#### New method `SignalGeneratorAgent._debate_signal()` (pseudocode)

```
method _debate_signal(signal: TradeSignal, context: DebateContext) -> DebateResult:

  if settings.signal_debate_rounds == 0:
    return DebateResult(verdict=signal.signal_direction, rounds=[])

  if signal.confidence_score < settings.signal_debate_threshold:
    return DebateResult(verdict=signal.signal_direction, rounds=[])

  history: list[DebateRound] = []

  for round_n in range(settings.signal_debate_rounds):

    bull_prompt  = build_bull_prompt(signal, context, history)
    bull_response  = llm_quick.invoke(bull_prompt)     # cheap model

    bear_prompt  = build_bear_prompt(signal, context, history)
    bear_response  = llm_quick.invoke(bear_prompt)

    history.append(DebateRound(round=round_n, bull=bull_response, bear=bear_response))

  synthesis_prompt = build_synthesis_prompt(signal, history)
  verdict = llm_deep.invoke(synthesis_prompt)          # structured: CONFIRM / REDUCE / REJECT

  return DebateResult(verdict=verdict.direction, confidence_delta=verdict.delta, rounds=history)
```

#### New Pydantic models (`models/signals.py`)

```python
class DebateRound(BaseModel):
    round_number:   int
    bull_argument:  str
    bear_argument:  str

class DebateVerdict(str, Enum):
    CONFIRM  = 'CONFIRM'   # proceed with original signal unchanged
    REDUCE   = 'REDUCE'    # proceed but halve position size
    REJECT   = 'REJECT'    # flip passed_confidence_gate back to False

class DebateResult(BaseModel):
    verdict:           DebateVerdict
    confidence_delta:  float = 0.0    # applied to TradeSignal.confidence_score
    rounds:            list[DebateRound] = []

# Add to TradeSignal:
debate_result: DebateResult | None = None
```

#### Integration point in `SignalGeneratorAgent.run()`

```python
# After ConfidenceScorer passes the gate:
if signal.passed_confidence_gate:
    debate = self._debate_signal(signal, context)
    signal = signal.model_copy(update={
        'debate_result': debate,
        'confidence_score': signal.confidence_score + debate.confidence_delta,
    })
    if debate.verdict == DebateVerdict.REJECT:
        signal = signal.model_copy(update={
            'passed_confidence_gate': False,
            'rejection_reason': 'Debate: bear thesis dominated',
        })
    elif debate.verdict == DebateVerdict.REDUCE:
        signal = signal.model_copy(update={
            'signal_strength': signal.signal_strength * 0.5,
        })
```

### 2.4  Step-by-step implementation checklist

1. Read `tradingagents-ref/tradingagents/agents/researchers/` — study `bull_researcher.py`, `bear_researcher.py`, and the synthesis logic.
2. **Design prompt templates in chat (pseudocode only).** Do not write Python yet.
3. Add `signal_debate_rounds`, `signal_debate_model`, `signal_debate_threshold` to `config.py` via Claude Code.
4. Add `DebateRound`, `DebateVerdict`, `DebateResult` to `models/signals.py` via Claude Code.
5. Add `debate_result: DebateResult | None = None` to `TradeSignal` via Claude Code.
6. Implement `_debate_signal()` in `SignalGeneratorAgent` via Claude Code.
7. Wire into `run()` after the confidence gate check via Claude Code.
8. Add unit tests: gate-off path, below-threshold path, `CONFIRM` / `REDUCE` / `REJECT` verdicts.

> ⚠️ Do not run the debate in the happy path of every signal. The threshold guard is essential for cost control.

---

## 3  Pattern B — Deep / Quick LLM Split with Provider-Ready Factory

### 3.1  What TradingAgents does

> 📂 **Source to read:** `tradingagents-ref/tradingagents/default_config.py` and `tradingagents-ref/tradingagents/llm_clients/`

TradingAgents separates LLM calls into two tiers: a fast, cheap model (`quick_think_llm`) for high-throughput tasks like tool calls and data formatting, and a more capable model (`deep_think_llm`) for complex multi-step reasoning like final trading decisions. The factory pattern in `llm_clients/` handles provider switching without touching agent logic.

### 3.2  Why it matters for news-trade

news-trade currently uses `claude-sonnet-4-6` for all `SentimentAnalystAgent` calls regardless of task complexity. The same expensive model handles both:

- Initial classification of a news headline (which may be discarded 3 lines later by the watchlist filter)
- Full four-component confidence scoring that gates an `EARN_PRE` signal worth thousands of dollars

These have very different value-to-cost ratios. Routing them to appropriately priced models reduces Anthropic API spend with no accuracy loss on the cheap tasks.

Beyond cost, the factory is the **correct place to introduce multi-provider compatibility later** — without touching any agent code. The `LLMClient` protocol defined here is the seam that makes OpenAI, Gemini, and others drop-in additions in the future. See [Section 6](#6-multi-provider-compatibility--design-considerations) for full design considerations.

### 3.3  Design

#### New config keys (`config.py`)

```python
# LLM provider and tier configuration
llm_provider:    str = 'anthropic'                     # 'anthropic' only for now — protocol-ready for others
llm_quick_model: str = 'claude-haiku-4-5-20251001'     # cheap, fast — classification, extraction, debate rounds
llm_deep_model:  str = 'claude-sonnet-4-6'             # accurate — confidence scoring, EARN_PRE synthesis, debate verdict

# Tasks routed to quick model:
#   - initial event_type classification
#   - entity/ticker extraction from headlines
#   - dedup similarity checks
#   - debate rounds (Pattern A)

# Tasks routed to deep model:
#   - full ConfidenceScorer four-component scoring
#   - EARN_PRE synthesis with historical beat rate
#   - debate synthesis verdict (Pattern A)
```

#### `LLMClient` protocol (`services/llm_client.py` — new file)

The protocol is the key design decision. It is the boundary that keeps all agent code provider-agnostic. Define it first; the factory and concrete implementations satisfy it.

```python
# pseudocode — implement via Claude Code

class LLMResponse(BaseModel):
    content:   str
    model_id:  str       # exact model string used — e.g. 'claude-sonnet-4-6'
    provider:  str       # e.g. 'anthropic'

class LLMClient(Protocol):
    """Single abstract call site. All providers implement this interface."""

    def invoke(
        self,
        prompt:          str,
        system:          str | None = None,
        response_schema: type[BaseModel] | None = None,   # if set, return structured JSON
    ) -> LLMResponse: ...

    @property
    def model_id(self) -> str: ...     # used for signal provenance tracking

    @property
    def provider(self) -> str: ...
```

> ℹ️ `response_schema` is the critical parameter that abstracts structured output across providers. Anthropic uses tool-use JSON extraction; OpenAI has `response_format: {type: "json_schema"}`; Gemini has its own schema enforcement. Each concrete implementation handles this internally — agents never see the difference.

#### `LLMClientFactory` (`services/llm_client.py`)

```python
# pseudocode — implement via Claude Code
class LLMClientFactory:
    def __init__(self, settings: Settings):
        self._quick = _build_client(settings.llm_provider, settings.llm_quick_model)
        self._deep  = _build_client(settings.llm_provider, settings.llm_deep_model)

    @property
    def quick(self) -> LLMClient: ...   # typed against the Protocol, not a concrete class

    @property
    def deep(self) -> LLMClient: ...

def _build_client(provider: str, model: str) -> LLMClient:
    match provider:
        case 'anthropic': return AnthropicLLMClient(model)
        case _: raise ValueError(f"Unsupported provider: {provider}. Add implementation first.")

# AnthropicLLMClient is the only concrete implementation for now.
# OpenAILLMClient, GeminiLLMClient added here later — zero agent changes required.
```

#### `AnthropicLLMClient` (the only concrete implementation, for now)

```python
# pseudocode — implement via Claude Code
class AnthropicLLMClient:
    """Satisfies LLMClient protocol. Anthropic-only."""

    def __init__(self, model: str):
        self._model  = model
        self._client = anthropic.Anthropic()

    def invoke(self, prompt, system=None, response_schema=None) -> LLMResponse:
        if response_schema:
            # use tool-use JSON extraction — Anthropic's reliable structured output pattern
            response = self._client.messages.create(
                model=self._model,
                tools=[schema_to_tool(response_schema)],
                tool_choice={"type": "tool", "name": response_schema.__name__},
                ...
            )
        else:
            response = self._client.messages.create(model=self._model, ...)

        return LLMResponse(
            content=extract_content(response),
            model_id=self._model,
            provider='anthropic',
        )

    @property
    def model_id(self) -> str: return self._model

    @property
    def provider(self) -> str: return 'anthropic'
```

#### `model_id` tracking on output models

Every model produced by an LLM call must record which model produced it. Add to both `SentimentResult` and `TradeSignal`:

```python
# models/sentiment.py
class SentimentResult(BaseModel):
    ...
    model_id:  str        # e.g. 'claude-sonnet-4-6'
    provider:  str        # e.g. 'anthropic'

# models/signals.py
class TradeSignal(BaseModel):
    ...
    model_id:  str        # model that produced the confidence score
    provider:  str
```

> ℹ️ These two fields cost almost nothing to add now and make future cross-model comparison possible without a schema migration. Every signal must know which model produced it — this is essential for diagnosing signal quality regressions when models are upgraded.

#### Task routing in `SentimentAnalystAgent`

```python
# Quick model — cheap, fast
def _classify_event_type(headline: str) -> EventType:
    resp = self.llm.quick.invoke(CLASSIFY_PROMPT.format(headline=headline))
    return EventType(resp.content)

def _extract_tickers(text: str) -> list[str]:
    resp = self.llm.quick.invoke(TICKER_EXTRACT_PROMPT.format(text=text))
    return parse_tickers(resp.content)

# Deep model — accurate, slower — note: model_id captured from response
def _score_confidence(event: NewsEvent, market: MarketSnapshot) -> ConfidenceScore:
    resp = self.llm.deep.invoke(CONFIDENCE_PROMPT.format(...), response_schema=ConfidenceScore)
    return ConfidenceScore.model_validate_json(resp.content)

def _synthesise_earn_pre(event: NewsEvent, estimates: EstimatesData) -> SentimentResult:
    resp = self.llm.deep.invoke(EARN_PRE_PROMPT.format(...), response_schema=SentimentResult)
    result = SentimentResult.model_validate_json(resp.content)
    return result.model_copy(update={'model_id': resp.model_id, 'provider': resp.provider})
```

### 3.4  Step-by-step implementation checklist

1. Read `tradingagents-ref/tradingagents/default_config.py` — note the `deep_think_llm` / `quick_think_llm` keys and their defaults.
2. Read `tradingagents-ref/tradingagents/llm_clients/` — understand the factory pattern and constructor injection.
3. ✅ Add `llm_provider`, `llm_quick_model`, `llm_deep_model` to `config.py`.
4. ✅ Create `services/llm_client.py` with `LLMClient` protocol, `LLMClientFactory`, and `AnthropicLLMClient`.
5. ✅ Add `provider` field to `SentimentResult`; add `model_id` + `provider` fields to `TradeSignal`.
6. Update all agent constructors to accept `llm: LLMClientFactory` via Claude Code.
7. Update `OrchestratorAgent` to instantiate and inject `LLMClientFactory` via Claude Code.
8. Audit `SentimentAnalystAgent` — tag each call as quick or deep, route accordingly, propagate `model_id` to output models via Claude Code.
9. ✅ Add tests: protocol is satisfied by `AnthropicLLMClient`, factory returns correct tier, `model_id` is present on all output models, unsupported provider raises `ValueError`.

> ℹ️ Steps 6–8 are deferred until `SignalGeneratorAgent` is implemented. The current routing happens at the factory level: `get_sentiment_provider()` injects `LLMClientFactory.deep` into `ClaudeSentimentProvider`. Per-call quick/deep routing within agents becomes relevant once agents make multiple distinct LLM calls.

> ✅ Haiku is ~25x cheaper than Sonnet per token. Even routing 50% of calls to Haiku meaningfully cuts daily API spend. The `LLMClient` protocol means adding a second provider later requires writing one new class — no agent changes.

---

## 4  Pattern C — Fundamentals Prompt Structure for ConfidenceScorer

### 4.1  What TradingAgents does

> 📂 **Source to read:** `tradingagents-ref/tradingagents/agents/analysts/fundamentals_analyst.py`

TradingAgents renders financial data — EPS history, revenue trends, analyst estimate ranges, and prior surprise deltas — into a structured narrative block before asking the LLM to reason about it. The data is not dumped as raw JSON. Instead it is formatted as a mini-report with labeled sections, explicit units, and contextual anchors (e.g., comparing current estimates to the prior 4-quarter mean).

This structured pre-rendering pattern is directly applicable to the `composite_surprise` calculation inside `ConfidenceScorer`, which currently receives raw FMP JSON.

### 4.2  Why it matters for news-trade

The `ConfidenceScorer` computes four components: `surprise_score`, `sentiment_score`, `coverage_score`, `source_score`. The `surprise_score` is the most complex — it depends on EPS consensus, revenue consensus, analyst high/low range, and historical beat rate. If the LLM receives raw FMP JSON for this calculation, it must:

- parse non-obvious field names (`epsEstimated`, `revenueEstimated`, `actualEps`)
- infer units and magnitude from context
- internally compute the surprise delta and normalise it

Structured pre-rendering moves this work out of the LLM call and into deterministic Python, reducing token usage and improving scoring consistency across models.

### 4.3  Design

#### New utility `services/estimates_renderer.py` (new file)

```python
# pseudocode — implement via Claude Code
class EstimatesRenderer:
    """Converts raw FMP EstimatesData into a structured narrative
    block for LLM consumption. No LLM calls — pure Python formatting."""

    def render(self, ticker: str, data: EstimatesData) -> str:
        return f"""
=== EARNINGS ESTIMATES: {ticker} ===

Report date:         {data.report_date}
Fiscal period:       {data.fiscal_period}

EPS consensus:       ${data.eps_estimate:.2f}
EPS analyst range:   ${data.eps_low:.2f} — ${data.eps_high:.2f}
Prior 4Q EPS mean:   ${data.eps_trailing_mean:.2f}

Revenue consensus:   ${data.revenue_estimate/1e6:.0f}M
Revenue range:       ${data.revenue_low/1e6:.0f}M — ${data.revenue_high/1e6:.0f}M

Historical beat rate (last 8Q): {data.historical_beat_rate:.0%}
Mean EPS surprise (last 8Q):    {data.mean_eps_surprise:+.1%}

Analyst coverage:    {data.num_analysts} analysts
Estimate dispersion: {data.estimate_dispersion:.3f}  (std/mean — lower = higher consensus)
"""

    def compute_pre_surprise_delta(self, data: EstimatesData) -> float:
        """Normalised surprise delta used as surprise_score input.
        Range -1.0 to +1.0. Positive = above consensus, negative = below."""
        ...
```

#### Updated `ConfidenceScorer._score_surprise()`

```python
# Before (current): LLM receives raw JSON
prompt = SURPRISE_PROMPT.format(estimates_json=estimates.model_dump_json())

# After (Pattern C): LLM receives structured narrative
narrative  = EstimatesRenderer().render(ticker, estimates)
pre_delta  = EstimatesRenderer().compute_pre_surprise_delta(estimates)
prompt = SURPRISE_PROMPT.format(
    estimates_narrative=narrative,
    pre_computed_delta=pre_delta,   # LLM validates, not computes
)
```

### 4.4  Step-by-step implementation checklist

1. ✅ **Read `fundamentals_analyst.py` carefully.** Focus on how data is structured before the LLM call, not on the agent wiring around it.
2. ✅ Map every FMP field returned by your `EstimatesProvider` to a human-readable label and unit.
3. ✅ Create `services/estimates_renderer.py` with `EstimatesRenderer` — pure Python, no LLM call.
4. ✅ Implement `compute_pre_surprise_delta()` as pure Python — primary path uses `eps_trailing_mean`; fallback uses `mean_eps_surprise`; result clamped to `[-1, 1]`.
5. ✅ Create `services/confidence_scorer.py` with `ConfidenceScorer` — 4-component weighted matrix keyed by `EventType`; `apply_gate()` stamps result onto `TradeSignal` via `model_copy()`.
6. ✅ Create `models/surprise.py` — `EstimatesData` (with `estimate_dispersion` computed field), `MetricSurprise` (pct/sigma/direction/confidence), `EarningsSurprise` (composite + signal strength tier).
7. ✅ Expand `EventType` with 20 fine-grained values (EARN_PRE/BEAT/MISS/MIXED, GUID_*, MA_*, REG_*, sector contagion) while keeping 8 coarse values for backward compatibility.
8. ✅ Add confidence fields to `TradeSignal`: `signal_strength`, `confidence_score`, `passed_confidence_gate`, `rejection_reason`.
9. ✅ Add unit tests: 206 tests pass — renderer determinism, delta clamping, all 4 scorer components, gate pass/fail, `EARN_MIXED` always fails.
10. TODO: Update `SURPRISE_PROMPT` template to use the narrative format when `SignalGeneratorAgent` LLM integration is implemented (depends on Pattern A).

> ℹ️ `estimate_dispersion` (std/mean of analyst estimates) is a particularly useful signal not currently in your `ConfidenceScorer`. High dispersion = low consensus = lower `source_score` weighting. Worth adding as a field to `EstimatesData`.

---

## 5  Pattern D — Reflection Loop in Stage1Repository

### 5.1  What TradingAgents does

> 📂 **Source to read:** `tradingagents-ref/tradingagents/graph/` (reflection and memory update logic)

TradingAgents maintains a memory store that accumulates outcomes from past trading decisions. After each trade, a reflection agent reads the outcome (win/loss, actual vs expected move), generates a lesson summary, and writes it back to memory. Future analysis runs query this memory for the same ticker before the Analyst Team fires.

The key idea to extract: **past outcomes per ticker actively modulate future signal confidence**, rather than treating each event as independent.

### 5.2  Why it matters for news-trade

The `EARN_PRE` two-stage model already has a `historical_beat_rate` field influencing Stage 1 position size. However, this rate is currently a static config value or a raw FMP historical query — it is not updated by your own system's observed outcomes.

Adapting the reflection pattern means:

- When Stage 1 is `CONFIRMED` → the beat happened → increment ticker beat count
- When Stage 1 is `REVERSED` → the miss happened → increment ticker miss count
- When Stage 1 is `EXPIRED` → no report detected → log as data quality issue

Over time, news-trade accumulates its own per-ticker beat rate, which is more accurate than FMP historical data because it reflects the specific subset of events that passed your confidence gate — a much narrower and more relevant population.

### 5.3  Design

#### New ORM table (`services/database.py`)

```python
class EarningsOutcomeRow(Base):
    __tablename__ = 'earnings_outcomes'

    id:                Mapped[int]        = mapped_column(primary_key=True)
    ticker:            Mapped[str]        = mapped_column(index=True)
    report_date:       Mapped[date]
    stage1_id:         Mapped[str]        = mapped_column(ForeignKey('stage1_positions.id'))
    final_status:      Mapped[str]        # CONFIRMED / REVERSED / EXPIRED
    eps_surprise_pct:  Mapped[float | None]   # actual vs consensus, nullable
    price_move_1d:     Mapped[float | None]   # % move day-of report
    recorded_at:       Mapped[datetime]   = mapped_column(default=func.now())
```

#### New method `Stage1Repository.load_historical_outcomes()` (pseudocode)

```
method load_historical_outcomes(
    ticker: str,
    lookback_quarters: int = 8
) -> HistoricalOutcomes:

  rows = session.query(EarningsOutcomeRow)
      .filter_by(ticker=ticker)
      .order_by(report_date.desc())
      .limit(lookback_quarters)
      .all()

  if not rows:
    return HistoricalOutcomes(source='fmp', beat_rate=fmp_fallback(ticker))

  beats  = sum(1 for r in rows if r.final_status == 'CONFIRMED')
  misses = sum(1 for r in rows if r.final_status == 'REVERSED')
  total  = beats + misses   # exclude EXPIRED

  return HistoricalOutcomes(
      source='observed',
      beat_rate=beats/total if total > 0 else None,
      sample_size=total,
      mean_eps_surprise=mean(r.eps_surprise_pct for r in rows if r.eps_surprise_pct),
      mean_price_move_1d=mean(r.price_move_1d for r in rows if r.price_move_1d),
  )
```

#### New method `Stage1Repository.record_outcome()` (pseudocode)

```
method record_outcome(
    stage1_id: str,
    final_status: Stage1Status,
    eps_surprise_pct: float | None,
    price_move_1d: float | None,
) -> None:

  row = session.get(OpenStage1PositionRow, stage1_id)
  outcome = EarningsOutcomeRow(
      ticker=row.ticker,
      report_date=row.expected_report_date,
      stage1_id=stage1_id,
      final_status=final_status.value,
      eps_surprise_pct=eps_surprise_pct,
      price_move_1d=price_move_1d,
  )
  session.add(outcome)
  session.commit()
```

#### Integration with `EarningsCalendarAgent`

```python
# When synthesising an EARN_PRE NewsEvent:
outcomes = stage1_repo.load_historical_outcomes(ticker)

if outcomes.source == 'observed' and outcomes.sample_size >= 4:
    beat_rate = outcomes.beat_rate          # own observed data — more relevant than FMP
else:
    beat_rate = estimates_provider.get_historical_beat_rate(ticker)   # FMP fallback

stage1_size = base_size * beat_rate         # existing sizing logic unchanged
```

### 5.4  Step-by-step implementation checklist

1. Read `tradingagents-ref/tradingagents/graph/` reflection logic — focus on how outcomes are written back and how they are queried on the next run.
2. Add `EarningsOutcomeRow` to `services/database.py` with Alembic migration via Claude Code.
3. Add `HistoricalOutcomes` Pydantic model to `models/signals.py` via Claude Code.
4. Implement `Stage1Repository.load_historical_outcomes()` via Claude Code.
5. Implement `Stage1Repository.record_outcome()` via Claude Code.
6. Wire `record_outcome()` into the `ExpiryScanner` cron when it flips a position to `CONFIRMED`, `REVERSED`, or `EXPIRED` via Claude Code.
7. Wire `load_historical_outcomes()` into `EarningsCalendarAgent` beat rate lookup via Claude Code.
8. Add unit tests: outcomes accumulate correctly, FMP fallback triggers on empty history, `beat_rate` matches hand-computed value.

> ℹ️ The system starts in **bootstrapping mode** — all `load_historical_outcomes()` calls fall back to FMP because the outcomes table is empty. After ~8 quarters of live operation, observed data takes over. This is by design.

---

## 6  Multi-Provider Compatibility — Design Considerations

The `LLMClient` protocol introduced in Pattern B is specifically designed so that adding OpenAI, Gemini, or other providers later requires writing **one new class** with no changes to any agent. This section documents the design rationale, trade-offs, and the concrete steps needed when the time comes to add a second provider.

### 6.1  Why not implement multi-provider now?

news-trade is Anthropic-first by design, and adding providers prematurely creates real costs:

- **Prompt portability is lower than it looks.** Prompts are tuned against Claude's instruction-following behaviour — system prompt phrasing, JSON extraction patterns, and handling of ambiguous financial content all need per-provider tuning. This is not free.
- **Structured output APIs differ meaningfully.** Anthropic uses tool-use JSON extraction. OpenAI has native `response_format: {type: "json_schema"}`. Gemini has its own schema enforcement. Each provider's `invoke()` implementation must handle this internally.
- **Confidence scoring calibration shifts.** `CONFIDENCE_GATES` thresholds and `ConfidenceScorer` weights were implicitly calibrated against Claude's output distribution. Switching providers without recalibrating produces signals with systematically different `confidence_score` distributions.
- **Maintenance surface multiplies.** Every provider deprecation cycle (GPT-4o → GPT-5, Gemini 2.0 → 2.5, etc.) is a potential re-tuning event. Currently you track one provider's release cadence.

### 6.2  What is already in place (from Pattern B)

The groundwork is laid without doing the extra work prematurely:

- `LLMClient` protocol defines the single call site all providers must satisfy.
- `_build_client(provider, model)` is the one place a new provider is registered — the `match` statement raises `ValueError` for anything not yet implemented.
- `model_id` and `provider` fields on `SentimentResult` and `TradeSignal` ensure every signal carries its provenance. Cross-model comparison and regression diagnosis are possible from day one without a future schema migration.
- `llm_provider` config key is already present — switching providers is a `.env` change, not a code change, once the implementation exists.

### 6.3  Trade-off summary

| Concern | Severity | Mitigation |
|---|---|---|
| Prompt re-tuning per provider | Medium | Maintain per-provider prompt variant files; A/B test via `risk_dry_run` mode before live |
| Structured output API differences | Medium | Encapsulated entirely inside each `LLMClient` implementation — agents are unaffected |
| Confidence score distribution shift | High | Re-run `ConfidenceScorer` calibration suite against new provider before enabling in production |
| `model_id` tracking gaps | Low | Already solved by Pattern B — `model_id` on all output models from day one |
| Maintenance surface | Low-Medium | Anthropic-first default; other providers are opt-in via `llm_provider` config |

### 6.4  When to add a second provider

The right trigger is a concrete need, not speculative flexibility. Reasonable triggers:

- **Anthropic outage resilience** — configure OpenAI as a hot standby for the quick tier only. Low risk because quick-tier tasks (classification, extraction) are less sensitive to provider-specific reasoning differences.
- **Cost arbitrage** — GPT-4o-mini or Gemini Flash occasionally undercuts Haiku. Worth evaluating quarterly.
- **Research comparison** — running the same news event through multiple providers to quantify signal variance is genuinely useful during calibration phases.

### 6.5  How to add OpenAI (when the time comes)

Effort estimate: 2–3 days via Claude Code once Pattern B is implemented.

```python
# pseudocode — implement via Claude Code when needed

class OpenAILLMClient:
    """Satisfies LLMClient protocol. OpenAI-only."""

    def __init__(self, model: str):
        self._model  = model
        self._client = openai.OpenAI()

    def invoke(self, prompt, system=None, response_schema=None) -> LLMResponse:
        if response_schema:
            # OpenAI native structured output — stricter than Anthropic tool-use
            response = self._client.beta.chat.completions.parse(
                model=self._model,
                response_format=response_schema,   # Pydantic model passed directly
                ...
            )
        else:
            response = self._client.chat.completions.create(model=self._model, ...)

        return LLMResponse(
            content=extract_content(response),
            model_id=self._model,
            provider='openai',
        )

    @property
    def model_id(self) -> str: return self._model

    @property
    def provider(self) -> str: return 'openai'

# Register in _build_client():
case 'openai': return OpenAILLMClient(model)
```

Implementation checklist for OpenAI:

1. Add `openai` to `pyproject.toml` dependencies via Claude Code.
2. Implement `OpenAILLMClient` satisfying the `LLMClient` protocol via Claude Code.
3. Register `'openai'` case in `_build_client()` via Claude Code.
4. Write per-provider prompt variants for classification and confidence scoring tasks via Claude Code.
5. Run `ConfidenceScorer` calibration suite with `llm_provider = openai` — adjust gate thresholds if distributions differ significantly.
6. Add tests: `OpenAILLMClient` satisfies `LLMClient` protocol, `model_id` is correct, structured output parses to correct Pydantic model.

### 6.6  How to add Gemini (when the time comes)

Effort estimate: 4–5 days via Claude Code. Google's SDK diverges more from Anthropic's than OpenAI's does — context window management, streaming, and error handling all need separate handling.

```python
# pseudocode — implement via Claude Code when needed

class GeminiLLMClient:
    """Satisfies LLMClient protocol. Google Gemini-only."""

    def __init__(self, model: str):
        self._model  = model
        self._client = google.generativeai.GenerativeModel(model)

    def invoke(self, prompt, system=None, response_schema=None) -> LLMResponse:
        generation_config = {}
        if response_schema:
            # Gemini schema enforcement — different API shape from Anthropic and OpenAI
            generation_config['response_mime_type'] = 'application/json'
            generation_config['response_schema'] = pydantic_to_gemini_schema(response_schema)
        ...

        return LLMResponse(
            content=extract_content(response),
            model_id=self._model,
            provider='gemini',
        )
```

> ⚠️ `pydantic_to_gemini_schema()` is a non-trivial conversion utility — Gemini's schema format is not identical to JSON Schema. Build and test this carefully before wiring into confidence scoring.

### 6.7  What never changes regardless of provider

By design, the following files require **zero modifications** when a new provider is added:

- All agent files (`agents/`)
- All model files (`models/`)
- `services/confidence_scorer.py`
- `services/estimates_renderer.py`
- `graph/pipeline.py`
- `graph/state.py`

Only `services/llm_client.py` grows (one new class), `pyproject.toml` gains a dependency, and `.env.example` documents the new provider string. This is the payoff of the protocol boundary.

---

## 7  Recommended Implementation Order

Each pattern is independent — none depends on another. The order below is based on cost impact and implementation complexity.

| Order | Pattern | Rationale | Status |
|---|---|---|---|
| 1 | **B — Deep/Quick LLM Split** | Highest ROI: immediate cost reduction. Establishes the `LLMClient` protocol and `model_id` tracking that everything else builds on. | ✅ Done |
| 2 | **C — Fundamentals Prompt Structure** | Medium complexity. Adds `EstimatesRenderer` and updates `ConfidenceScorer` prompts. Improves scoring consistency before the Pattern A debate is layered on. | ✅ Done |
| 3 | **D — Reflection Loop** | New ORM table + migration. Starts bootstrapping the observed outcomes database immediately so it has data by the time Pattern A is tuned. | TODO |
| 4 | **A — Bull/Bear Debate** | Highest complexity. Depends on Pattern B (needs cheap debate model) and benefits from Pattern C (richer context for debaters). Implement last. | TODO |

> ⚠️ Each pattern should be a separate branch and PR. Do not bundle them. The test suite for each pattern should pass independently before the next pattern begins.

### 7.1  Dependency on existing issues

| Pattern | Requires | Notes |
|---|---|---|
| A — Debate | Issue #3 (`MarketSnapshot`) | `MarketSnapshot` is passed as `DebateContext` — needs the typed model. |
| B — LLM Split | None | Fully independent. Safe to implement immediately. |
| C — Prompt Structure | `EstimatesProvider` (architecture doc) | `EstimatesData` model must be finalised before the renderer is built. |
| D — Reflection | `Stage1Repository` (architecture doc) | `record_outcome()` extends an existing repo — interface must be stable first. |

---

## 8  TradingAgents Source Code Reading Guide

A concise map of exactly which files to read for each pattern, what to look for, and what to ignore.

### 8.1  Pattern A — `researchers/`

| File | What to focus on |
|---|---|
| `agents/researchers/bull_researcher.py` | System prompt framing (role definition + context given), how prior round history is appended to the prompt, output format expected from the LLM. |
| `agents/researchers/bear_researcher.py` | Same as above — note the structural symmetry with bull. The prompts are mirrors. |
| `graph/trading_graph.py` (debate loop) | How `max_debate_rounds` terminates the loop, how round results accumulate in graph state, how the synthesiser is invoked post-debate. |

> 🚫 **Ignore:** Their ChromaDB / BM25 memory integration (you use SQLite). Their graph state `TypedDict` (you have `PipelineState`). Extract prompt patterns and debate loop logic only.

### 8.2  Pattern B — `default_config.py` + `llm_clients/`

| File | What to focus on |
|---|---|
| `default_config.py` | The `deep_think_llm` / `quick_think_llm` key names and their defaults. The `online_tools` flag as a model for your own feature flags. How config flows into agent constructors. |
| `llm_clients/` (entire folder) | The factory pattern: how a single factory object provides both tiers. How the provider string maps to a concrete client class. Constructor injection pattern for agents. |

> 🚫 **Ignore:** All non-Anthropic provider implementations (OpenAI, Gemini, Grok, Ollama) in their `llm_clients/` — you are building your own `LLMClient` protocol. Study the factory structure and constructor injection pattern only.

### 8.3  Pattern C — `fundamentals_analyst.py`

| File | What to focus on |
|---|---|
| `agents/analysts/fundamentals_analyst.py` | How raw financial data fields are formatted into labeled sections before the LLM call. What contextual anchors are added (prior quarter comparisons, mean calculations). How the output is structured to be parseable by the downstream agent. |
| `dataflows/alpha_vantage_fundamentals.py` | Only to understand what data fields they pull and how they name them — then map these to your FMP field names. |

> 🚫 **Ignore:** Their actual Alpha Vantage API calls and data fetching. Your `EstimatesProvider` already handles FMP. Study rendering and prompt-structuring patterns only.

### 8.4  Pattern D — `graph/` reflection

| File | What to focus on |
|---|---|
| `graph/trading_graph.py` (reflection section) | How the reflection step is triggered after a trade outcome is known. What data it reads (signal, decision, actual outcome). What it writes back. How the next run queries prior memory. |

> 🚫 **Ignore:** Their BM25 vector retrieval (your reflection is SQL-based). Their lesson-summary LLM call (your reflection is deterministic beat/miss counts, not narrative).

---

## 9  Quick Reference

### 9.1  Files to create

| New file | Pattern | Purpose | Status |
|---|---|---|---|
| `services/llm_client.py` | B | `LLMClientFactory` — deep/quick tier routing | ✅ Done |
| `services/estimates_renderer.py` | C | `EstimatesRenderer` — structured narrative for LLM prompts | TODO |
| `models/debate.py` (or extend `signals.py`) | A | `DebateRound`, `DebateVerdict`, `DebateResult` | TODO |

### 9.2  Files to modify

| Existing file | Pattern(s) | Change summary | Status |
|---|---|---|---|
| `config.py` | A, B | Add `signal_debate_*`, `llm_provider`, `llm_quick_model`, `llm_deep_model` keys | ✅ B done; A TODO |
| `models/sentiment.py` | B | Add `provider` field to `SentimentResult` | ✅ Done |
| `models/signals.py` | A, B | Add `debate_result` to `TradeSignal`; add `model_id`, `provider` to `TradeSignal` | ✅ B done; A TODO |
| `providers/sentiment/claude.py` | B | Accept `LLMClient`; propagate `model_id`/`provider` to results | ✅ Done |
| `providers/__init__.py` | B | Inject `LLMClientFactory.deep` into `ClaudeSentimentProvider` | ✅ Done |
| `agents/sentiment_analyst.py` | B | Route calls to quick vs deep client; propagate `model_id` to output | TODO |
| `agents/signal_generator.py` | A | Add `_debate_signal()` method and wire into `run()` | TODO |
| `services/database.py` | D | Add `EarningsOutcomeRow` ORM model | TODO |
| `services/confidence_scorer.py` | C | Use `EstimatesRenderer` in `_score_surprise()` | TODO |
| `agents/earnings_calendar.py` | D | Use `load_historical_outcomes()` for beat rate | TODO |
| `Stage1Repository` | D | Add `load_historical_outcomes()` and `record_outcome()` | TODO |

### 9.3  Rules

- **Never add TradingAgents to `pyproject.toml`** as a dependency.
- **Never copy Python files** from the TradingAgents repo into news-trade.
- **Never run the debate on every signal** — only above `signal_debate_threshold`.
- **Never write runnable code in chat** — pseudocode in chat, implementation via Claude Code.
- **Never add a second LLM provider** until there is a concrete trigger (outage resilience, cost arbitrage, or research comparison). The protocol boundary is ready; the implementation waits for a real need.
- **Always record `model_id` and `provider`** on every `SentimentResult` and `TradeSignal` — these fields must be populated from `LLMResponse`, never hardcoded.
