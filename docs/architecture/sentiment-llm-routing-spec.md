# Sentiment LLM Routing Spec

> **Status:** Ready to implement
> **Depends on:** Pattern B (LLMClientFactory ✅), Pattern A (SignalGeneratorAgent ✅)
> **Branch:** create `claude/sentiment-llm-routing-<id>`
> **Repo:** `fra-l/news-trade`

---

## Context

This spec closes the two remaining actionable items from `tradingagents-integration-spec.md`
now that all four patterns (A–D) are complete:

- **Pattern B §3.4 steps 6–8** (explicitly deferred until `SignalGeneratorAgent` was done):
  audit `ClaudeSentimentProvider` for per-call quick/deep model routing.
- **Pattern C §4.4 item 10** (depends on Pattern A): use `EstimatesRenderer` narrative in
  the EARN_PRE sentiment prompt.

---

## Current State

### What exists

| Component | File | Current behaviour |
|---|---|---|
| `ClaudeSentimentProvider` | `providers/sentiment/claude.py` | Accepts a single `LLMClient` (always `deep`). Uses one prompt for all event types. |
| Provider factory | `providers/__init__.py` | Injects `LLMClientFactory.deep` into `ClaudeSentimentProvider`. |
| `EstimatesRenderer` | `services/estimates_renderer.py` | Produces structured narrative; used only by `ConfidenceScorer._score_surprise()` (deterministic path). `render()` is never passed to any LLM. |
| `SentimentAnalystAgent` | `agents/sentiment_analyst.py` | Delegates entirely to `self._provider.analyse_batch()`. No LLM client. |

### The cost problem

`ClaudeSentimentProvider` calls Sonnet (`deep`) for every event regardless of type. An
M&A rumour headline and a complex EARN_PRE pre-announcement synthesis both hit the same
expensive model. The split:

| Task | Ideal tier | Reason |
|---|---|---|
| Non-EARN events (M&A, regulatory, guidance, macro, analyst rating) | `quick` (Haiku) | Simple label + score; Haiku is ~25× cheaper per token with no meaningful quality loss |
| EARN_PRE | `deep` (Sonnet) | Multi-factor synthesis; quality directly affects capital allocation |
| EARN_BEAT / EARN_MISS | `deep` (Sonnet) | Post-announcement confirmation/reversal; high stakes |
| EARN_MIXED | `quick` (Haiku) | Always blocked by `ConfidenceScorer` gate (1.01); result discarded anyway |

### The prompt problem

For EARN_PRE events the current prompt sends only `headline + summary + tickers`. The
`EstimatesRenderer` already produces a structured narrative block (analyst estimates,
dispersion, historical beat rate) but it is never injected into any LLM call.
Adding it to the EARN_PRE prompt gives the model pre-computed context instead of asking
it to infer from a headline alone.

---

## Architecture Decisions

### Decision 1: inject `LLMClientFactory` into `ClaudeSentimentProvider`, not `SentimentAnalystAgent`

The routing decision (which tier to use) depends on `event.event_type`, which is known
only inside the provider at call time. `SentimentAnalystAgent` delegates to the provider
and has no per-event visibility. Therefore the factory is injected into
`ClaudeSentimentProvider` directly — the agent remains unchanged.

```
Before:  providers/__init__.py → LLMClientFactory.deep → ClaudeSentimentProvider
After:   providers/__init__.py → LLMClientFactory       → ClaudeSentimentProvider
                                                             ↓ (selects tier per event)
                                                           .quick  or  .deep
```

### Decision 2: `EstimatesRenderer` in EARN_PRE prompt, no pipeline change required

`EstimatesData` is not currently in `PipelineState` — it belongs to a future
`EarningsCalendarAgent`. However, `ClaudeSentimentProvider` can still benefit from
`EstimatesRenderer` immediately by passing an **empty/stub** `EstimatesData` when
no real data is available, or more practically by having a dedicated EARN_PRE system
prompt that asks the LLM to reason from the headline and event context
(richer instructions, no estimates block yet).

The spec defines a **two-phase approach**:
- **Phase 1 (this PR):** Inject a richer EARN_PRE system prompt into the deep-model call.
  The prompt instructs the model to treat the event as pre-announcement and to assess
  expected surprise direction. No `EstimatesData` required.
- **Phase 2 (future PR):** When `EarningsCalendarAgent` lands and populates `estimates`
  in state, extend `ClaudeSentimentProvider.analyse_batch()` to accept optional
  `EstimatesData` per ticker and inject the `EstimatesRenderer.render()` block.

---

## Implementation

### 1. `providers/sentiment/claude.py` — inject factory, add tier routing, add EARN_PRE prompt

#### Constructor change

```python
# Before
def __init__(self, llm: LLMClient, daily_budget: float = 2.00) -> None:
    self._llm = llm

# After
def __init__(self, llm: LLMClientFactory, daily_budget: float = 2.00) -> None:
    self._factory = llm
    # keep self._llm pointing to deep for budget tracking (cost estimate uses deep rates)
    self._llm = llm.deep
```

Note: `self._llm` is used for budget tracking (`_INPUT_COST_PER_TOKEN`,
`_OUTPUT_COST_PER_TOKEN`, `_record_usage`, `model_id`/`provider` on neutral results).
Keep it pointing to the `deep` client so cost estimates remain conservative.

#### New EARN_PRE system prompt

```python
_EARN_PRE_SYSTEM_PROMPT = """\
You are a financial news sentiment analyst specialising in pre-earnings analysis.
The event you are analysing is a PRE-ANNOUNCEMENT — a report date is upcoming but
earnings have not yet been released.

Assess sentiment based on:
- Analyst estimate revisions and consensus trend in the headline/summary
- Any forward guidance signals
- Historical beat/miss reputation implied by the text
- Market positioning language (e.g. "raised guidance", "cautious outlook")

Return a JSON array where each element has these exact keys:
  ticker        (string)  — the stock symbol
  label         (string)  — one of: VERY_BULLISH, BULLISH, NEUTRAL, BEARISH, VERY_BEARISH
  score         (float)   — sentiment score from -1.0 (very bearish) to +1.0 (very bullish)
  confidence    (float)   — confidence from 0.0 to 1.0
  reasoning     (string)  — one-sentence explanation citing the specific signal

Return ONLY the JSON array with no surrounding text or markdown fences.
"""
```

#### Tier-selection helper

```python
_EARN_DEEP_TYPES = frozenset({
    "earn_pre", "earn_beat", "earn_miss",
    # coarse fallback
    "earnings",
})

def _select_client(self, event: NewsEvent) -> LLMClient:
    """Return deep for high-stakes earnings events, quick for everything else."""
    event_type_str = str(event.event_type).lower()
    if event_type_str in _EARN_DEEP_TYPES:
        return self._factory.deep
    return self._factory.quick
```

#### Updated `_call_claude()`

```python
async def _call_claude(self, event: NewsEvent) -> list[SentimentResult]:
    client = self._select_client(event)

    # Select system prompt based on event type
    event_type_str = str(event.event_type).lower()
    system_prompt = (
        _EARN_PRE_SYSTEM_PROMPT
        if event_type_str == "earn_pre"
        else _SYSTEM_PROMPT
    )

    tickers_str = ", ".join(event.tickers) if event.tickers else "unspecified"
    user_message = (
        f"Headline: {event.headline}\n"
        f"Summary: {event.summary}\n"
        f"Tickers: {tickers_str}\n"
        f"Event type: {event.event_type}"
    )

    try:
        response = await client.invoke(user_message, system=system_prompt)
    except Exception as exc:
        _logger.error("Claude API error for event %s: %s", event.event_id, exc)
        return [_neutral_result(event, self._llm.model_id, self._llm.provider)]

    self._record_usage(response.input_tokens, response.output_tokens)
    return _parse_response(
        response.content, event, response.model_id, response.provider
    )
```

Key changes:
- `client` is selected per event via `_select_client()`
- `system_prompt` is selected per event type
- `_record_usage` still uses `self._llm` (deep) rates — conservative overestimate for quick calls

### 2. `providers/__init__.py` — inject factory instead of `.deep`

```python
# Before
from news_trade.services.llm_client import LLMClientFactory

def get_sentiment_provider(settings: Settings) -> SentimentProvider:
    match settings.sentiment_provider:
        case SentimentProviderType.CLAUDE:
            return ClaudeSentimentProvider(
                llm=LLMClientFactory(settings).deep,    # ← was .deep
                daily_budget=settings.claude_daily_budget_usd,
            )
        case SentimentProviderType.KEYWORD:
            return KeywordSentimentProvider()

# After
def get_sentiment_provider(settings: Settings) -> SentimentProvider:
    match settings.sentiment_provider:
        case SentimentProviderType.CLAUDE:
            return ClaudeSentimentProvider(
                llm=LLMClientFactory(settings),         # ← full factory
                daily_budget=settings.claude_daily_budget_usd,
            )
        case SentimentProviderType.KEYWORD:
            return KeywordSentimentProvider()
```

### 3. Type annotation fix in `ClaudeSentimentProvider`

Import `LLMClientFactory` in `claude.py`:

```python
from news_trade.services.llm_client import LLMClient, LLMClientFactory
```

`self._llm` stays typed as `LLMClient` (points to `factory.deep`).
`self._factory` is typed as `LLMClientFactory`.

### 4. No changes to `SentimentAnalystAgent`, `ConfidenceScorer`, or `EstimatesRenderer`

The agent, scorer, and renderer are unchanged. The routing and prompt improvement are
entirely encapsulated within `ClaudeSentimentProvider`.

---

## Tests

### Modify `tests/test_providers.py`

The existing `ClaudeSentimentProvider` tests construct it with a mock `LLMClient`. They
need updating to pass a mock `LLMClientFactory` instead.

```python
def _make_provider(
    mock_llm_response: str = "[]",
    daily_budget: float = 10.0,
) -> ClaudeSentimentProvider:
    """Helper: build a ClaudeSentimentProvider with a mocked LLMClientFactory."""
    from unittest.mock import AsyncMock, MagicMock
    from news_trade.services.llm_client import LLMResponse

    response = LLMResponse(
        content=mock_llm_response,
        model_id="claude-haiku-4-5-20251001",
        provider="anthropic",
        input_tokens=10,
        output_tokens=5,
    )
    mock_client = AsyncMock()
    mock_client.invoke = AsyncMock(return_value=response)
    mock_client.model_id = "claude-haiku-4-5-20251001"
    mock_client.provider = "anthropic"

    mock_factory = MagicMock()
    mock_factory.quick = mock_client
    mock_factory.deep = mock_client

    return ClaudeSentimentProvider(llm=mock_factory, daily_budget=daily_budget)
```

### New test class: `TestClaudeProviderTierRouting`

```python
class TestClaudeProviderTierRouting:
    """Verify correct LLM tier is selected per event type."""

    def setup_method(self) -> None:
        # Build provider with separate quick/deep mocks
        ...

    async def test_earn_pre_uses_deep(self): ...
    async def test_earn_beat_uses_deep(self): ...
    async def test_earn_miss_uses_deep(self): ...
    async def test_ma_target_uses_quick(self): ...
    async def test_guidance_uses_quick(self): ...
    async def test_earn_mixed_uses_quick(self): ...
    async def test_other_uses_quick(self): ...

class TestEarnPrePrompt:
    """Verify EARN_PRE events use the specialised system prompt."""

    async def test_earn_pre_receives_earn_pre_system_prompt(self): ...
    async def test_non_earn_pre_receives_standard_system_prompt(self): ...
```

### Verify `model_id` provenance on results

Add assertions that `SentimentResult.model_id` reflects the actual model used (quick
model for non-EARN events, deep model for EARN events):

```python
async def test_model_id_reflects_quick_model_for_non_earn(self): ...
async def test_model_id_reflects_deep_model_for_earn_pre(self): ...
```

---

## Verification

```bash
uv run ruff check src/ tests/        # must be clean
uv run mypy src/                     # strict — no new errors
uv run python -m pytest              # full suite must stay green
```

Specifically check:
- All existing `ClaudeSentimentProvider` tests still pass after the mock update
- New routing tests pass
- `test_pipeline.py` still passes (pipeline wires the factory correctly)

---

## Files to Modify

| File | Change |
|---|---|
| `src/news_trade/providers/sentiment/claude.py` | Inject `LLMClientFactory`; add `_EARN_PRE_SYSTEM_PROMPT`; add `_select_client()`; update `_call_claude()` |
| `src/news_trade/providers/__init__.py` | Pass full `LLMClientFactory` instead of `.deep` |
| `tests/test_providers.py` | Update `ClaudeSentimentProvider` mock helper; add `TestClaudeProviderTierRouting`; add `TestEarnPrePrompt` |

No other files require changes.

---

## Out of Scope for This PR

- Injecting `EstimatesRenderer.render()` output into the EARN_PRE prompt — blocked until
  `EarningsCalendarAgent` fetches `EstimatesData` and makes it available in `PipelineState`
- `OrchestratorAgent` wiring — `OrchestratorAgent` is unused (`graph/pipeline.py` builds
  the graph directly); no change needed
- Adding a second LLM provider (OpenAI, Gemini) — explicitly deferred per spec §6.1 until
  there is a concrete trigger (outage resilience, cost arbitrage, or research comparison)
