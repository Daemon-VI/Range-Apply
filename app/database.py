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
REQUIRED_TABLES = (
    "jobs",
    "discovery_runs",
    "job_matches",
    "tenants",
    "evidence_nodes",
    "opportunities",
    "application_queue",
)


class SchemaNotReadyError(RuntimeError):
    """Raised when the configured database has not been migrated."""


def get_engine():
    """Get or create the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        is_sqlite = settings.database_url.startswith("sqlite")
        # SQLite has one writer at a time: wait for the lock (30 s) instead of failing
        # with "database is locked" when autopilot, a worker and a page write together.
        connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
        engine_kwargs = {}
        if not is_sqlite:
            # Free-tier Postgres (Supabase, Neon) has tight connection caps and
            # kills idle connections; a small pre-pinged pool is what survives.
            engine_kwargs = {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_pre_ping": settings.db_pool_pre_ping,
                "pool_recycle": settings.db_pool_recycle_seconds,
            }
        _engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            echo=settings.debug and settings.log_level == "DEBUG",
            **engine_kwargs,
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


def alembic_revision(engine=None) -> str | None:
    """The revision Alembic recorded for this database, or ``None`` when there is none.

    ``None`` covers both "no ``alembic_version`` table" and "the table exists
    but holds no row" (what an ``alembic upgrade`` that failed on its first
    migration leaves behind).
    """
    engine = engine or get_engine()
    if "alembic_version" not in set(inspect(engine).get_table_names()):
        return None
    with engine.connect() as connection:
        row = connection.exec_driver_sql("SELECT version_num FROM alembic_version").fetchone()
    return str(row[0]) if row and row[0] else None


#: Tables that a database created outside Alembic (the pre-migration
#: ``create_all()`` bootstrap) will have. Their presence without a revision
#: means "adopt the schema", never "create it".
_BOOTSTRAP_MARKERS = ("jobs", "discovery_runs")

UNSTAMPED_ADVICE = (
    "Do NOT run 'alembic stamp head': that records the newest revision without creating the "
    "tables it adds, after which 'alembic upgrade head' silently does nothing. Instead find the "
    "revision whose schema this database matches (build a scratch database with "
    "'DATABASE_URL=sqlite:///scratch.db alembic upgrade <revision>' and compare the DDL, or use "
    "'alembic history' and the tables present), back the file up, run "
    "'alembic stamp <that revision>', then 'alembic upgrade head'."
)


def unstamped_schema(engine=None) -> bool:
    """True when application tables exist but Alembic has no recorded revision.

    This is the state an old ``create_all()`` bootstrap leaves behind; a plain
    ``alembic upgrade head`` then fails with "table ... already exists".
    """
    engine = engine or get_engine()
    existing = set(inspect(engine).get_table_names())
    return any(name in existing for name in _BOOTSTRAP_MARKERS) and alembic_revision(engine) is None


def verify_schema(engine=None) -> None:
    """Fail fast when the database has not been migrated.

    Raises:
        SchemaNotReadyError: if required tables or the Alembic version stamp
            are missing. The message names the exact command to run — and,
            for a database that was created outside Alembic, the exact
            command *not* to run.
    """
    engine = engine or get_engine()
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    absent = [name for name in REQUIRED_TABLES if name not in existing]
    if absent and unstamped_schema(engine):
        raise SchemaNotReadyError(
            f"Database tables exist but Alembic has no recorded revision (missing required tables: {', '.join(absent)}). "
            "The tables were created outside of migrations, so 'alembic upgrade head' will fail with "
            f"\"table already exists\". {UNSTAMPED_ADVICE} Database: {safe_database_url()}"
        )
    if absent:
        raise SchemaNotReadyError(
            f"Database schema is not initialized (missing tables: {', '.join(absent)}). "
            "Run 'alembic upgrade head' before starting the application. "
            f"Database: {safe_database_url()}"
        )

    if alembic_revision(engine) is None:
        raise SchemaNotReadyError(
            "Database tables exist but Alembic has no recorded revision. "
            f"This happens when tables were created outside of migrations. {UNSTAMPED_ADVICE}"
        )

    logger.info("Database schema verified: %s", safe_database_url())


def create_all_tables(engine=None) -> None:
    """Create every table directly from the ORM metadata.

    **Test helper only.** Production and development schemas are owned by
    Alembic; calling this against a real database is what breaks migrations.
    """
    from app.application import killswitch as _killswitch  # noqa: F401
    from app.application.database import models as _application_models  # noqa: F401
    from app.career.database import models as _career_models  # noqa: F401
    from app.documents.database import models as _document_models  # noqa: F401
    from app.execution.database import models as _execution_models  # noqa: F401
    from app.intelligence.database import models as _intelligence_models  # noqa: F401
    from app.jobs.database.models import Base
    from app.pipeline.database import models as _pipeline_models  # noqa: F401
    from app.preparation.database import models as _preparation_models  # noqa: F401
    from app.scheduler.database import models as _scheduler_models  # noqa: F401
    from app.tailoring.database import models as _tailoring_models  # noqa: F401

    Base.metadata.create_all(bind=engine or get_engine())
