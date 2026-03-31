"""Alembic migration environment — sync SQLAlchemy, SQLite-compatible.

The database URL is read from the application Settings object so it always
matches whatever DATABASE_URL the process was started with. The URL is never
hard-coded in alembic.ini.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from news_trade.config import get_settings
from news_trade.services.tables import Base

# ---------------------------------------------------------------------------
# Alembic Config object — gives access to values in alembic.ini
# ---------------------------------------------------------------------------
config = context.config

# Inject the runtime DATABASE_URL so alembic.ini never needs to hard-code it.
_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.database_url)

# Target metadata drives --autogenerate comparison.
target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline mode (generate SQL script without a live DB connection)
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Generates SQL DDL to stdout/file without connecting to the database.
    Useful for reviewing what a migration will do before applying it.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode (connects to the database and applies migrations)
# ---------------------------------------------------------------------------


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates a real engine connection and applies pending migrations.
    This is what ``alembic upgrade head`` and the programmatic API call.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
