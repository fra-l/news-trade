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

## Issue 4: Add unit tests for Pydantic models and pipeline graph

**Priority:** P0 — Must-have
**Depends on:** #3 (MarketSnapshot)
**Labels:** `testing`

`tests/` is empty. Add at minimum:

- `tests/test_models.py` — validate serialization round-trips, field
  constraints (score bounds, enum values), and optional-field defaults
  for all 5+ Pydantic models.
- `tests/test_pipeline.py` — verify `build_pipeline()` compiles without
  error and the graph has the expected node names and conditional edges.
- `tests/test_risk_rules.py` — stub for risk manager rule unit tests
  (blocked until agent is implemented, but create the file with TODOs).

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

## Issue 9: Add GitHub Actions CI workflow

**Priority:** P2 — Nice-to-have
**Depends on:** #4 (tests exist)
**Labels:** `ci`, `dx`

Add `.github/workflows/ci.yml` that runs on push/PR:

1. `uv sync --group dev`
2. `uv run ruff check src/ tests/`
3. `uv run mypy src/`
4. `uv run pytest`

Matrix over Python 3.11 and 3.12.

---

## Dependency graph

```
#3 MarketSnapshot ─────► #4 Tests
#4 Tests ──────────────► #9 CI
#5 NewsIngestorAgent    (no remaining deps — ORM and event bus done)
#7 docker-compose       (independent)
#8 py.typed             (independent)
```

## Suggested implementation order

1. **#3** MarketSnapshot model ← no deps, small
2. **#8** py.typed marker ← trivial
3. **#7** docker-compose.yml ← trivial
4. **#4** Tests ← needs #3
5. **#5** NewsIngestorAgent ← no remaining deps
6. **#9** CI workflow ← needs #4
