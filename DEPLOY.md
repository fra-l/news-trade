# Deployment Notes

## Normal startup

`create_tables()` calls `alembic upgrade head` programmatically on every startup. No manual
migration step is required for a fresh database or when the new version adds migrations.

## Migrating from the pre-Alembic schema (existing database)

If your database was created by a version of news-trade that used `Base.metadata.create_all()`
(before Alembic was introduced), the `initial_schema` migration will fail with
`TableAlreadyExistsError` because the tables already exist.

Run this **once** before starting the new version:

```bash
uv run alembic stamp head
```

This writes the current revision into Alembic's `alembic_version` table without executing
any DDL. Subsequent application starts will find no pending migrations and proceed normally.

New deployments (empty database) do **not** need this step.

## Adding new migrations

After modifying table definitions in `src/news_trade/services/tables.py`, generate a migration:

```bash
uv run alembic revision --autogenerate -m "describe_the_change"
```

Review the generated file in `alembic/versions/` before committing — autogenerate is not
perfect and may miss computed columns, custom types, or index naming edge cases. Then apply:

```bash
uv run alembic upgrade head
```

Or simply restart the application — `create_tables()` runs `upgrade head` on every startup.

## Other useful Alembic commands

```bash
uv run alembic history          # list all migrations and their revision IDs
uv run alembic current          # show what revision the live database is at
uv run alembic downgrade -1     # roll back one migration (use with caution)
uv run alembic upgrade head     # apply all pending migrations manually
```

## Switch window

When upgrading between versions that include schema changes, the safe maintenance window is:

- **Friday 16:00 ET → Monday 06:45 ET** (before the first cron at 07:00 ET)

The cron scheduler (`earnings_calendar`, `expiry_scanner`, `pead_expiry_scanner`) has a
`misfire_grace_time=300s` — brief outages within that window are tolerated. Alpaca positions
are broker-side and survive process restarts.
