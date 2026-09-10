"""Shared test configuration.

Sets ``DATABASE_URL`` to a throwaway SQLite file **before** ``app.config`` is
imported, then brings that database to head with Alembic. This mirrors the
production contract exactly: the application never creates its own tables, so
the test suite must migrate too — which also means every run exercises the
migrations.
"""

import os
import tempfile
from pathlib import Path

# Per-process file: several pytest processes (parallel agents, xdist) must not
# share one SQLite file, or one run's teardown deletes another run's database.
TEST_DB_PATH = Path(tempfile.gettempdir()) / f"careeros_test_{os.getpid()}.db"

# An explicitly configured DATABASE_URL wins, so CI can point the same suite at
# a real PostgreSQL service. Only the default (nothing set) falls back to a
# throwaway SQLite file. Overriding unconditionally made the Postgres CI job
# silently run against SQLite.
if not os.environ.get("DATABASE_URL"):
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(TEST_DB_PATH) + suffix)
        if candidate.exists():
            candidate.unlink()
    os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("API_KEY", "test-api-key")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.config import PROJECT_ROOT  # noqa: E402

TEST_API_KEY = os.environ["API_KEY"]
AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


def _migrate() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(config, "head")


_migrate()


@pytest.fixture
def auth_headers() -> dict:
    """Headers authorizing a write request."""
    return dict(AUTH_HEADERS)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Keep token buckets from leaking state between tests."""
    from app.jobs.ratelimit import source_rate_limiter

    source_rate_limiter.reset()
    yield
    source_rate_limiter.reset()
