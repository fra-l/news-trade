"""SQLAlchemy engine, session factory, and table initialisation."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.config import Settings


def build_engine(settings: Settings) -> Engine:
    """Create a SQLAlchemy engine from application settings.

    For ``sqlite:///`` URLs the parent directory is created automatically
    so that the database file can be written on first run.
    """
    url = settings.database_url
    if url.startswith("sqlite:///"):
        db_path = url.removeprefix("sqlite:///")
        parent = Path(db_path).parent
        os.makedirs(parent, exist_ok=True)
    return create_engine(url, echo=False)


def build_session_factory(settings: Settings) -> sessionmaker[Session]:
    """Return a session factory bound to the configured database."""
    engine = build_engine(settings)
    return sessionmaker(bind=engine)


def _make_alembic_config(settings: Settings) -> Config:
    """Build an Alembic Config pointing at the project's alembic/ directory.

    ``sqlalchemy.url`` is set programmatically so that DATABASE_URL drives
    migrations in all environments — the ini file contains no hardcoded URL.

    ``database.py`` lives at ``src/news_trade/services/database.py``.
    ``parents[3]`` walks up: services/ → news_trade/ → src/ → project root.
    """
    ini_path = Path(__file__).parents[3] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def create_tables(settings: Settings) -> None:
    """Apply all pending Alembic migrations (``upgrade head``) on startup.

    Replaces the old ``Base.metadata.create_all()`` call so that schema
    evolution is version-controlled and auditable.

    First-deploy note
    -----------------
    If the database already exists with the correct schema (created by the
    old ``create_all()`` path before this change was deployed), running
    ``upgrade head`` will fail because the baseline migration uses
    ``op.create_table()`` calls that conflict with existing tables.

    In that situation, stamp the database *once* before restarting:

        uv run alembic stamp head

    After stamping, ``upgrade head`` will see no pending revisions and become
    a no-op on every subsequent startup. See DEPLOY.md for the full procedure.
    """
    # Ensure the SQLite parent directory exists before Alembic opens the file.
    # build_engine() does this too, but Alembic's env.py creates its own engine
    # directly from the URL and would fail if the directory is missing.
    url = settings.database_url
    if url.startswith("sqlite:///"):
        db_path = url.removeprefix("sqlite:///")
        os.makedirs(Path(db_path).parent, exist_ok=True)
    command.upgrade(_make_alembic_config(settings), "head")
