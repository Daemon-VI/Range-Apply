"""Tests for Deduplication and Identity Resolution (Milestone 8)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.jobs.database.models import Base, JobRow, JobVersionRow, SourceReferenceRow
from app.jobs.deduplication.deduplicator import JobDeduplicator
from app.jobs.models.enums import EmploymentType, JobSourceType, RemoteType
from app.jobs.models.job import NormalizedJob


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def make_job(source=JobSourceType.GREENHOUSE, job_id="101", title="Backend Engineer", company="Stripe", location="Seattle, WA", content_hash="hash_v1"):
    return NormalizedJob(
        canonical_key=f"{company.lower()}|{title.lower()}|{location.lower()}",
        source=source,
        source_job_id=job_id,
        company=company,
        title=title,
        original_title=title,
        description="A backend engineer role",
        location=location,
        remote_type=RemoteType.HYBRID,
        employment_type=EmploymentType.FULL_TIME,
        source_url=f"https://boards.greenhouse.io/stripe/jobs/{job_id}",
        content_hash=content_hash,
    )


def test_first_discovery_is_new(db_session):
    dedup = JobDeduplicator()
    job = make_job()

    res = dedup.process(db_session, job)
    assert res.status == "NEW"
    assert db_session.query(JobRow).count() == 1
    assert db_session.query(SourceReferenceRow).count() == 1


def test_idempotent_re_discovery_is_duplicate(db_session):
    dedup = JobDeduplicator()
    job = make_job()

    res1 = dedup.process(db_session, job)
    assert res1.status == "NEW"

    res2 = dedup.process(db_session, job)
    assert res2.status == "DUPLICATE"
    assert db_session.query(JobRow).count() == 1
    assert db_session.query(SourceReferenceRow).count() == 1


def test_changed_content_creates_version_and_updates(db_session):
    dedup = JobDeduplicator()
    job_v1 = make_job(content_hash="hash_v1")
    dedup.process(db_session, job_v1)

    job_v2 = make_job(content_hash="hash_v2")
    job_v2.description = "Updated role description with new requirements"

    res = dedup.process(db_session, job_v2)
    assert res.status == "UPDATED"
    assert res.is_content_changed is True
    assert db_session.query(JobRow).count() == 1
    assert db_session.query(JobVersionRow).count() == 1
    saved = db_session.query(JobRow).first()
    assert saved.content_hash == "hash_v2"


def test_cross_source_duplicate_provenance(db_session):
    dedup = JobDeduplicator()
    # First seen on Greenhouse
    gh_job = make_job(source=JobSourceType.GREENHOUSE, job_id="gh-100", company="Ramp", title="Software Engineer", location="New York, NY")
    dedup.process(db_session, gh_job)

    # Later seen on Ashby with matching canonical key
    ashby_job = make_job(source=JobSourceType.ASHBY, job_id="ashby-200", company="Ramp", title="Software Engineer", location="New York, NY")
    ashby_job.source_url = "https://jobs.ashbyhq.com/ramp/ashby-200"

    res = dedup.process(db_session, ashby_job)
    assert res.status == "CROSS_SOURCE_DUPLICATE"
    assert db_session.query(JobRow).count() == 1
    # Both source references should be preserved!
    assert db_session.query(SourceReferenceRow).count() == 2
    sources = [r.source for r in db_session.query(SourceReferenceRow).all()]
    assert "GREENHOUSE" in sources
    assert "ASHBY" in sources


def test_different_locations_remain_distinct_jobs(db_session):
    dedup = JobDeduplicator()
    job_sf = make_job(job_id="101", title="Backend Engineer", location="San Francisco, CA")
    job_hyd = make_job(job_id="102", title="Backend Engineer", location="Hyderabad, India")

    res1 = dedup.process(db_session, job_sf)
    res2 = dedup.process(db_session, job_hyd)

    assert res1.status == "NEW"
    assert res2.status == "NEW"
    assert db_session.query(JobRow).count() == 2
