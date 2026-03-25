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
| Market | `market/polygon_free.py` | `PolygonFreeMarketProvider` | Free tier | `POLYGON_API_KEY` |
| Market | `market/polygon_paid.py` | `PolygonPaidMarketProvider` | Starter+ | `POLYGON_API_KEY` |
| Sentiment | `sentiment/claude.py` | `ClaudeSentimentProvider` | Paid | `ANTHROPIC_API_KEY` |
| Sentiment | `sentiment/keyword.py` | `KeywordSentimentProvider` | Free | — |

`ClaudeSentimentProvider` injects `LLMClientFactory.deep` and enforces a daily budget cap
(`settings.claude_daily_budget_usd`). When the cap is hit it returns a neutral `SentimentResult`
rather than raising.

---

## Factory Functions (`__init__.py`)

```python
get_news_provider(settings)         # → RSSNewsProvider | BenzingaNewsProvider
get_market_data_provider(settings)  # → YFinance | PolygonFree | PolygonPaid
get_sentiment_provider(settings)    # → ClaudeSentimentProvider | KeywordSentimentProvider
```

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
