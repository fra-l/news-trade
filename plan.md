# Plan: Earnings Thesis Debate + Configurable Pre-Earnings Horizon



## Context



The pipeline currently generates earnings signals purely from a historical beat rate, with

direction hardcoded to `beat_rate >= 0.60 → LONG`. At startup on a fresh DB, no EARN_PRE

signals are produced at all because `EarningsCalendarAgent` only runs as a cron at 07:00 ET

and `EarningsTickerNode` depends on the DB rows it writes.



Three connected changes are planned:



1. **Single `EARN_PRE_HORIZON_DAYS` config parameter** — unify three scattered hardcoded

   horizons (5 days in `EarningsCalendarAgent`, 7 days in `EarningsTickerNode`, 14 days in

   `StartupSelector`) into one setting. Default 14.



2. **Run `EarningsCalendarAgent` once at startup** — seed the DB before the first pipeline

   cycle so EARN_PRE signals are available from cycle 1.



3. **Bull/Bear thesis debate in `_handle_earn_pre()`** — replace beat-rate-only direction

   with a three-LLM debate: Bull LLM + Bear LLM (parallel, quick model) → Synthesis LLM

   (deep model) → `LONG / SHORT / NEUTRAL` verdict + conviction. Beat rate becomes

   evidence passed to both debaters, not the sole decision criterion. The debate re-runs

   on subsequent cycles only when new (non-ephemeral) news has arrived for the ticker,

   keeping API cost proportional to actual news volume.



---



## Phases



| Phase | Branch | What it delivers |

|---|---|---|

| **Phase 1** | `claude/earn-pre-horizon-startup-fix` | `EARN_PRE_HORIZON_DAYS` setting + startup calendar seed |

| **Phase 2** | `claude/earn-thesis-debate` | Bull/Bear thesis debate in `_handle_earn_pre()` |



Phases are independent commits on separate branches. Phase 1 is a prerequisite for Phase 2

(Phase 2 tests assume the new config fields from Phase 1).



---



## Phase 1 — Horizon Parameter + Startup Calendar Seed



### Files changed



| File | Change |

|---|---|

| `src/news_trade/config.py` | Add `earn_pre_horizon_days: int = 14` and `earn_thesis_flip_conviction_threshold: float = 0.65` |

| `.env.example` | Document both new settings |

| `src/news_trade/agents/earnings_calendar.py` | Remove `_SCAN_DAYS_AHEAD = 5`; use `settings.earn_pre_horizon_days`; replace `is_actionable` filter with `1 <= days_until_report <= settings.earn_pre_horizon_days` |

| `src/news_trade/agents/earnings_ticker.py` | Remove `_HORIZON_DAYS = 7`; use `settings.earn_pre_horizon_days` |

| `src/news_trade/main.py` | Stop importing `SCAN_DAYS`; use `settings.earn_pre_horizon_days`; add startup `earnings_agent.run({})` call |

| `tests/test_earnings_calendar.py` | Update window-filter tests for new 1-to-horizon range |

| `CLAUDE.md` | Add config table rows |

| `src/news_trade/agents/CLAUDE.md` | Update `EarningsCalendarAgent` and `EarningsTickerNode` sections |



### Implementation



#### 1. `config.py` — insert after `earn_default_beat_rate`



```python

earn_pre_horizon_days: int = Field(

    default=14,

    description=(

        "Look-ahead window (calendar days) for the pre-earnings pipeline. "

        "Controls EarningsCalendarAgent scan range, EarningsTickerNode filter, "

        "and StartupSelector scan range. Default 14 matches the original "

        "StartupSelector SCAN_DAYS constant."

    ),

)

earn_thesis_flip_conviction_threshold: float = Field(

    default=0.65,

    description=(

        "Minimum thesis-debate conviction required to flip (REVERSE) an existing "

        "open Stage 1 EARN_PRE position when a new debate contradicts its direction."

    ),

)

```



#### 2. `earnings_calendar.py`



- Delete `_SCAN_DAYS_AHEAD = 5` (line 26).

- In `run()`: `to_date = today + timedelta(days=self.settings.earn_pre_horizon_days)`

- Replace `actionable = [e for e in entries if e.is_actionable]` with:

  ```python

  horizon = self.settings.earn_pre_horizon_days

  actionable = [e for e in entries if 1 <= e.days_until_report <= horizon]

  ```

  (`EarningsCalendarEntry.is_actionable` property (2–5 days) is NOT modified — it stays

  for external consumers and its own model tests.)



#### 3. `earnings_ticker.py`



- Delete `_HORIZON_DAYS = 7` (line 36).

- In `_gather_active_events()`:

  ```python

  # Before:

  if not (1 <= days_until <= _HORIZON_DAYS):

  # After:

  if not (1 <= days_until <= self.settings.earn_pre_horizon_days):

  ```



#### 4. `main.py`



Remove `SCAN_DAYS` from the import line:

```python

# Before:

from news_trade.cli.startup_selector import SCAN_DAYS, StartupSelector

# After:

from news_trade.cli.startup_selector import StartupSelector

```



Replace hardcoded `SCAN_DAYS` with `settings.earn_pre_horizon_days` in the startup block:

```python

scan_days = settings.earn_pre_horizon_days

logger.info("Fetching small-cap earnings candidates for the next %d days …", scan_days)

candidates = await selector.fetch_candidates(today, today + timedelta(days=scan_days))

```



After `scheduler.start()` (before the main loop), add the startup seed call:

```python

logger.info("Seeding earnings calendar DB at startup …")

try:

    _cal_result = await earnings_agent.run({})

    logger.info(

        "Startup calendar seed: %d EARN_PRE event(s) published",

        len(_cal_result.get("news_events", [])),

    )

except Exception as _exc:

    logger.warning("Startup calendar seed failed (non-fatal): %s", _exc)

```



#### 5. `test_earnings_calendar.py` — update window-filter tests



Delete `test_entry_at_1_day_not_emitted` and `test_entry_at_6_days_not_emitted`

(they tested the old 2–5 day `is_actionable` filter).



Add new tests with a settings fixture using `earn_pre_horizon_days=5`:

- `test_entry_at_0_days_not_emitted` — report today (0 days) → excluded (`>= 1` check)

- `test_entry_at_1_day_is_emitted` — 1 day out, within `[1, 5]` → included

- `test_entry_at_horizon_is_emitted` — exactly 5 days out → included

- `test_entry_beyond_horizon_not_emitted` — 6 days out, `earn_pre_horizon_days=5` → excluded



---



## Phase 2 — Bull/Bear Thesis Debate in `_handle_earn_pre()`



### Files changed



| File | Change |

|---|---|

| `src/news_trade/agents/signal_generator.py` | New schema, prompts, `_run_thesis_debate()` method; rewrite `_handle_earn_pre()`; make `_build_signal()` async |

| `tests/test_signal_generator.py` | Update all `TestHandleEarnPre` tests to async + mock LLM; add `TestRunThesisDebate` (6 tests) |

| `src/news_trade/agents/CLAUDE.md` | Document new debate flow |



### Implementation



#### 1. New `_ThesisVerdictSchema` — add after `_DebateVerdictSchema` (line ~68)



```python

class _ThesisVerdictSchema(BaseModel):

    """Structured output for the EARN_PRE quarterly thesis debate."""

    direction: Literal["LONG", "SHORT", "NEUTRAL"]

    conviction: Annotated[float, Field(ge=0.0, le=1.0)]

    reasoning: str

```



Add `from typing import Annotated, Literal` to the imports block.



#### 2. New module-level prompt helpers — add after `_build_synthesis_prompt`



```python

def _build_thesis_bull_prompt(ticker, fiscal_quarter, days_until_report,

                               beat_rate, beat_rate_source, eps_estimate,

                               news_summaries: list[str]) -> str: ...



def _build_thesis_bear_prompt(ticker, fiscal_quarter, days_until_report,

                               beat_rate, beat_rate_source, eps_estimate,

                               news_summaries: list[str]) -> str: ...



def _build_thesis_synthesis_prompt(ticker, fiscal_quarter, days_until_report,

                                    bull_arg: str, bear_arg: str) -> str: ...

```



Prompts frame the debate as: "company X reports in N days, here is beat rate + EPS

estimate + top-5 recent news summaries (decay-weighted). Bull: make strongest LONG case.

Bear: make strongest SHORT case. Synthesis: LONG / SHORT / NEUTRAL + conviction."



#### 3. New `_run_thesis_debate()` method on `SignalGeneratorAgent`



```python

async def _run_thesis_debate(

    self,

    ticker: str,

    days_until_report: int,

    fiscal_quarter: str,

    beat_rate: float,

    beat_rate_source: str,

    news_summaries: list[str],

    eps_estimate: float | None,

) -> _ThesisVerdictSchema:

    bull_resp, bear_resp = await asyncio.gather(

        self._llm.quick.invoke(_build_thesis_bull_prompt(...)),

        self._llm.quick.invoke(_build_thesis_bear_prompt(...)),

    )

    verdict_resp = await self._llm.deep.invoke(

        _build_thesis_synthesis_prompt(..., bull_resp.content, bear_resp.content),

        response_schema=_ThesisVerdictSchema,

    )

    return _ThesisVerdictSchema.model_validate(json.loads(verdict_resp.content))

```



#### 4. Make `_build_signal()` async and pass `group`



New signature:

```python

async def _build_signal(

    self,

    sentiment: SentimentResult,

    market_ctx: MarketSnapshot,

    event_lookup: dict[str, NewsEvent],

    estimates: dict[str, EstimatesData],

    group: list[SentimentResult] | None = None,

) -> TradeSignal | None:

```



In `run()`: `signal = await self._build_signal(agg, market_ctx, event_lookup, estimates, group)`



Dispatch in `_build_signal()`:

```python

case EventType.EARN_PRE:

    return await self._handle_earn_pre(

        sentiment, market_ctx, news_event, estimates, group or [], event_lookup

    )

```



#### 5. Rewrite `_handle_earn_pre()` — new async signature and logic



```python

async def _handle_earn_pre(

    self,

    sentiment: SentimentResult,

    market_ctx: MarketSnapshot,

    news_event: NewsEvent | None,

    estimates: dict[str, EstimatesData],

    group: list[SentimentResult],

    event_lookup: dict[str, NewsEvent] | None = None,

) -> TradeSignal | None:

```



**Decision tree (11 steps):**



1. `existing = self._stage1_repo.load_open(ticker)`

2. `new_news_present = any(not sr.event_id.startswith("ticker_earn_pre_") for sr in group)`

3. If `existing is not None` AND `not new_news_present` → `return None` (skip; cost guard)

4. Beat rate — same three-tier fallback as before; now used as **context**, not direction

5. Build `news_summaries`: top-5 `sr.reasoning` strings from `group`, sorted by `confidence × decay(event.published_at)` descending

6. `verdict = await self._run_thesis_debate(ticker, days_until_report, fiscal_quarter, beat_rate, beat_rate_source, news_summaries, eps_estimate)`

7. If `verdict.direction == "NEUTRAL"` → `return None`

8. If `existing is not None` AND `verdict.direction == existing.direction` → log "reaffirmed", `return None`

9. If `existing is not None` AND direction flipped AND `verdict.conviction > settings.earn_thesis_flip_conviction_threshold`:

   - `update_status(existing.id, Stage1Status.REVERSED)`

   - Return `TradeSignal(direction=CLOSE, conviction=1.0, passed_confidence_gate=True, stage1_id=existing.id)`

10. If `existing is not None` AND direction flipped AND conviction ≤ threshold → `return None`

11. No existing position: compute `size_pct` using beat_rate formula (clamped to `[_BEAT_RATE_MIN, _BEAT_RATE_MAX]`) × `max(verdict.conviction, 0.25)`; persist `OpenStage1Position`; emit `TradeSignal` with `debate_result` set



**Remove the beat_rate bounds check** (`if beat_rate < _BEAT_RATE_MIN or beat_rate > _BEAT_RATE_MAX: return None`) — direction now comes from the debate.



#### 6. Skip `_debate_signal()` for EARN_PRE signals



In `run()`:

```python

if signal.passed_confidence_gate:

    if signal.debate_result is None:

        signal = await self._debate_signal(signal)

    # EARN_PRE already ran _run_thesis_debate(); bypass gate debate

```



#### 7. `test_signal_generator.py` — test updates



**All `TestHandleEarnPre` tests** → convert to `async def`; add `await` to `_build_signal()` calls;

mock `agent._run_thesis_debate = AsyncMock(return_value=_ThesisVerdictSchema(...))`.



Tests that specifically tested beat_rate-driven direction (`test_earn_pre_short_for_low_beat_rate`,

`test_earn_pre_skip_when_beat_rate_below_min`, `test_earn_pre_skip_when_beat_rate_above_max`) are

**deleted** (that logic is removed). Beat-rate fallback tests are kept but assertions change:

they now verify `_run_thesis_debate` is called with the right `beat_rate` argument.



**Add `TestRunThesisDebate`** (6 tests):



| Test | Setup | Expected |

|---|---|---|

| `test_neutral_verdict_returns_none` | verdict=NEUTRAL | `None` |

| `test_long_verdict_opens_stage1` | no existing, verdict=LONG | signal with `stage1_id`; `persist()` called |

| `test_long_verdict_reaffirms_existing_long` | existing LONG, new news, verdict=LONG | `None`; no status update |

| `test_high_conviction_flip_emits_close` | existing LONG, new news, verdict=SHORT, conviction=0.80 > 0.65 | CLOSE signal; `update_status(REVERSED)` |

| `test_low_conviction_flip_keeps_position` | existing LONG, new news, verdict=SHORT, conviction=0.50 < 0.65 | `None`; no status update |

| `test_no_new_news_skips_debate` | existing LONG, group has only `ticker_earn_pre_*` events | `None`; `_run_thesis_debate` NOT called |



---



## Documentation File



As a first step (before Phase 1 code), create `ROADMAP.md` at the repo root documenting:

- What has already been implemented (decay-weighted aggregation on `claude/fix-multiple-orders-per-ticker-DHeBu`)

- Phase 1 scope and branch

- Phase 2 scope and branch

- The design rationale (discussion summary from this planning session)



---



## Verification



```bash

# After Phase 1:

uv run ruff check src/ tests/

uv run mypy src/

uv run pytest tests/test_earnings_calendar.py -v

uv run pytest  # full suite



# After Phase 2:

uv run ruff check src/ tests/

uv run mypy src/

uv run pytest tests/test_signal_generator.py -k "TestHandleEarnPre or TestRunThesisDebate" -v

uv run pytest  # full suite

```



Expected: all existing tests pass; new tests cover horizon configuration, startup

calendar seed, NEUTRAL/LONG/SHORT debate verdicts, thesis reaffirmation, direction

flip (high and low conviction), and the cost-guard skip when no new news.
