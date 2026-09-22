"""Fixtures for Phase 3 discovery tests.

Each test gets a private in-memory SQLite database with every table the app
declares (``create_all_tables``), so volume tests never touch the shared
migrated test database, and a fake source factory that returns crafted
RawJobs or raises a classified ``SourceError`` — no network anywhere.
"""

from datetime import datetime, timedelta
from typing import Callable, List, Optional

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.timeutils import utc_now
from app.database import create_all_tables
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.sources.base import JobSource, SourceError, source_circuits


class WriteCounter:
    """Counts INSERT/UPDATE/DELETE statements issued on an engine."""

    def __init__(self):
        self.inserts = 0
        self.updates = 0
        self.deletes = 0
        self.selects = 0

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip()[:6].upper()
        if head.startswith("INSERT"):
            self.inserts += 1
        elif head.startswith("UPDATE"):
            self.updates += 1
        elif head.startswith("DELETE"):
            self.deletes += 1
        elif head.startswith("SELECT"):
            self.selects += 1

    @property
    def writes(self) -> int:
        return self.inserts + self.updates + self.deletes


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    create_all_tables(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def write_counter(engine):
    counter = WriteCounter()
    event.listen(engine, "after_cursor_execute", counter)
    yield counter
    event.remove(engine, "after_cursor_execute", counter)


@pytest.fixture
def db_session(engine):
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _reset_circuits():
    source_circuits.reset()
    yield
    source_circuits.reset()


def make_raw(
    source_job_id: str,
    title: str = "Backend Engineer",
    company: str = "acme",
    location: Optional[str] = "Remote",
    content: str = "<p>We need Python and PostgreSQL experience. Full-time, remote.</p>",
    source: JobSourceType = JobSourceType.OTHER,
    posted_at: Optional[datetime] = None,
    url: Optional[str] = None,
) -> RawJob:
    return RawJob(
        source=source,
        source_job_id=source_job_id,
        source_url=url or f"https://example.com/{source.value.lower()}/{company}/{source_job_id}",
        discovered_url=f"https://example.com/{company}",
        raw_title=title,
        raw_content=content,
        content_type="html",
        raw_location=location,
        source_posted_at=posted_at,
        raw_metadata={"board_token": company},
    )


def fake_source(
    source_type: JobSourceType = JobSourceType.OTHER,
    jobs: Optional[List[RawJob]] = None,
    error: Optional[Exception] = None,
    provider: Optional[Callable[[], List[RawJob]]] = None,
) -> type:
    """A JobSource subclass (the service instantiates the *type*)."""

    class _Fake(JobSource):
        @property
        def source_type(self) -> JobSourceType:
            return source_type

        async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
            self.stats.requests += 1
            if error is not None:
                if isinstance(error, SourceError) and error.kind == "rate_limited":
                    self.stats.rate_limit_hits += 1
                raise error
            if provider is not None:
                return provider()
            return list(jobs or [])

    return _Fake


@pytest.fixture
def days_ago():
    def _days_ago(days: float) -> datetime:
        return utc_now() - timedelta(days=days)

    return _days_ago
