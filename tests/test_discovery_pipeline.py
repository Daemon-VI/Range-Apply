"""Tests for the End-to-End Discovery Pipeline (Milestone 9)."""

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.jobs.database.models import Base, DiscoveryRunRow, JobRow, SourceReferenceRow
from app.jobs.models.enums import JobSourceType
from app.jobs.pipeline.discovery_service import JobDiscoveryService
from app.jobs.sources.greenhouse import GreenhouseSource

MOCK_BOARD_RESPONSE = {
    "jobs": [
        {
            "id": 5001,
            "title": "Senior Distributed Systems Engineer",
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/5001",
            "location": {"name": "Remote, USA"},
            "content": "<p>Looking for a Senior Backend Engineer with Go, Redis, and Distributed Systems experience.</p>",
            "updated_at": "2026-08-10T10:00:00Z",
        },
        {
            "id": 5002,
            "title": "Software Engineering Intern (Summer 2027)",
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/5002",
            "location": {"name": "Seattle, WA"},
            "content": "<p>Internship for students graduating in 2027 with Python and FastAPI skills.</p>",
            "updated_at": "2026-08-12T10:00:00Z",
        },
    ]
}


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


@pytest.mark.asyncio
async def test_discovery_pipeline_end_to_end(db_session):
    def mock_transport(request: httpx.Request):
        return httpx.Response(200, json=MOCK_BOARD_RESPONSE)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    gh_adapter = GreenhouseSource(client=mock_client)

    service = JobDiscoveryService()
    # Override adapter with mocked client
    service.get_source_adapter = lambda source_type: gh_adapter

    # Run 1: First ingestion
    run1 = await service.run_discovery(db_session, JobSourceType.GREENHOUSE, "stripe", company_name="Stripe")

    assert run1.status == "completed"
    assert run1.candidates_discovered == 2
    assert run1.jobs_new == 2
    assert run1.jobs_duplicate == 0
    assert run1.jobs_failed == 0

    assert db_session.query(JobRow).count() == 2
    assert db_session.query(SourceReferenceRow).count() == 2
    assert db_session.query(DiscoveryRunRow).count() == 1

    intern_job = db_session.query(JobRow).filter_by(source_job_id="5002").first()
    assert intern_job is not None
    assert intern_job.company == "Stripe"
    assert intern_job.employment_type == "INTERNSHIP"
    assert intern_job.graduation_requirement["minimum_year"] == 2027
    assert "Python" in intern_job.required_skills

    # Run 2: Idempotent second ingestion of the same board
    run2 = await service.run_discovery(db_session, JobSourceType.GREENHOUSE, "stripe", company_name="Stripe")

    assert run2.status == "completed"
    assert run2.candidates_discovered == 2
    assert run2.jobs_new == 0
    assert run2.jobs_duplicate == 2
    assert run2.jobs_failed == 0

    # Total jobs in database should remain 2!
    assert db_session.query(JobRow).count() == 2


@pytest.mark.asyncio
async def test_discovery_pipeline_failure_handling(db_session):
    def mock_transport(request: httpx.Request):
        return httpx.Response(404, json={"error": "Not Found"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    gh_adapter = GreenhouseSource(client=mock_client)

    service = JobDiscoveryService()
    service.get_source_adapter = lambda source_type: gh_adapter

    run = await service.run_discovery(db_session, JobSourceType.GREENHOUSE, "invalid_board")

    assert run.status == "failed"
    assert len(run.errors) > 0
    assert run.jobs_new == 0
