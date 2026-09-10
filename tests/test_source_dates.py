"""Tests for source date extraction: parse_timestamp and the ATS adapters.

Covers the timestamp parsing contract in app/core/timeutils.py, the
per-adapter field mapping (which raw field becomes source_posted_at /
source_updated_at / source_deadline), and the end-to-end path through
JobNormalizer and JobDeduplicator so a posted_at date actually reaches a
persisted, sortable JobRow.
"""

from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.timeutils import parse_timestamp
from app.jobs.database.models import Base, JobRow
from app.jobs.deduplication.deduplicator import JobDeduplicator
from app.jobs.normalization.normalizer import JobNormalizer
from app.jobs.sources.ashby import AshbySource
from app.jobs.sources.greenhouse import GreenhouseSource
from app.jobs.sources.lever import LeverSource

UTC = timezone.utc


# ---------------------------------------------------------------------------
# parse_timestamp
# ---------------------------------------------------------------------------


def test_parse_timestamp_iso_with_z():
    result = parse_timestamp("2026-08-15T12:00:00Z")
    assert result == datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
    assert result.tzinfo is not None


def test_parse_timestamp_iso_with_offset():
    result = parse_timestamp("2026-08-15T12:00:00+05:30")
    assert result == datetime(2026, 8, 15, 6, 30, 0, tzinfo=UTC)
    assert result.tzinfo is not None


def test_parse_timestamp_epoch_milliseconds():
    # Lever's createdAt style: 13-digit millisecond epoch.
    result = parse_timestamp(1723500000000)
    assert result == datetime(2024, 8, 12, 22, 0, 0, tzinfo=UTC)


def test_parse_timestamp_epoch_milliseconds_as_string():
    result = parse_timestamp("1723500000000")
    assert result == datetime(2024, 8, 12, 22, 0, 0, tzinfo=UTC)


def test_parse_timestamp_epoch_seconds():
    result = parse_timestamp(1723500000)
    assert result == datetime(2024, 8, 12, 22, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-date", "garbage-2026", float("nan")])
def test_parse_timestamp_unparseable_returns_none(value):
    assert parse_timestamp(value) is None


def test_parse_timestamp_bool_is_not_treated_as_epoch():
    # bool is an int subclass in Python; must not be silently coerced to a date.
    assert parse_timestamp(True) is None
    assert parse_timestamp(False) is None


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-15T12:00:00Z",
        "2026-08-15T12:00:00+05:30",
        1723500000000,
        1723500000,
        "1723500000000",
    ],
)
def test_parse_timestamp_every_non_none_result_is_aware_utc(value):
    result = parse_timestamp(value)
    assert result is not None
    assert result.tzinfo is not None
    assert result.utcoffset() == timezone.utc.utcoffset(result)


# ---------------------------------------------------------------------------
# GreenhouseSource date mapping
# ---------------------------------------------------------------------------


def _greenhouse_client(jobs_list):
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"jobs": jobs_list})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_greenhouse_first_published_becomes_posted_at():
    jobs = [
        {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Remote"},
            "content": "<p>desc</p>",
            "first_published": "2026-01-01T00:00:00Z",
            "updated_at": "2026-02-01T00:00:00Z",
        }
    ]
    adapter = GreenhouseSource(client=_greenhouse_client(jobs))
    raw_jobs = await adapter.discover("acme")

    job = raw_jobs[0]
    assert job.source_posted_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert job.source_updated_at == datetime(2026, 2, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_greenhouse_falls_back_to_updated_at_when_first_published_absent():
    jobs = [
        {
            "id": 2,
            "title": "Engineer",
            "location": {"name": "Remote"},
            "content": "<p>desc</p>",
            "updated_at": "2026-02-01T00:00:00Z",
            # no first_published
        }
    ]
    adapter = GreenhouseSource(client=_greenhouse_client(jobs))
    raw_jobs = await adapter.discover("acme")

    job = raw_jobs[0]
    assert job.source_posted_at == datetime(2026, 2, 1, tzinfo=UTC)
    assert job.source_updated_at == datetime(2026, 2, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_greenhouse_both_absent_yields_none_not_fabricated():
    jobs = [
        {
            "id": 3,
            "title": "Engineer",
            "location": {"name": "Remote"},
            "content": "<p>desc</p>",
            # neither first_published nor updated_at present
        }
    ]
    adapter = GreenhouseSource(client=_greenhouse_client(jobs))
    raw_jobs = await adapter.discover("acme")

    job = raw_jobs[0]
    assert job.source_posted_at is None
    assert job.source_updated_at is None


@pytest.mark.asyncio
async def test_greenhouse_deadline_is_never_fabricated():
    # The Greenhouse Job Board API has no deadline field at all - source_deadline
    # must stay None rather than being invented from some other date.
    jobs = [
        {
            "id": 4,
            "title": "Engineer",
            "location": {"name": "Remote"},
            "content": "<p>desc</p>",
            "first_published": "2026-01-01T00:00:00Z",
            "updated_at": "2026-02-01T00:00:00Z",
        }
    ]
    adapter = GreenhouseSource(client=_greenhouse_client(jobs))
    raw_jobs = await adapter.discover("acme")
    assert raw_jobs[0].source_deadline is None


# ---------------------------------------------------------------------------
# LeverSource date mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lever_created_at_epoch_millis_becomes_posted_at():
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json=[
                {
                    "id": "lev-1",
                    "text": "Engineer",
                    "urls": {"show": "https://jobs.lever.co/acme/lev-1"},
                    "categories": {"location": "Remote"},
                    "createdAt": 1723500000000,
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LeverSource(client=client)
    raw_jobs = await adapter.discover("acme")

    job = raw_jobs[0]
    assert job.source_posted_at == datetime(2024, 8, 12, 22, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_lever_deadline_is_never_fabricated():
    # Lever's Postings API exposes no deadline field - must stay None.
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json=[
                {
                    "id": "lev-2",
                    "text": "Engineer",
                    "urls": {"show": "https://jobs.lever.co/acme/lev-2"},
                    "categories": {"location": "Remote"},
                    "createdAt": 1723500000000,
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LeverSource(client=client)
    raw_jobs = await adapter.discover("acme")
    assert raw_jobs[0].source_deadline is None


# ---------------------------------------------------------------------------
# AshbySource date mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ashby_published_at_becomes_posted_at():
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "ash-1",
                        "title": "Engineer",
                        "location": "Remote",
                        "descriptionPlain": "desc",
                        "publishedAt": "2026-03-01T00:00:00Z",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AshbySource(client=client)
    raw_jobs = await adapter.discover("acme")

    job = raw_jobs[0]
    assert job.source_posted_at == datetime(2026, 3, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_ashby_deadline_is_never_fabricated():
    # Ashby's public Posting API exposes no deadline field - must stay None.
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "ash-2",
                        "title": "Engineer",
                        "location": "Remote",
                        "descriptionPlain": "desc",
                        "publishedAt": "2026-03-01T00:00:00Z",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AshbySource(client=client)
    raw_jobs = await adapter.discover("acme")
    assert raw_jobs[0].source_deadline is None


# ---------------------------------------------------------------------------
# End-to-end: RawJob -> NormalizedJob -> persisted JobRow, sortable on posted_at
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.mark.asyncio
async def test_dates_survive_normalization_and_persistence(db_session):
    jobs = [
        {
            "id": 5,
            "title": "Engineer",
            "location": {"name": "Remote"},
            "content": "<p>desc</p>",
            "first_published": "2026-01-05T00:00:00Z",
            "updated_at": "2026-01-10T00:00:00Z",
        }
    ]
    adapter = GreenhouseSource(client=_greenhouse_client(jobs))
    raw_jobs = await adapter.discover("acme")
    raw_job = raw_jobs[0]

    normalized = JobNormalizer().normalize(raw_job, company_name="Acme")
    assert normalized.posted_at == datetime(2026, 1, 5, tzinfo=UTC)
    assert normalized.source_updated_at == datetime(2026, 1, 10, tzinfo=UTC)

    result = JobDeduplicator().process(db_session, normalized, commit=True)
    assert result.status == "NEW"

    persisted = db_session.query(JobRow).filter_by(id=result.job_row.id).one()
    # Stored naive-UTC per app/core/timeutils.to_db - the persisted value must
    # equal the wall-clock UTC instant even though tzinfo is stripped.
    assert persisted.posted_at == datetime(2026, 1, 5)
    assert persisted.posted_at.tzinfo is None


@pytest.mark.asyncio
async def test_posted_at_is_sortable_across_multiple_persisted_jobs(db_session):
    earlier = [
        {
            "id": 10,
            "title": "Engineer A",
            "location": {"name": "Remote"},
            "content": "<p>a</p>",
            "first_published": "2026-01-01T00:00:00Z",
        }
    ]
    later = [
        {
            "id": 11,
            "title": "Engineer B",
            "location": {"name": "Remote"},
            "content": "<p>b</p>",
            "first_published": "2026-06-01T00:00:00Z",
        }
    ]
    normalizer = JobNormalizer()
    dedup = JobDeduplicator()

    for jobs_payload in (later, earlier):  # insert out of order on purpose
        adapter = GreenhouseSource(client=_greenhouse_client(jobs_payload))
        raw_job = (await adapter.discover("acme"))[0]
        normalized = normalizer.normalize(raw_job, company_name="Acme")
        dedup.process(db_session, normalized, commit=True)

    rows = db_session.query(JobRow).order_by(JobRow.posted_at.asc()).all()
    assert [row.title for row in rows] == ["Engineer A", "Engineer B"]
