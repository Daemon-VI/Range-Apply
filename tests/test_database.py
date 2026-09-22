"""Tests for Job database layer (Milestone 2)."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.jobs.database.models import (
    Base,
    DiscoveryRunRow,
    JobRow,
    JobVersionRow,
    SourceReferenceRow,
)


@pytest.fixture
def db_session():
    """In-memory SQLite database session for unit tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_job_crud(db_session):
    job_id = str(uuid.uuid4())
    job = JobRow(
        id=job_id,
        canonical_key="test_canonical_key_1",
        source="GREENHOUSE",
        source_job_id="1001",
        company="Stripe",
        title="Backend Engineer",
        original_title="Backend Engineer",
        description="Full job description",
        original_description="<p>Full job description</p>",
        location="Remote, US",
        locations=["Remote, US"],
        remote_type="REMOTE",
        employment_type="FULL_TIME",
        experience_level="MID",
        education_requirements=["BS in Computer Science or equivalent"],
        graduation_requirement={
            "minimum_year": 2026,
            "maximum_year": 2028,
            "exact_years": [2026, 2027, 2028],
            "original_text": "2026-2028 grads",
            "extraction_confidence": 0.9,
        },
        required_skills=["Python", "PostgreSQL"],
        preferred_skills=["Go", "Distributed Systems"],
        technologies=["Docker", "Redis"],
        source_url="https://boards.greenhouse.io/stripe/jobs/1001",
        content_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        processing_status="NORMALIZED",
        job_status="ACTIVE",
    )
    db_session.add(job)
    db_session.commit()

    saved = db_session.query(JobRow).filter_by(id=job_id).first()
    assert saved is not None
    assert saved.company == "Stripe"
    assert saved.graduation_requirement["minimum_year"] == 2026
    assert saved.required_skills == ["Python", "PostgreSQL"]
    assert saved.remote_type == "REMOTE"


def test_source_reference_relationship(db_session):
    job_id = str(uuid.uuid4())
    job = JobRow(
        id=job_id,
        canonical_key="test_canonical_key_2",
        source="GREENHOUSE",
        source_job_id="2002",
        company="Uber",
        title="Software Engineer",
        original_title="Software Engineer",
        source_url="https://boards.greenhouse.io/uber/jobs/2002",
        content_hash="hash2002",
    )
    ref = SourceReferenceRow(
        job_id=job_id,
        source="GREENHOUSE",
        source_job_id="2002",
        source_url="https://boards.greenhouse.io/uber/jobs/2002",
        application_url="https://boards.greenhouse.io/uber/jobs/2002#apply",
    )
    job.source_references.append(ref)
    db_session.add(job)
    db_session.commit()

    saved_job = db_session.query(JobRow).filter_by(id=job_id).first()
    assert len(saved_job.source_references) == 1
    assert saved_job.source_references[0].source_job_id == "2002"


def test_job_version_relationship(db_session):
    job_id = str(uuid.uuid4())
    job = JobRow(
        id=job_id,
        canonical_key="test_canonical_key_3",
        source="LEVER",
        source_job_id="3003",
        company="Figma",
        title="Product Engineer",
        original_title="Product Engineer",
        source_url="https://jobs.lever.co/figma/3003",
        content_hash="hash3003_v1",
    )
    version = JobVersionRow(
        job_id=job_id,
        content_hash="hash3003_v1",
        title="Product Engineer",
        description="Initial description",
        changes_summary="Initial version recorded",
    )
    job.versions.append(version)
    db_session.add(job)
    db_session.commit()

    saved_job = db_session.query(JobRow).filter_by(id=job_id).first()
    assert len(saved_job.versions) == 1
    assert saved_job.versions[0].changes_summary == "Initial version recorded"


def test_discovery_run_crud(db_session):
    run = DiscoveryRunRow(
        source="GREENHOUSE",
        source_identifier="stripe",
        candidates_discovered=25,
        pages_fetched=25,
        jobs_new=20,
        jobs_updated=3,
        jobs_duplicate=2,
        jobs_failed=0,
        status="completed",
        duration_seconds=3.5,
    )
    db_session.add(run)
    db_session.commit()

    saved = db_session.query(DiscoveryRunRow).filter_by(source_identifier="stripe").first()
    assert saved is not None
    assert saved.jobs_new == 20
    assert saved.duration_seconds == 3.5
    assert saved.status == "completed"
