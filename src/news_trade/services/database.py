"""SQLAlchemy async engine and session factory for trade logging."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from news_trade.config import Settings


def build_engine(settings: Settings):
    """Create a SQLAlchemy engine from application settings."""
    return create_engine(settings.database_url, echo=False)


def build_session_factory(settings: Settings) -> sessionmaker[Session]:
    """Return a session factory bound to the configured database."""
    engine = build_engine(settings)
    return sessionmaker(bind=engine)
