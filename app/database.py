"""SQLAlchemy database engine and session management."""

import logging
from typing import Optional

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def get_engine():
    """Get or create the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        is_sqlite = settings.database_url.startswith("sqlite")
        connect_args = {"check_same_thread": False} if is_sqlite else {}
        _engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            echo=settings.debug and settings.log_level == "DEBUG",
        )
        # Enable WAL mode for SQLite for better concurrent read performance
        if is_sqlite:
            @event.listens_for(_engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        logger.info("Database engine created: %s", settings.database_url.split("@")[-1])
    return _engine


def get_session_factory():
    """Get or create the session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal


def get_db() -> Session:
    """FastAPI dependency: yields a database session."""
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables. Called at application startup."""
    from app.jobs.database.models import Base  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created/verified")
