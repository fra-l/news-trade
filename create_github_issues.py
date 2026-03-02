#!/usr/bin/env python3
"""Create GitHub issues from ISSUES.md for the fra-l/news-trade repository.

Usage:
    GITHUB_TOKEN=<your_token> python create_github_issues.py

The script will:
  - Create issues for everything still needing work (open).
  - Create + immediately close issues 1, 2, and 6 since they are already
    implemented in the codebase.

Implementation status (as of codebase inspection):
  ✅ Issue 1 — services/tables.py has NewsEventRow, TradeSignalRow, OrderRow;
               database.py exports create_tables(); main.py calls it at startup.
  ✅ Issue 2 — Only services/event_bus.py exists (async redis); the sync
               duplicate (redis_bus.py) has already been removed.
  🔲 Issue 3 — No models/market.py; PipelineState.market_context is still
               dict[str, dict].
  🔲 Issue 4 — tests/ only contains __init__.py, no test files.
  🔲 Issue 5 — agents/news_ingestor.py methods all raise NotImplementedError.
  ✅ Issue 6 — services/database.py calls os.makedirs() before create_all(),
               so the data/ directory is created automatically.
  🔲 Issue 7 — No docker-compose.yml in the repo.
  🔲 Issue 8 — No src/news_trade/py.typed marker file.
  🔲 Issue 9 — No .github/workflows/ci.yml (only claude*.yml workflows exist).
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

REPO = "fra-l/news-trade"
API_BASE = "https://api.github.com"


def gh(method: str, path: str, body: dict | None = None) -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN environment variable not set.", file=sys.stderr)
        print("Usage: GITHUB_TOKEN=<your_token> python create_github_issues.py", file=sys.stderr)
        sys.exit(1)

    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        print(f"GitHub API error {e.code}: {body_text}", file=sys.stderr)
        raise


def ensure_labels() -> None:
    """Create required labels if they don't exist."""
    needed = {
        "database":      ("0075ca", "Database and persistence layer"),
        "core":          ("e4e669", "Core system functionality"),
        "cleanup":       ("fef2c0", "Code cleanup and refactoring"),
        "models":        ("1d76db", "Pydantic / data models"),
        "typing":        ("0e8a16", "Type annotations and type safety"),
        "testing":       ("d93f0b", "Test coverage"),
        "agent":         ("5319e7", "Agent implementation"),
        "feature":       ("a2eeef", "New feature"),
        "infrastructure":("c5def5", "Infrastructure and tooling"),
        "dx":            ("f9d0c4", "Developer experience"),
        "ci":            ("bfd4f2", "Continuous integration"),
    }
    existing_raw = gh("GET", f"/repos/{REPO}/labels?per_page=100")
    existing = {label["name"] for label in existing_raw}

    for name, (color, desc) in needed.items():
        if name not in existing:
            gh("POST", f"/repos/{REPO}/labels",
               {"name": name, "color": color, "description": desc})
            print(f"  Created label: {name}")
            time.sleep(0.3)


# ---------------------------------------------------------------------------
# Issue definitions
# ---------------------------------------------------------------------------

ISSUES: list[dict] = [
    # ── ✅ Already implemented ────────────────────────────────────────────────
    {
        "number_in_md": 1,
        "implemented": True,
        "title": "Add SQLAlchemy ORM table models for trade logging",
        "labels": ["database", "core"],
        "body": """\
## Summary

`services/tables.py` should contain `DeclarativeBase` ORM models for:

- `orders` — mirrors the `Order` Pydantic model, persists every order lifecycle
- `signals` (`TradeSignalRow`) — mirrors `TradeSignal`, logs every signal
- `news_events` (`NewsEventRow`) — stores ingested events for deduplication

A `create_tables()` helper should call `Base.metadata.create_all()`, and
`main.py` should call it at startup.

## Priority

**P0 — Must-have**

## Status

> ✅ **Already implemented** — `services/tables.py` contains all three ORM models
> (`NewsEventRow`, `TradeSignalRow`, `OrderRow`). `services/database.py` exports
> `create_tables()` and `main.py` calls it at startup.
""",
        "close_after_create": True,
        "close_reason": "completed",
    },
    # ── ✅ Already implemented ────────────────────────────────────────────────
    {
        "number_in_md": 2,
        "implemented": True,
        "title": "Consolidate duplicate Redis service files",
        "labels": ["cleanup", "core"],
        "body": """\
## Summary

Two Redis wrappers existed:

- `services/event_bus.py` — synchronous (`redis` package)
- `services/redis_bus.py` — asynchronous (`redis.asyncio`)

All agents use `async def run()`, so the async version is correct. Remove the
sync file, rename `redis_bus.py` → `event_bus.py`, and update all imports.

## Priority

**P0 — Must-have**

## Status

> ✅ **Already implemented** — only `services/event_bus.py` exists and it uses
> `redis.asyncio`. The synchronous duplicate has already been removed.
""",
        "close_after_create": True,
        "close_reason": "completed",
    },
    # ── 🔲 Needs work ─────────────────────────────────────────────────────────
    {
        "number_in_md": 3,
        "implemented": False,
        "title": "Add typed MarketSnapshot Pydantic model",
        "labels": ["models", "typing"],
        "body": """\
## Summary

`PipelineState.market_context` is typed as `dict[str, dict]` — the only
untyped boundary in the pipeline. Add a `MarketSnapshot` model in
`models/market.py`:

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

Update `PipelineState.market_context` to `dict[str, MarketSnapshot]` and
`MarketDataAgent._build_context()` return type accordingly.

## Priority

**P1 — Should-have**

## Depends on

None
""",
        "close_after_create": False,
        "close_reason": None,
    },
    # ── 🔲 Needs work ─────────────────────────────────────────────────────────
    {
        "number_in_md": 4,
        "implemented": False,
        "title": "Add unit tests for Pydantic models and pipeline graph",
        "labels": ["testing"],
        "body": """\
## Summary

`tests/` contains only `__init__.py`. Add at minimum:

- **`tests/test_models.py`** — validate serialisation round-trips, field
  constraints (score bounds, enum values), and optional-field defaults for
  all 5+ Pydantic models.
- **`tests/test_pipeline.py`** — verify `build_pipeline()` compiles without
  error and the graph has the expected node names and conditional edges.
- **`tests/test_risk_rules.py`** — stub for risk-manager rule unit tests
  (blocked until agent is implemented, but create the file with TODOs).

## Priority

**P0 — Must-have**

## Depends on

- #1 ORM tables ✅ (already done)
- #3 MarketSnapshot model
""",
        "close_after_create": False,
        "close_reason": None,
    },
    # ── 🔲 Needs work ─────────────────────────────────────────────────────────
    {
        "number_in_md": 5,
        "implemented": False,
        "title": "Implement NewsIngestorAgent end-to-end",
        "labels": ["agent", "feature"],
        "body": """\
## Summary

`agents/news_ingestor.py` exists but every method raises `NotImplementedError`.
Implement it fully to prove the architecture works end-to-end:

- `_fetch_benzinga()` — call Benzinga News API with `httpx`, parse into `NewsEvent`
- `_fetch_polygon()` — same for Polygon.io reference news
- `_is_duplicate()` — query SQLite (via ORM) for existing `event_id`
- `_matches_watchlist()` — filter tickers against `settings.watchlist`
- `run()` — orchestrate the above, publish to event bus, return state

## Priority

**P1 — Should-have**

## Depends on

- #1 ORM tables ✅ (already done)
- #2 async event bus ✅ (already done)
""",
        "close_after_create": False,
        "close_reason": None,
    },
    # ── ✅ Already implemented ────────────────────────────────────────────────
    {
        "number_in_md": 6,
        "implemented": True,
        "title": "Create `data/` directory for SQLite database",
        "labels": ["infrastructure"],
        "body": """\
## Summary

The default `DATABASE_URL` is `sqlite:///data/trades.db` but the `data/`
directory did not exist in the repo.

Options:

1. Add `data/.gitkeep` and commit the directory.
2. Add `os.makedirs()` in `create_tables()` to auto-create the parent
   directory before calling `create_all()`.

## Priority

**P1 — Should-have**

## Depends on

- #1 ORM tables ✅ (already done)

## Status

> ✅ **Already implemented** — `services/database.py` (`build_engine()`) calls
> `os.makedirs(parent, exist_ok=True)` for SQLite URLs before creating the
> engine, so `data/` is created automatically on first run.
""",
        "close_after_create": True,
        "close_reason": "completed",
    },
    # ── 🔲 Needs work ─────────────────────────────────────────────────────────
    {
        "number_in_md": 7,
        "implemented": False,
        "title": "Add `docker-compose.yml` for Redis",
        "labels": ["infrastructure", "dx"],
        "body": """\
## Summary

Contributors need Redis running locally but must install it manually today.
Add a `docker-compose.yml` with a Redis 7 service so they can run
`docker compose up -d`.

```yaml
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
```

## Priority

**P2 — Nice-to-have**

## Depends on

None
""",
        "close_after_create": False,
        "close_reason": None,
    },
    # ── 🔲 Needs work ─────────────────────────────────────────────────────────
    {
        "number_in_md": 8,
        "implemented": False,
        "title": "Add `py.typed` marker file",
        "labels": ["typing"],
        "body": """\
## Summary

Add an empty `src/news_trade/py.typed` marker file so that downstream
consumers get type-checking support per
[PEP 561](https://peps.python.org/pep-0561/).

## Priority

**P2 — Nice-to-have**

## Depends on

None
""",
        "close_after_create": False,
        "close_reason": None,
    },
    # ── 🔲 Needs work ─────────────────────────────────────────────────────────
    {
        "number_in_md": 9,
        "implemented": False,
        "title": "Add GitHub Actions CI workflow",
        "labels": ["ci", "dx"],
        "body": """\
## Summary

Add `.github/workflows/ci.yml` that runs on push and pull-request events:

1. `uv sync --group dev`
2. `uv run ruff check src/ tests/`
3. `uv run mypy src/`
4. `uv run pytest`

Matrix: Python 3.11 and 3.12.

Note: the repo already has `claude.yml` and `claude-code-review.yml` workflows,
but no general CI pipeline.

## Priority

**P2 — Nice-to-have**

## Depends on

- #4 Tests (tests must exist before CI is meaningful)
""",
        "close_after_create": False,
        "close_reason": None,
    },
]


def main() -> None:
    print(f"Creating issues for {REPO} …\n")

    print("Ensuring labels exist …")
    ensure_labels()
    print()

    created: list[tuple[int, str, int, bool]] = []  # (md_num, title, gh_num, implemented)

    for issue in ISSUES:
        md_num = issue["number_in_md"]
        title = issue["title"]
        implemented = issue["implemented"]

        if implemented:
            print(f"  [SKIP ✅] Issue {md_num}: {title} (already implemented)")
            continue

        status = "🔲 OPEN"
        print(f"  [{status}] Issue {md_num}: {title}")

        resp = gh(
            "POST",
            f"/repos/{REPO}/issues",
            {
                "title": title,
                "body": issue["body"],
                "labels": issue["labels"],
            },
        )
        gh_num = resp["number"]
        print(f"           → Created #{gh_num}")

        if issue["close_after_create"]:
            time.sleep(0.5)
            gh(
                "PATCH",
                f"/repos/{REPO}/issues/{gh_num}",
                {"state": "closed", "state_reason": issue["close_reason"]},
            )
            print(f"           → Closed as '{issue['close_reason']}'")

        created.append((md_num, title, gh_num, implemented))
        time.sleep(0.5)  # Stay well within rate limits

    print("\nDone! Summary:")
    print("-" * 60)
    for md_num, title, gh_num, implemented in created:
        icon = "✅" if implemented else "🔲"
        print(f"  {icon} ISSUES.md #{md_num} → GitHub #{gh_num}: {title}")

    open_count = sum(1 for *_, impl in created if not impl)
    closed_count = sum(1 for *_, impl in created if impl)
    print(f"\n  {open_count} open  |  {closed_count} closed (already implemented)")


if __name__ == "__main__":
    main()
