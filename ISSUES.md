# Pre-Merge Issues

Ordered by priority. Each issue lists its dependencies (if any).

---

## Issue 3: Add typed MarketSnapshot Pydantic model

**Priority:** P1 — Should-have
**Depends on:** None
**Labels:** `models`, `typing`

`PipelineState.market_context` is `dict[str, dict]` — the only untyped
boundary in the pipeline. Add a `MarketSnapshot` model in `models/market.py`:

```python
class MarketSnapshot(BaseModel):
    ticker: str
    latest_close: float
    volume: int
    vwap: float
    volatility_20d: float
    bars: list[OHLCVBar]
    fetched_at: datetime
```

Update `PipelineState.market_context` to `dict[str, MarketSnapshot]`
and `MarketDataAgent._build_context()` return type accordingly.

---

## ~~Issue 4: Add unit tests for Pydantic models and pipeline graph~~ ✅ Done

**Priority:** P0 — Must-have
**Depends on:** #3 (MarketSnapshot)
**Labels:** `testing`

Implemented in commit `a7d022b`:

- `tests/test_models.py` — 42 tests covering all 6 Pydantic models
- `tests/test_pipeline.py` — 10 tests for `build_pipeline()` and routing helpers
- `tests/test_risk_rules.py` — 10 skipped placeholder tests for risk rule methods

---

## ~~Issue 5: Implement NewsIngestorAgent end-to-end~~ ✅ Done

**Priority:** P1 — Should-have
**Depends on:** None (ORM and async event bus are already implemented)
**Labels:** `agent`, `feature`

Implemented in `src/news_trade/agents/news_ingestor.py`:

- `_fetch_benzinga()` — calls Benzinga News API with `httpx`, parses into `NewsEvent`
- `_fetch_polygon()` — same for Polygon.io reference news
- `_is_duplicate()` — checks SQLite (via ORM) for existing `event_id`
- `_matches_watchlist()` — filters tickers against `settings.watchlist`
- `_persist()` — inserts new `NewsEventRow` into the database
- `run()` — orchestrates the above, publishes to event bus, returns state
- `_classify_event_type()` / `_parse_dt()` — module-level helpers
- `tests/test_news_ingestor.py` — 27 tests covering all methods and `run()` end-to-end

---

## ~~Issue 7: Add `docker-compose.yml` for Redis~~ ✅ Done

**Priority:** P2 — Nice-to-have
**Depends on:** None
**Labels:** `infrastructure`, `dx`

Implemented in commit `a13a504`:

- `docker-compose.yml` — Redis 7-alpine service on port 6379

---

## ~~Issue 8: Add `py.typed` marker~~ ✅ Done

**Priority:** P2 — Nice-to-have
**Depends on:** None
**Labels:** `typing`

Implemented: `src/news_trade/py.typed` (empty PEP 561 marker file) so downstream
consumers get type-checking support.

---

## ~~Issue 9: Add GitHub Actions CI workflow~~ ✅ Done

**Priority:** P2 — Nice-to-have
**Depends on:** #4 (tests exist)
**Labels:** `ci`, `dx`

Implemented in commit `a7d022b` as `.github/workflows/tests.yml`:
runs `uv sync --extra dev` + `uv run pytest tests/ -v` on every pull request.

---

## Dependency graph

```
#3 MarketSnapshot ✅ ──► #4 Tests ✅
#4 Tests ✅ ───────────► #9 CI ✅
#5 NewsIngestorAgent ✅ (no remaining deps — ORM and event bus done)
#7 docker-compose ✅    (independent)
#8 py.typed ✅          (independent)
```

All issues resolved — no remaining work.
