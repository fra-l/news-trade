"""SQLAlchemy engine, session factory, and table initialisation."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.config import Settings
from news_trade.services.tables import Base


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


def create_tables(settings: Settings) -> None:
    """Create all ORM tables if they do not already exist.

    Safe to call on every startup — ``create_all`` is a no-op for
    tables that are already present.
    """
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
