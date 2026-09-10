"""SQLAlchemy database engine and session management.

Schema ownership rule: **Alembic is authoritative.** The application never
creates its own tables. Running ``create_all()`` at startup used to leave the
database populated but without an ``alembic_version`` row, after which
``alembic upgrade head`` failed permanently with "table already exists". The
startup path now only *verifies* the schema and tells the operator what to run.
"""

import logging

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None

# Tables that must exist for the application to serve any request.
REQUIRED_TABLES = ("jobs", "discovery_runs", "job_matches")


class SchemaNotReadyError(RuntimeError):
    """Raised when the configured database has not been migrated."""


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

        logger.info("Database engine created: %s", safe_database_url())
    return _engine


def safe_database_url() -> str:
    """Database URL with any credentials stripped, safe to log."""
    url = settings.database_url
    if "@" in url:
        scheme, _, rest = url.partition("://")
        return f"{scheme}://***@{rest.split('@', 1)[-1]}"
    return url


def reset_engine() -> None:
    """Drop the cached engine/session factory (used by tests)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_session_factory():
    """Get or create the session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal


def get_db() -> Session:
    """FastAPI dependency: yields a database session.

    The session is rolled back if the request handler raised, so a failed
    request can never leave a half-applied transaction behind for the next
    caller that borrows this connection.
    """
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def missing_tables(engine=None) -> list:
    """Return the required tables that are absent from the database."""
    inspector = inspect(engine or get_engine())
    existing = set(inspector.get_table_names())
    return [name for name in REQUIRED_TABLES if name not in existing]


def verify_schema(engine=None) -> None:
    """Fail fast when the database has not been migrated.

    Raises:
        SchemaNotReadyError: if required tables or the Alembic version stamp
            are missing. The message names the exact command to run.
    """
    engine = engine or get_engine()
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    absent = [name for name in REQUIRED_TABLES if name not in existing]
    if absent:
        raise SchemaNotReadyError(
            f"Database schema is not initialized (missing tables: {', '.join(absent)}). "
            "Run 'alembic upgrade head' before starting the application. "
            f"Database: {safe_database_url()}"
        )

    if "alembic_version" not in existing:
        raise SchemaNotReadyError(
            "Database tables exist but Alembic has never stamped this database. "
            "This happens when tables were created outside of migrations. "
            "Run 'alembic stamp head' if the schema is already current, "
            "otherwise recreate it with 'alembic upgrade head'."
        )

    logger.info("Database schema verified: %s", safe_database_url())


def create_all_tables(engine=None) -> None:
    """Create every table directly from the ORM metadata.

    **Test helper only.** Production and development schemas are owned by
    Alembic; calling this against a real database is what breaks migrations.
    """
    from app.intelligence.database import models as _intelligence_models  # noqa: F401
    from app.jobs.database.models import Base

    Base.metadata.create_all(bind=engine or get_engine())
