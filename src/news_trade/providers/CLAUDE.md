# providers/ — Data Provider Abstraction Layer

Providers are defined as **Protocols** (`base.py`), enabling structural subtyping.
Concrete implementations do not inherit from a base class — they just satisfy the Protocol.
Factory functions in `__init__.py` select the implementation from `Settings`.

---

## Protocol Definitions (`base.py`)

```python
class NewsProvider(Protocol):
    @property
    def name(self) -> str: ...
    async def fetch(self, tickers: list[str], since: datetime | None = None) -> list[NewsEvent]: ...

class MarketDataProvider(Protocol):
    @property
    def name(self) -> str: ...
    async def get_snapshot(self, ticker: str) -> MarketSnapshot: ...
    async def get_snapshots(self, tickers: list[str]) -> dict[str, MarketSnapshot]: ...

class SentimentProvider(Protocol):
    @property
    def name(self) -> str: ...
    async def analyse(self, event: NewsEvent) -> SentimentResult: ...
    async def analyse_batch(self, events: list[NewsEvent]) -> list[SentimentResult]: ...

class CalendarProvider(Protocol):
    @property
    def name(self) -> str: ...
    async def get_upcoming_earnings(
        self, tickers: list[str], from_date: date, to_date: date
    ) -> list[EarningsCalendarEntry]: ...

class EstimatesProvider(Protocol):
    @property
    def name(self) -> str: ...
    async def get_historical_beat_rate(
        self, ticker: str, lookback: int = 8
    ) -> float | None: ...
```

All provider methods are `async`. Test mocks implement only the methods the test needs
— no inheritance required.

---

## Current Implementations

| Category | File | Class | Tier | Requires |
|---|---|---|---|---|
| News | `news/rss.py` | `RSSNewsProvider` | Free | — |
| News | `news/benzinga.py` | `BenzingaNewsProvider` | Premium | `BENZINGA_API_KEY` |
| Market | `market/yfinance.py` | `YFinanceMarketProvider` | Free | — |
| Market | `market/massive_free.py` | `MassiveFreeMarketProvider` | Free tier | `MASSIVE_API_KEY` |
| Market | `market/massive_paid.py` | `MassivePaidMarketProvider` | Starter+ | `MASSIVE_API_KEY` |
| Market | `market/finnhub.py` | `FinnhubMarketDataProvider` | Free | `FINNHUB_API_KEY` |
| Sentiment | `sentiment/claude.py` | `ClaudeSentimentProvider` | Paid | `ANTHROPIC_API_KEY` |
| Sentiment | `sentiment/keyword.py` | `KeywordSentimentProvider` | Free | — |
| Calendar | `calendar/finnhub.py` | `FinnhubCalendarProvider` | Free | `FINNHUB_API_KEY` |
| Calendar | `calendar/fmp.py` | `FMPCalendarProvider` | Free (250 req/day) | `FMP_API_KEY` |
| Calendar | `calendar/yfinance_provider.py` | `YFinanceCalendarProvider` | Free | — |
| Estimates | `estimates/fmp.py` | `FMPEstimatesProvider` | Free (250 req/day) | `FMP_API_KEY` |

`ClaudeSentimentProvider` injects a full `LLMClientFactory` and selects the LLM tier per
event inside `_select_client()`:

| Event types | Tier | Reason |
|---|---|---|
| `EARN_PRE`, `EARN_BEAT`, `EARN_MISS`, `EARNINGS` | `.deep` (Sonnet) | High-stakes; quality affects capital allocation |
| All other types (M&A, guidance, macro, analyst, etc.) | `.quick` (Haiku) | Simple label + score; ~25× cheaper |

`EARN_PRE` events also receive a specialised system prompt (`_EARN_PRE_SYSTEM_PROMPT`) that
instructs the model to reason from pre-announcement signals. When `state["estimates"]`
contains an `EstimatesData` entry for the ticker, `analyse_batch()` appends the
`EstimatesRenderer.render()` narrative block to the user message, giving Claude
pre-computed analyst context (EPS consensus, estimate dispersion, historical beat rate)
rather than inferring from headline text alone. `self._llm` always points to `.deep` so
budget cost estimates remain conservative even for quick-tier calls.
Enforces a daily budget cap (`settings.claude_daily_budget_usd`); returns a neutral
`SentimentResult` (not an exception) when the cap is hit.

---

## Factory Functions (`__init__.py`)

```python
get_news_provider(settings)         # → RSSNewsProvider | BenzingaNewsProvider
get_market_data_provider(settings)  # → YFinance | MassiveFree | MassivePaid
get_sentiment_provider(settings)    # → ClaudeSentimentProvider | KeywordSentimentProvider
get_calendar_provider(settings)     # → FinnhubCalendarProvider (if FINNHUB_API_KEY) | FMPCalendarProvider (if FMP_API_KEY) | YFinanceCalendarProvider
get_estimates_provider(settings)    # → FMPEstimatesProvider (if FMP_API_KEY) | None
```

`get_calendar_provider` priority: **Finnhub → FMP → yfinance**.
Finnhub is preferred because its free tier supports broad date-range scans (no ticker
filter). FMP is retained as a fallback and remains the sole source for EPS beat rates
(`get_estimates_provider`). `YFinanceCalendarProvider` is the last resort — per-ticker
only, no broad scan.

**Broad market scan:** both `FinnhubCalendarProvider` and `FMPCalendarProvider` accept an
empty `tickers` list to return all companies reporting in the date window.
`FMPCalendarProvider` requires a paid plan for this; `FinnhubCalendarProvider` supports it
on the free tier.
`YFinanceCalendarProvider` requires explicit tickers and cannot do a broad scan
— always pass at least a fallback list when using yfinance.

`get_estimates_provider` returns `FMPEstimatesProvider` when `settings.fmp_api_key` is set,
or `None` when no key is present. Callers must handle `None` gracefully (fall back to
`earn_default_beat_rate`).

Selection is driven by `settings.news_provider`, `settings.market_data_provider`,
and `settings.sentiment_provider` (enum fields in `config.py`).

---

## Adding a New Provider

1. **Implement the Protocol** in a new file under the relevant sub-package.
   Satisfy every method signature — no base class needed.

2. **Add an enum value** to the relevant `*ProviderType` enum in `config.py`.

3. **Wire into the factory** in `providers/__init__.py` — add a `case` to the `match`
   statement in the relevant `get_*_provider()` function.

4. **Add Protocol compliance tests** in `tests/test_providers.py`.
   Use `isinstance(provider, SentimentProvider)` — it works because `SentimentProvider`
   is decorated with `@runtime_checkable`.

---

## Test Mocks

Provider mocks need only implement the methods called by the code under test:

```python
mock_provider = AsyncMock()
mock_provider.fetch.return_value = [some_event]
mock_provider.name = "mock"
```

No inheritance from the Protocol class required — Python's structural subtyping
means the mock satisfies the Protocol at runtime as long as it has the right methods.
