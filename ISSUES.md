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

## Issue 5: Implement NewsIngestorAgent end-to-end

**Priority:** P1 — Should-have
**Depends on:** None (ORM and async event bus are already implemented)
**Labels:** `agent`, `feature`

Implement the first agent fully to prove the architecture works:

- `_fetch_benzinga()` — call Benzinga News API with `httpx`, parse into `NewsEvent`
- `_fetch_polygon()` — same for Polygon.io reference news
- `_is_duplicate()` — check SQLite (via ORM) for existing `event_id`
- `_matches_watchlist()` — filter tickers against `settings.watchlist`
- `run()` — orchestrate the above, publish to event bus, return state

---

## Issue 7: Add `docker-compose.yml` for Redis

**Priority:** P2 — Nice-to-have
**Depends on:** None
**Labels:** `infrastructure`, `dx`

Add a `docker-compose.yml` with a Redis 7 service so contributors can
`docker compose up -d` instead of installing Redis manually.

```yaml
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
```

---

## Issue 8: Add `py.typed` marker

**Priority:** P2 — Nice-to-have
**Depends on:** None
**Labels:** `typing`

Add `src/news_trade/py.typed` (empty marker file) so downstream
consumers get type-checking support per PEP 561.

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
#3 MarketSnapshot ─────► #4 Tests ✅
#4 Tests ✅ ───────────► #9 CI ✅
#5 NewsIngestorAgent    (no remaining deps — ORM and event bus done)
#7 docker-compose       (independent)
#8 py.typed             (independent)
```

## Remaining implementation order

1. **#8** py.typed marker ← trivial
2. **#7** docker-compose.yml ← trivial
3. **#5** NewsIngestorAgent ← no remaining deps
