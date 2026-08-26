"""Tests for Job domain models (Milestone 1)."""

from datetime import datetime

from app.jobs.models import (
    DiscoveryRun,
    EmploymentType,
    ExperienceLevel,
    GraduationRequirement,
    JobSourceType,
    JobStatus,
    NormalizedJob,
    ProcessingStatus,
    RawJob,
    RemoteType,
    SourceReference,
)


def test_raw_job_creation():
    raw = RawJob(
        source=JobSourceType.GREENHOUSE,
        source_job_id="12345",
        source_url="https://boards-api.greenhouse.io/v1/boards/stripe/jobs/12345",
        discovered_url="https://boards.greenhouse.io/stripe",
        raw_title="Software Engineer - Backend",
        raw_content="<p>Full job description here</p>",
        content_type="html",
        raw_location="Seattle, WA",
        raw_metadata={"department": "Engineering"},
    )
    assert raw.source == JobSourceType.GREENHOUSE
    assert raw.source_job_id == "12345"
    assert raw.raw_title == "Software Engineer - Backend"
    assert raw.content_type == "html"
    assert raw.raw_metadata["department"] == "Engineering"
    assert isinstance(raw.discovered_at, datetime)


def test_normalized_job_with_graduation_requirement():
    grad_req = GraduationRequirement(
        minimum_year=2026,
        maximum_year=2028,
        exact_years=[2026, 2027, 2028],
        original_text="Graduating between 2026 and 2028",
        extraction_confidence=0.95,
    )

    job = NormalizedJob(
        canonical_key="hash_123456",
        source=JobSourceType.GREENHOUSE,
        source_job_id="12345",
        company="Stripe",
        title="Software Engineer - Backend",
        original_title="Software Engineer - Backend",
        description="Full backend engineering role description",
        original_description="<p>Full backend engineering role description</p>",
        location="Seattle, WA",
        locations=["Seattle, WA"],
        remote_type=RemoteType.HYBRID,
        employment_type=EmploymentType.FULL_TIME,
        experience_level=ExperienceLevel.ENTRY_LEVEL,
        graduation_requirement=grad_req,
        required_skills=["Python", "Go", "Distributed Systems"],
        technologies=["PostgreSQL", "Redis"],
        source_url="https://boards.greenhouse.io/stripe/jobs/12345",
        content_hash="abcdef1234567890",
        processing_status=ProcessingStatus.NORMALIZED,
        job_status=JobStatus.ACTIVE,
    )

    assert job.company == "Stripe"
    assert job.remote_type == RemoteType.HYBRID
    assert job.employment_type == EmploymentType.FULL_TIME
    assert job.graduation_requirement.minimum_year == 2026
    assert job.graduation_requirement.maximum_year == 2028
    assert "Go" in job.required_skills
    assert job.content_hash == "abcdef1234567890"


def test_source_reference():
    ref = SourceReference(
        job_id="job-uuid-1",
        source=JobSourceType.GREENHOUSE,
        source_job_id="12345",
        source_url="https://boards.greenhouse.io/stripe/jobs/12345",
        application_url="https://boards.greenhouse.io/stripe/jobs/12345#apply",
        metadata={"token": "stripe"},
    )
    assert ref.job_id == "job-uuid-1"
    assert ref.source == JobSourceType.GREENHOUSE
    assert ref.source_job_id == "12345"
    assert isinstance(ref.first_seen_at, datetime)


def test_discovery_run():
    run = DiscoveryRun(
        source=JobSourceType.GREENHOUSE,
        source_identifier="stripe",
        candidates_discovered=10,
        pages_fetched=10,
        jobs_new=8,
        jobs_updated=1,
        jobs_duplicate=1,
        jobs_failed=0,
        status="completed",
    )
    assert run.source == JobSourceType.GREENHOUSE
    assert run.candidates_discovered == 10
    assert run.jobs_new == 8
    assert run.status == "completed"
