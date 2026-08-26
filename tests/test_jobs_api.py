"""Tests for Phase 2 API endpoints and dashboard views (Milestones 11 & 12)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.jobs.database.models import Base, JobRow
from app.main import app

# Create a clean file-backed or static in-memory database for testing
test_engine = create_engine("sqlite:///./test_api_runner.db", connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="module", autouse=True)
def init_test_database():
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    db = TestingSessionLocal()
    job = JobRow(
        id="test-job-uuid-1",
        canonical_key="stripe|software engineer|seattle, wa",
        source="GREENHOUSE",
        source_job_id="6001",
        company="Stripe",
        title="Software Engineer",
        original_title="Software Engineer",
        description="Looking for an engineer with Python and Go skills.",
        location="Seattle, WA",
        locations=["Seattle, WA"],
        remote_type="HYBRID",
        employment_type="FULL_TIME",
        experience_level="ENTRY_LEVEL",
        required_skills=["Python", "Go"],
        source_url="https://boards.greenhouse.io/stripe/jobs/6001",
        content_hash="hash6001",
        processing_status="NORMALIZED",
        job_status="ACTIVE",
    )
    db.add(job)
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()
    import os
    if os.path.exists("./test_api_runner.db"):
        try:
            os.remove("./test_api_runner.db")
        except Exception:
            pass


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_list_jobs(client):
    response = client.get("/api/v2/jobs")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["company"] == "Stripe"


def test_list_jobs_filter(client):
    response = client.get("/api/v2/jobs?source=GREENHOUSE&company=Stripe")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1

    response_empty = client.get("/api/v2/jobs?company=Nonexistent")
    assert response_empty.status_code == 200
    assert response_empty.json()["total"] == 0


def test_get_job_detail(client):
    response = client.get("/api/v2/jobs/test-job-uuid-1")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "test-job-uuid-1"
    assert data["title"] == "Software Engineer"
    assert "Python" in data["required_skills"]


def test_get_job_detail_404(client):
    response = client.get("/api/v2/jobs/unknown-id")
    assert response.status_code == 404


def test_get_job_stats(client):
    response = client.get("/api/v2/jobs/stats/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["total_jobs"] == 1
    assert data["active_jobs"] == 1
    assert data["by_source"]["GREENHOUSE"] == 1


def test_dashboard_views(client):
    res_overview = client.get("/dashboard/")
    assert res_overview.status_code == 200
    assert "CareerOS Discovery" in res_overview.text

    res_jobs = client.get("/dashboard/jobs")
    assert res_jobs.status_code == 200
    assert "Discovered Jobs" in res_jobs.text

    res_detail = client.get("/dashboard/jobs/test-job-uuid-1")
    assert res_detail.status_code == 200
    assert "Software Engineer" in res_detail.text

    res_runs = client.get("/dashboard/runs")
    assert res_runs.status_code == 200
    assert "Discovery Runs" in res_runs.text
