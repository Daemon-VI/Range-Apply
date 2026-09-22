"""Migration integrity tests (P0.3 regression coverage).

Alembic is the sole owner of the schema (see ``app/database.py``): the
application must never create its own tables. These tests verify that
``alembic upgrade head`` alone produces the exact schema the ORM expects, that
a downgrade/upgrade cycle does not break, and that ``verify_schema`` actually
distinguishes an unmigrated database from a migrated one.

Each test points a *temporary* SQLite database at ``settings.database_url``
(which is what ``alembic/env.py`` reads) and always restores the previous
value afterward, so this file never disturbs the shared test database that
``tests/conftest.py`` set up for the rest of the suite.
"""

import os
from contextlib import contextmanager

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

# Registers the Phase 3 tables (match_policies, match_runs, job_matches,
# requirement_assessments) onto the shared Base.metadata, exactly as
# alembic/env.py does via the same import.
import app.intelligence.database.models  # noqa: F401,E402
from alembic import command
from app.config import PROJECT_ROOT, settings
from app.database import SchemaNotReadyError, verify_schema
from app.jobs.database.models import Base


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


@contextmanager
def _pointed_at(database_url: str):
    """Temporarily point both ``DATABASE_URL`` and the settings singleton at
    ``database_url``.

    ``alembic/env.py`` reads ``settings.database_url`` (not the environment
    directly), so the singleton attribute is what actually matters; the env
    var is kept in sync too since that is the documented contract for how the
    app is configured. Both are restored in ``finally`` no matter what the
    caller does inside the block.
    """
    original_url = settings.database_url
    original_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    settings.database_url = database_url
    try:
        yield
    finally:
        settings.database_url = original_url
        if original_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_env


def _sqlite_url(path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_alembic_upgrade_head_matches_orm_metadata_exactly(tmp_path):
    """A clean database migrated to head must have every table and every
    ORM column the models declare - no drift between migrations and models.
    """
    url = _sqlite_url(tmp_path / "migration_head.db")
    with _pointed_at(url):
        command.upgrade(_alembic_config(), "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())

        expected_tables = set(Base.metadata.tables.keys())
        missing_tables = expected_tables - existing_tables
        assert not missing_tables, f"migration did not create: {sorted(missing_tables)}"

        for table_name, table in Base.metadata.tables.items():
            db_columns = {col["name"] for col in inspector.get_columns(table_name)}
            orm_columns = {col.name for col in table.columns}
            missing_columns = orm_columns - db_columns
            assert not missing_columns, f"{table_name} missing columns: {sorted(missing_columns)}"
    finally:
        engine.dispose()


def test_downgrade_then_upgrade_round_trips(tmp_path):
    """One step down and back up must succeed and leave the schema intact."""
    url = _sqlite_url(tmp_path / "migration_roundtrip.db")
    config = _alembic_config()

    with _pointed_at(url):
        command.upgrade(config, "head")
        command.downgrade(config, "-1")
        command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        # The head schema (including the last migration's tables) must be
        # fully back in place after the round trip.
        expected_tables = set(Base.metadata.tables.keys())
        assert expected_tables <= set(inspector.get_table_names())
    finally:
        engine.dispose()


def test_verify_schema_rejects_empty_database_and_accepts_migrated_one(tmp_path):
    """The P0.3 regression: the app must refuse to run against a database it
    never migrated, and must accept one that Alembic actually brought to head.
    """
    empty_url = _sqlite_url(tmp_path / "never_migrated.db")
    empty_engine = create_engine(empty_url)
    try:
        with pytest.raises(SchemaNotReadyError):
            verify_schema(engine=empty_engine)
    finally:
        empty_engine.dispose()

    migrated_url = _sqlite_url(tmp_path / "properly_migrated.db")
    with _pointed_at(migrated_url):
        command.upgrade(_alembic_config(), "head")

    migrated_engine = create_engine(migrated_url)
    try:
        verify_schema(engine=migrated_engine)  # must not raise
    finally:
        migrated_engine.dispose()


# ---------------------------------------------------------------------------
# A database created outside Alembic (the old ``create_all()`` bootstrap) must
# be diagnosed as "adopt at the matching revision", never as "stamp head".
# ---------------------------------------------------------------------------


def _bootstrap_like_database(tmp_path, revision: str):
    """A database whose tables match ``revision`` but which carries no Alembic row.

    Exactly what the pre-migration bootstrap left behind, and what an
    ``alembic upgrade head`` that failed on its first migration leaves behind
    (an empty ``alembic_version`` table).
    """
    url = _sqlite_url(tmp_path / f"bootstrap_{revision}.db")
    with _pointed_at(url):
        command.upgrade(_alembic_config(), revision)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM alembic_version")
    return url, engine


def test_unstamped_bootstrap_schema_is_told_to_stamp_the_matching_revision_not_head(tmp_path):
    from app.database import alembic_revision, unstamped_schema

    url, engine = _bootstrap_like_database(tmp_path, "b11ea16cb71f")
    try:
        assert alembic_revision(engine) is None and unstamped_schema(engine) is True
        with pytest.raises(SchemaNotReadyError) as excinfo:
            verify_schema(engine=engine)
        message = str(excinfo.value)
        assert "created outside of migrations" in message
        assert "Do NOT run 'alembic stamp head'" in message
        assert "alembic stamp <that revision>" in message and "alembic upgrade head" in message
        assert "Run 'alembic stamp head'" not in message, "the old advice that wedges the database is gone"
        # The desktop readiness check says the same thing instead of "run alembic upgrade head".
        from app.database import reset_engine
        from app.desktop.launcher import check_readiness

        with _pointed_at(url):
            reset_engine()
            try:
                report = check_readiness()
            finally:
                reset_engine()
        assert report.ready is False and report.database_migrated is False
        assert any("no Alembic revision" in p and "Do NOT run 'alembic stamp head'" in p for p in report.problems), report.problems
        assert not any("run `alembic upgrade head` first" in p for p in report.problems), report.problems
    finally:
        engine.dispose()


def test_fully_built_schema_without_a_revision_is_still_refused_with_the_adoption_advice(tmp_path):
    url, engine = _bootstrap_like_database(tmp_path, "head")
    try:
        with pytest.raises(SchemaNotReadyError) as excinfo:
            verify_schema(engine=engine)
        assert "no recorded revision" in str(excinfo.value) and "Do NOT run 'alembic stamp head'" in str(excinfo.value)
    finally:
        engine.dispose()


def test_application_startup_never_creates_tables(tmp_path):
    """Alembic stays authoritative: booting against an empty database fails
    fast and leaves the database exactly as empty as it found it."""
    from fastapi.testclient import TestClient
    from sqlalchemy import inspect as sa_inspect

    from app.database import reset_engine
    from app.main import app

    url = _sqlite_url(tmp_path / "boot_empty.db")
    with _pointed_at(url):
        reset_engine()
        try:
            with pytest.raises(SchemaNotReadyError):
                with TestClient(app):
                    pass
        finally:
            reset_engine()
    engine = create_engine(url)
    try:
        assert sa_inspect(engine).get_table_names() == [], "startup must not create_all()"
    finally:
        engine.dispose()
