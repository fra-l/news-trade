# Issues & Phase Tracking

---

## ~~Issue 3: Add typed MarketSnapshot Pydantic model~~ ✅ Done

**Priority:** P1 — Should-have
**Depends on:** None
**Labels:** `models`, `typing`

Implemented in `src/news_trade/models/market.py`.  Phase 0 extended the
model with two additional optional fields:

- `atr_14d: float | None` — 14-day Average True Range in dollars
- `relative_volume: float | None` — today's volume divided by 20-day average volume

`PipelineState.market_context` is typed as `dict[str, MarketSnapshot]`.

---

## ~~Issue 4: Add unit tests for Pydantic models and pipeline graph~~ ✅ Done

**Priority:** P0 — Must-have
**Depends on:** #3 (MarketSnapshot)
**Labels:** `testing`

Implemented in commit `a7d022b`:

- `tests/test_models.py` — 44 tests covering all Pydantic models (including new `atr_14d` / `relative_volume` fields)
- `tests/test_pipeline.py` — 10 tests for `build_pipeline()` and routing helpers
- `tests/test_risk_rules.py` — 10 skipped placeholder tests for risk rule methods
- `tests/test_providers.py` — 25 tests for Protocol compliance, factory functions, `KeywordSentimentProvider` logic, and `Settings` enums

Total: 96 passing tests.

---

## ~~Issue 5: Implement NewsIngestorAgent end-to-end~~ ✅ Done

**Priority:** P1 — Should-have
**Depends on:** None (ORM and async event bus are already implemented)
**Labels:** `agent`, `feature`

Phase 0 refactored `NewsIngestorAgent` to accept an injected `NewsProvider`
instead of calling Benzinga/Polygon directly.  Provider-specific HTTP logic
lives in `providers/news/benzinga.py` and `providers/news/rss.py`.

- `run()` — delegates fetch to `self._provider`, deduplicates, persists, publishes
- `_is_duplicate()`, `_matches_watchlist()`, `_persist()` — unchanged
- `_classify_event_type()` / `_parse_dt()` — module-level helpers retained for backward compatibility
- `tests/test_news_ingestor.py` — 27 tests updated to use a mock provider fixture

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

## ~~Phase 0: Provider Abstraction Layer~~ ✅ Done

**Commit:** `e6efcc9`
**Branch:** `claude/provider-abstraction-layer-XED3B`
**Labels:** `architecture`, `refactor`, `feature`

Establishes a provider abstraction layer so the pipeline can swap between
free-tier and premium data sources via configuration, without touching agent logic.

### Deliverables

| # | Task | Files |
|---|------|-------|
| 1 | `atr_14d` + `relative_volume` on `MarketSnapshot` | `models/market.py` |
| 2 | `NewsProvider`, `MarketDataProvider`, `SentimentProvider` Protocols | `providers/base.py` |
| 3 | RSS, Benzinga news providers | `providers/news/rss.py`, `providers/news/benzinga.py` |
| 4 | yfinance, Polygon free, Polygon paid market providers | `providers/market/` |
| 5 | Claude (with budget cap), keyword sentiment providers | `providers/sentiment/` |
| 6 | Provider factory functions | `providers/__init__.py` |
| 7 | `NewsProviderType`, `MarketDataProviderType`, `SentimentProviderType` enums | `config.py` |
| 8 | Cost-control settings (`claude_daily_budget_usd`, `sentiment_dry_run`, `news_keyword_prefilter`) | `config.py`, `.env.example` |
| 9 | Agent DI refactor — `NewsIngestorAgent`, `MarketDataAgent`, `SentimentAnalystAgent` | `agents/` |
| 10 | Pipeline wiring via factory | `graph/pipeline.py` |
| 11 | 25 new provider + settings tests | `tests/test_providers.py` |

### Design decisions

- **Protocols over ABCs** — structural subtyping; providers need no inheritance
- **Factory with `match/case`** — three injection points; no DI framework needed
- **Daily budget cap** — `ClaudeSentimentProvider` tracks per-day token spend and falls back to neutral when the cap is hit
- **Keyword pre-filter** — `SentimentAnalystAgent` strips non-watchlist events before the Claude call to reduce cost
- **Default stack is free-tier** — `NEWS_PROVIDER=rss`, `MARKET_DATA_PROVIDER=yfinance`, `SENTIMENT_PROVIDER=claude`

---

## Dependency graph

```
#3 MarketSnapshot ✅ ──► #4 Tests ✅
#4 Tests ✅ ───────────► #9 CI ✅
#5 NewsIngestorAgent ✅ (no remaining deps — ORM and event bus done)
#7 docker-compose ✅    (independent)
#8 py.typed ✅          (independent)
Phase 0 Provider Layer ✅ (depends on #3, #5)
```

All issues and Phase 0 resolved — no remaining work on this branch.
